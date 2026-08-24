"""Akash adapter — decentralized GPU network, public aggregate pricing.

GET https://console-api.akash.network/v1/gpu-prices answers keyless: one row
per (model, ram, interface) with min/med/avg USD prices and real availability
counts. We list each row once at the MIN price — the best obtainable lease —
as on_demand; rows with no price or nothing available are not offers and drop.
RAM joins the model name only when one model ships in several sizes (v100
16/32GB), so common names still match the other feeds' spelling.
"""
import time

from core.schema import make_offer
from providers.common import request_json

NAME = "akash"
URL = "https://console-api.akash.network/v1/gpu-prices"
LINK = "https://console.akash.network/rent-gpu"

def _ram_gb(ram):
    try:
        return float(str(ram).lower().removesuffix("gi"))
    except (ValueError, AttributeError):
        return None

def parse(payload, fetched_at: int) -> list[dict]:
    models = (payload or {}).get("models") or []
    rams: dict[str, set] = {}
    for m in models:
        rams.setdefault(m.get("model") or "", set()).add(m.get("ram"))
    offers = []
    for m in models:
        try:
            price = ((m.get("price") or {}).get("min"))
            avail = (m.get("availability") or {})
            if not price or not (avail.get("available") or 0):
                continue
            name = m.get("model") or ""
            gb = _ram_gb(m.get("ram"))
            if len(rams.get(name, ())) > 1 and gb:
                name = f"{name} {gb:g}GB"
            vendor = m.get("vendor") or ""
            if vendor and vendor != "nvidia":
                name = f"{vendor} {name}"
            offers.append(make_offer(
                provider=NAME,
                gpu_model=name,
                gpu_count=1,
                vram_gb=gb,
                price_hr=price,
                region=None,
                country=None,
                cls="on_demand",
                availability=f"{avail.get('available')}/{avail.get('total')} GPUs",
                reliability=None,
                provider_link=LINK,
                provider_offer_id=f"{m.get('model')}:{m.get('ram')}:{m.get('interface')}",
                fetched_at=fetched_at,
            ))
        except (ValueError, TypeError):
            continue
    return offers

def fetch_offers():
    """-> (offers, http_status, note)"""
    fetched_at = int(time.time())
    payload, status = request_json("GET", URL)
    if status != 200 or payload is None:
        return [], status, f"HTTP {status}"
    return parse(payload, fetched_at), status, ""
