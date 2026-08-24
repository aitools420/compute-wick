"""Vast.ai adapter — true per-host marketplace offers.

POST https://console.vast.ai/api/v0/bundles  (Bearer key)
Body: filter-name -> {operator: value}, plus type/limit. Two passes: on-demand
and bid (interruptible). Docs: https://docs.vast.ai/api-reference/search/search-offers
"""
import time

import config
from core.schema import make_offer
from providers.common import request_json

NAME = "vast"
URL = "https://console.vast.ai/api/v0/bundles"
LINK = "https://cloud.vast.ai/"   # no documented per-offer permalink — console link
PAGE = 128                        # the server's hard page size, whatever limit we ask

_SEARCH = {
    "rentable": {"eq": True},
    "rented": {"eq": False},
}

def _country(geo):
    if not geo:
        return None
    tail = geo.split(",")[-1].strip()
    return tail if len(tail) == 2 else None

def parse(payload, fetched_at: int, cls: str = None) -> list[dict]:
    """cls, when given, fixes the offer class for the whole page — the offer id
    is hashed from it, so it MUST be the pass's class, not r['is_bid'] (which the
    caller would otherwise override after the id is already frozen)."""
    rows = payload.get("offers") if isinstance(payload, dict) else payload
    offers = []
    for r in rows or []:
        try:
            price = r.get("dph_total")
            if not price or not r.get("gpu_name"):
                continue
            vram_mb = r.get("gpu_ram") or 0
            offers.append(make_offer(
                provider=NAME,
                gpu_model=r["gpu_name"],
                gpu_count=r.get("num_gpus") or 1,
                vram_gb=(vram_mb / 1024) if vram_mb else None,
                price_hr=price,
                region=r.get("geolocation"),
                country=_country(r.get("geolocation")),
                cls=cls if cls else ("interruptible" if r.get("is_bid") else "on_demand"),
                availability="available",
                reliability=r.get("reliability2", r.get("reliability")),
                provider_link=LINK,
                provider_offer_id=r.get("id"),
                fetched_at=fetched_at,
            ))
        except (ValueError, TypeError):
            continue
    return offers

# The server pages /bundles at 128 rows no matter the requested limit, so a
# keyed fetch walks the book cheapest-first with a dph_total cursor. 8 pages
# x 128 = the cheapest ~1k offers per class — the head of the book that
# ranking actually uses. Keyless stays a single polite page.
MAX_PAGES = 8

def fetch_offers():
    """-> (offers, http_status, note, complete). complete is False when a page
    request failed mid-walk, so the poller can refuse to publish a fragment as
    the whole market. Hitting the page budget with more inventory behind it is
    by design, not incompleteness."""
    keyed = bool(config.VAST_API_KEY)
    note = "" if keyed else "no VAST_API_KEY configured"
    headers = {"Authorization": f"Bearer {config.VAST_API_KEY}"} if keyed else {}
    fetched_at = int(time.time())
    offers, last_status, complete = [], None, True
    seen: set[str] = set()
    for typ, cls in (("on-demand", "on_demand"), ("bid", "interruptible")):
        cursor = None
        for _ in range(MAX_PAGES if keyed else 1):
            body = dict(_SEARCH)
            body["type"] = typ
            body["limit"] = 2000
            body["order"] = [["dph_total", "asc"]]
            if cursor is not None:
                # gte + de-dup so offers priced exactly at a page boundary are
                # not skipped; the cursor row reappears and is dropped by `seen`.
                body["dph_total"] = {"gte": cursor}
            payload, status = request_json("POST", URL, headers=headers,
                                           json_body=body)
            last_status = status
            if status != 200 or payload is None:
                note = f"{typ} pass HTTP {status}"
                complete = False          # a fragment, not the book — flag it
                break
            raw = payload.get("offers") if isinstance(payload, dict) else payload
            for o in parse(payload, fetched_at, cls=cls):
                if o["id"] not in seen:
                    seen.add(o["id"])
                    offers.append(o)
            if not raw or len(raw) < PAGE:
                break
            nxt = raw[-1].get("dph_total")
            if nxt is None or nxt == cursor:
                break                     # a full page all at one price: stop, not spin
            cursor = nxt
    return offers, last_status, note, complete
