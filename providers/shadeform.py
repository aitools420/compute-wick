"""Shadeform adapter — keyed aggregator, 19 clouds behind one API (crusoe,
lambdalabs, nebius, voltagepark, paperspace, scaleway, digitalocean, ...).

GET https://api.shadeform.ai/v1/instances/types   (X-API-KEY header)
hourly_price is WHOLE-NODE US cents/hr (8x A6000 = 392 -> $0.49/GPU).
One offer per (instance type, region with available=true); the upstream cloud
goes in availability — the rental account here is Shadeform.
"""
import re
import time

import config
from core.schema import make_offer
from providers.common import request_json

NAME = "shadeform"
BASE = "https://api.shadeform.ai"
LINK = "https://www.shadeform.ai/"

MODELS = {"A4000": "RTX A4000", "A5000": "RTX A5000", "A6000": "RTX A6000",
          "RTXPro6000": "RTX PRO 6000", "RTX6000Ada": "RTX 6000 ADA",
          "RTX4000Ada": "RTX 4000 ADA", "RTX4090": "RTX 4090",
          "RTX5090": "RTX 5090", "RTX6000": "RTX 6000", "H100_nvl": "H100 NVL",
          "GAUDI2": "GAUDI 2"}

def _model(gpu_type: str, interconnect: str):
    base = re.sub(r"_(80|32)G$", "", gpu_type or "")
    if base in ("CPU", ""):
        return None
    if base in ("H100", "A100", "H200", "B200"):
        ic = (interconnect or "").lower()
        if ic.startswith("sxm"):
            return f"{base} SXM4" if base == "A100" else f"{base} SXM"
        if ic.startswith("pci"):
            return f"{base} PCIE"
    return MODELS.get(base, base)

def _country(region: str, display_name: str):
    tok = (display_name or "").split(",")[0].strip()
    if re.fullmatch(r"[A-Z]{2}", tok):
        return tok
    r = (region or tok).lower()
    if r.startswith("us"):
        return "US"
    if r.startswith(("tyo", "jp")):
        return "JP"
    if r.startswith(("syd", "au")):
        return "AU"
    return None

def parse(payload, fetched_at: int) -> list[dict]:
    offers = []
    for t in (payload or {}).get("instance_types", []):
        try:
            cfg = t.get("configuration") or {}
            model = _model(t.get("gpu_type"), t.get("interconnect"))
            count = t.get("num_gpus") or 0
            price = float(t.get("hourly_price") or 0) / 100
            if not model or not count or not price:
                continue
            for r in t.get("availability", []):
                if not r.get("available"):
                    continue
                region = r.get("region")
                offers.append(make_offer(
                    provider=NAME,
                    gpu_model=model,
                    gpu_count=count,
                    vram_gb=cfg.get("vram_per_gpu_in_gb"),
                    price_hr=price,
                    region=region,
                    country=_country(region, r.get("display_name")),
                    cls="on_demand",
                    availability=f"available via {t.get('cloud')}"
                                 f" ({t.get('deployment_type')})",
                    reliability=None,
                    provider_link=LINK,
                    provider_offer_id=(f"{t.get('cloud')}:"
                                       f"{t.get('shade_instance_type')}:{region}"),
                    fetched_at=fetched_at,
                ))
        except (ValueError, TypeError):
            continue
    return offers

def fetch_offers():
    """-> (offers, http_status, note, complete)"""
    if not config.SHADEFORM_API_KEY:
        return [], None, "SHADEFORM_API_KEY not set", True
    fetched_at = int(time.time())
    payload, status = request_json(
        "GET", f"{BASE}/v1/instances/types",
        headers={"X-API-KEY": config.SHADEFORM_API_KEY})
    if status != 200:
        return [], status, f"instance types HTTP {status}", True
    return parse(payload, fetched_at), status, "", True
