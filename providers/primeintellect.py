"""Prime Intellect adapter — keyed aggregator (lambdalabs, massedcompute, nebius,
vultr, primecompute behind one book).

GET https://api.primeintellect.ai/api/v1/availability/   (Bearer; trailing slash
required — without it the API answers 307 with an empty body)
Payload is {gpuType: [entry,...]}. prices.onDemand is the WHOLE-NODE $/hr
(8x H100 SXM5 = 31.92 -> $3.99/GPU). The upstream cloud goes in availability,
not provider — the rental account here is Prime Intellect.
"""
import re
import time

import config
from core.schema import make_offer
from providers.common import request_json

NAME = "primeintellect"
BASE = "https://api.primeintellect.ai"
LINK = "https://www.primeintellect.ai/"

MODELS = {"A6000": "RTX A6000", "RTX6000Ada": "RTX 6000 ADA",
          "RTX_PRO_6000B": "RTX PRO 6000"}

def _model(gpu_type: str, socket: str):
    base = re.sub(r"_\d+GB$", "", gpu_type or "")
    if base == "CPU_NODE":
        return None
    if base in ("H100", "A100", "H200", "B200"):
        s = (socket or "").upper()
        if s.startswith("SXM"):
            return f"{base} SXM4" if base == "A100" else f"{base} SXM"
        if s.startswith("PCI"):
            return f"{base} PCIE"
    return MODELS.get(base, base.replace("_", " "))

def parse(payload, fetched_at: int) -> list[dict]:
    offers = []
    for gpu_type, entries in (payload or {}).items():
        for e in entries:
            try:
                model = _model(gpu_type, e.get("socket"))
                price = float((e.get("prices") or {}).get("onDemand") or 0)
                count = e.get("gpuCount") or 0
                if not model or not price or not count:
                    continue
                if (e.get("stockStatus") or "").lower() != "available":
                    continue
                m = re.search(r"_(\d+)GB$", gpu_type)
                total_vram = e.get("gpuMemory")
                vram = (total_vram / count if total_vram else
                        int(m.group(1)) if m else None)
                offers.append(make_offer(
                    provider=NAME,
                    gpu_model=model,
                    gpu_count=count,
                    vram_gb=vram,
                    price_hr=price,
                    region=e.get("dataCenter") or e.get("region"),
                    country=e.get("country"),
                    cls="interruptible" if e.get("isSpot") else "on_demand",
                    availability=f"available via {e.get('provider')}",
                    reliability=None,
                    provider_link=LINK,
                    provider_offer_id=(f"{e.get('cloudId')}:{e.get('provider')}:"
                                       f"{e.get('dataCenter')}"),
                    fetched_at=fetched_at,
                ))
            except (ValueError, TypeError):
                continue
    return offers

def fetch_offers():
    """-> (offers, http_status, note, complete)"""
    if not config.PRIME_INTELLECT_API_KEY:
        return [], None, "PRIME_INTELLECT_API_KEY not set", True
    fetched_at = int(time.time())
    payload, status = request_json(
        "GET", f"{BASE}/api/v1/availability/",
        headers={"Authorization": f"Bearer {config.PRIME_INTELLECT_API_KEY}"})
    if status != 200:
        return [], status, f"availability HTTP {status}", True
    return parse(payload, fetched_at), status, "", True
