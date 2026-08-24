"""Novita AI adapter — keyed, GPU-instance product catalog.

GET https://api.novita.ai/gpu-instance/openapi/v1/products  (Bearer auth)
`price` is per-GPU per-hour in 1e-5 USD units (H100 SXM = 339000 -> $3.39/hr,
cross-checked against their site 2026-08-24). `availableDeploy` is the product-level
stock gate; regions list where the SKU exists, so one offer per (product, region).
"""
import re
import time

import config
from core.schema import make_offer
from providers.common import request_json

NAME = "novita"
BASE = "https://api.novita.ai"
LINK = "https://novita.ai/"

COUNTRY = {"US": "US", "EU-GB": "GB", "EU-GER": "DE", "EU-IS": "IS",
           "AS-SGP": "SG", "AS-AE": "AE", "AS-IN": "IN", "CN-HK": "HK",
           "JP-TYO": "JP", "OC-AU": "AU", "SA-BR": "BR", "AF-ZA": "ZA"}

def _country(code: str):
    for prefix, iso in COUNTRY.items():
        if code.startswith(prefix):
            return iso
    return None

def parse(payload, fetched_at: int) -> list[dict]:
    offers = []
    for p in (payload or {}).get("data", []):
        try:
            if not p.get("availableDeploy"):
                continue
            price = float(p.get("price") or 0) / 1e5
            if not price:
                continue
            name = p.get("name") or ""
            m = re.search(r"(\d+)GB", name)
            vram = int(m.group(1)) if m else None
            model = re.sub(r"\s*\d+GB\s*", " ", name)
            model = re.sub(r"\s*\(High frequency\)", "", model).strip()
            for region in p.get("regions", []):
                code = region.split(" (")[0].strip()
                offers.append(make_offer(
                    provider=NAME,
                    gpu_model=model,
                    gpu_count=1,
                    vram_gb=vram,
                    price_hr=price,
                    region=code,
                    country=_country(code),
                    cls="on_demand",
                    availability="deployable",
                    reliability=None,
                    provider_link=LINK,
                    provider_offer_id=f"{p.get('id')}:{code}",
                    fetched_at=fetched_at,
                ))
        except (ValueError, TypeError):
            continue
    return offers

def fetch_offers():
    """-> (offers, http_status, note, complete)"""
    if not config.NOVITA_API_KEY:
        return [], None, "NOVITA_API_KEY not set", True
    fetched_at = int(time.time())
    payload, status = request_json(
        "GET", f"{BASE}/gpu-instance/openapi/v1/products",
        headers={"Authorization": f"Bearer {config.NOVITA_API_KEY}"})
    if status != 200:
        return [], status, f"products HTTP {status}", True
    return parse(payload, fetched_at), status, "", True
