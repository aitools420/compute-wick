"""DataCrunch adapter — fixed instance-type catalog, keyless.

GET https://api.datacrunch.io/v1/instance-types answers WITHOUT a key: 64 types
with on-demand and spot prices as strings. Docs: https://docs.datacrunch.io/
CPU-only rows (number_of_gpus=0) are skipped. gpu_memory is the TOTAL across
the instance, so per-GPU VRAM = size / count. Locations/availability endpoints
are key-gated, so region is the honest coarse truth (all DCs are European) and
availability is None, never guessed.
"""
import time

from core.schema import make_offer
from providers.common import request_json

NAME = "datacrunch"
URL = "https://api.datacrunch.io/v1/instance-types"
LINK = "https://cloud.datacrunch.io/"

_KINDS = (("price_per_hour", "on_demand"), ("spot_price", "interruptible"))

def parse(payload, fetched_at: int) -> list[dict]:
    offers = []
    for r in payload if isinstance(payload, list) else []:
        try:
            n = (r.get("gpu") or {}).get("number_of_gpus") or 0
            if n < 1:
                continue
            total_vram = (r.get("gpu_memory") or {}).get("size_in_gigabytes") or 0
            for field, cls in _KINDS:
                price = float(r.get(field) or 0)
                if not price:
                    continue
                offers.append(make_offer(
                    provider=NAME,
                    gpu_model=r.get("model") or "",
                    gpu_count=n,
                    vram_gb=(total_vram / n) if total_vram else None,
                    price_hr=price,
                    region="Europe",
                    country=None,
                    cls=cls,
                    availability=None,
                    reliability=None,
                    provider_link=LINK,
                    provider_offer_id=f"{r.get('instance_type')}:{cls}",
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
