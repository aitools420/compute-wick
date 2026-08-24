"""RunPod adapter — fixed GPU-class prices synthesized into offers.

GraphQL POST https://api.runpod.io/graphql (Bearer key): one gpuTypes query
carries on-demand AND spot (interruptible) prices for both clouds, which the
REST v2 catalog does not. Docs: https://graphql-spec.runpod.io/
Rows become offers per (gpu class x cloud x price kind); availability comes
from lowestPrice.stockStatus, there is no per-host reliability.
"""
import time

import config
from core.schema import make_offer
from providers.common import request_json

NAME = "runpod"
URL = "https://api.runpod.io/graphql"
LINK = "https://www.runpod.io/console/deploy"

QUERY = """query GpuTypes {
  gpuTypes {
    id displayName memoryInGb secureCloud communityCloud
    securePrice communityPrice secureSpotPrice communitySpotPrice
    maxGpuCount
    lowestPrice(input: {gpuCount: 1}) { stockStatus }
  }
}"""

_KINDS = (
    ("securePrice", "secure cloud", "on_demand"),
    ("communityPrice", "community cloud", "on_demand"),
    ("secureSpotPrice", "secure cloud", "interruptible"),
    ("communitySpotPrice", "community cloud", "interruptible"),
)

def parse(payload, fetched_at: int) -> list[dict]:
    types = (((payload or {}).get("data") or {}).get("gpuTypes")) or []
    offers = []
    for g in types:
        stock = ((g.get("lowestPrice") or {}).get("stockStatus"))
        for field, region, cls in _KINDS:
            price = g.get(field)
            if not price:
                continue
            cloud_ok = g.get("secureCloud") if "secure" in field else g.get("communityCloud")
            if cloud_ok is False:
                continue
            try:
                offers.append(make_offer(
                    provider=NAME,
                    gpu_model=g.get("displayName") or g.get("id"),
                    gpu_count=1,
                    vram_gb=g.get("memoryInGb"),
                    price_hr=price,
                    region=region,
                    country=None,
                    cls=cls,
                    availability=stock,
                    reliability=None,
                    provider_link=LINK,
                    provider_offer_id=f"{g.get('id')}:{region.split()[0]}:{cls}",
                    fetched_at=fetched_at,
                ))
            except (ValueError, TypeError):
                continue
    return offers

def fetch_offers():
    """-> (offers, http_status, note)"""
    headers = ({"Authorization": f"Bearer {config.RUNPOD_API_KEY}"}
               if config.RUNPOD_API_KEY else {})
    note = "" if config.RUNPOD_API_KEY else "no RUNPOD_API_KEY configured"
    fetched_at = int(time.time())
    payload, status = request_json("POST", URL, headers=headers,
                                   json_body={"query": QUERY})
    if status != 200 or payload is None or payload.get("errors"):
        err = ((payload or {}).get("errors") or [{}])[0].get("message", "")
        return [], status, (f"HTTP {status} {err}".strip() or note)
    return parse(payload, fetched_at), status, note
