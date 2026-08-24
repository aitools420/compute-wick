"""Hyperstack (NexGen Cloud) adapter — keyed, flavor catalog × pricebook join.

GET https://infrahub-api.nexgencloud.com/v1/core/flavors  (per-region GPU flavors)
GET https://infrahub-api.nexgencloud.com/v1/pricebook      (per-GPU $/hr, keys match
    flavor `gpu` names exactly, including the -spot variants; verified 2026-08-24)
GET https://infrahub-api.nexgencloud.com/v1/core/stocks    (per-region free counts)
All three need the `api_key` header. vCPU/RAM price 0 on GPU flavors, so
flavor price = per-GPU price × gpu_count.
"""
import time

import config
from core.schema import make_offer
from providers.common import request_json

NAME = "hyperstack"
BASE = "https://infrahub-api.nexgencloud.com/v1"
LINK = "https://www.hyperstack.cloud/"

COUNTRY = {"CANADA": "CA", "NORWAY": "NO", "US": "US"}

# base gpu name (spot suffix stripped) -> (book model vocab, vram_gb)
MODELS = {
    "A100-80G-PCIe": ("A100 PCIE", 80),
    "A100-80G-PCIe-NVLink": ("A100 PCIE", 80),
    "A100-80G-SXM4": ("A100 SXM4", 80),
    "H100-80G-PCIe": ("H100 PCIE", 80),
    "H100-80G-PCIe-NVLink": ("H100 PCIE", 80),
    "H100-80G-SXM5": ("H100 SXM", 80),
    "H200-141G-SXM5": ("H200", 141),
    "B200-SXM": ("B200", 192),
    "B300-SXM": ("B300", 288),
    "L40": ("L40", 48),
    "RTX-A4000": ("RTX A4000", 16),
    "RTX-A6000": ("RTX A6000", 48),
    "RTX-PRO6000-SE": ("RTX PRO 6000 S", 96),
}

def _stock_note(stocks_payload, region: str, base_gpu: str):
    for s in (stocks_payload or {}).get("stocks", []):
        if s.get("region") != region:
            continue
        for m in s.get("models", []):
            if m.get("model") == base_gpu:
                return f"{m.get('available')} available in {region}"
    return None

def parse(flavors_payload, pricebook_payload, stocks_payload, fetched_at: int) -> list[dict]:
    prices = {}
    for p in pricebook_payload or []:
        try:
            prices[p["name"]] = float(p["value"])
        except (KeyError, ValueError, TypeError):
            continue
    offers = []
    for group in (flavors_payload or {}).get("data", []):
        for f in group.get("flavors", []):
            try:
                raw = f.get("gpu") or ""
                count = f.get("gpu_count") or 0
                if not raw or not count or not f.get("stock_available"):
                    continue
                per_gpu = prices.get(raw)
                if not per_gpu:
                    continue
                spot = raw.endswith("-spot")
                base = raw[:-5] if spot else raw
                model, vram = MODELS.get(base) or (base.replace("-", " "), None)
                region = f.get("region_name") or ""
                note = _stock_note(stocks_payload, region, base) or "in stock"
                if "NVLink" in base:
                    note += "; NVLink"
                offers.append(make_offer(
                    provider=NAME,
                    gpu_model=model,
                    gpu_count=count,
                    vram_gb=vram,
                    price_hr=per_gpu * count,
                    region=region,
                    country=COUNTRY.get(region.rsplit("-", 1)[0]),
                    cls="interruptible" if spot else "on_demand",
                    availability=note,
                    reliability=None,
                    provider_link=LINK,
                    provider_offer_id=f"{region}:{f.get('name')}",
                    fetched_at=fetched_at,
                ))
            except (ValueError, TypeError):
                continue
    return offers

def fetch_offers():
    """-> (offers, http_status, note, complete)"""
    if not config.HYPERSTACK_API_KEY:
        return [], None, "HYPERSTACK_API_KEY not set", True
    fetched_at = int(time.time())
    h = {"api_key": config.HYPERSTACK_API_KEY}
    fl, s1 = request_json("GET", f"{BASE}/core/flavors", headers=h)
    pb, s2 = request_json("GET", f"{BASE}/pricebook", headers=h)
    if s1 != 200 or s2 != 200:
        # both legs are essential: no flavors = no offers, no pricebook = no prices
        return [], s1 if s1 != 200 else s2, f"flavors HTTP {s1}, pricebook HTTP {s2}", True
    st, s3 = request_json("GET", f"{BASE}/core/stocks", headers=h)
    note = "" if s3 == 200 else f"stocks HTTP {s3}, free counts omitted"
    return parse(fl, pb, st if s3 == 200 else None, fetched_at), s1, note, True
