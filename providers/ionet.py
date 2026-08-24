"""io.net adapter — keyless network market snapshot.

GET https://api.io.solutions/v1/io-explorer/network/market-snapshot
    ?hardware_type=gpu
Feeds their public explorer; undocumented, so shape drift lands as a recorded
provider_health failure, never a silent gap. Each SKU carries USD/hr per card
for up to five rental products; each priced product becomes one offer (the
runpod per-cloud pattern), region names the product. Only SKUs with idle
capacity are listed — a fully-hired price is a quote nobody can take.
"""
import re
import time

from core.schema import make_offer
from providers.common import request_json

NAME = "ionet"
URL = "https://api.io.solutions/v1/io-explorer/network/market-snapshot"
LINK = "https://cloud.io.net/"

_PRODUCTS = (
    ("price", "ray cluster"),
    ("baremetal_price", "bare metal"),
    ("kubernetes_price", "kubernetes"),
    ("caas_price", "container"),
    ("vmaas_price", "vm"),
)

def _vram(name: str):
    m = re.search(r"(\d+)\s*GB", name or "")
    return float(m.group(1)) if m else None

def parse(payload, fetched_at: int) -> list[dict]:
    offers = []
    for row in (payload or {}).get("data", []):
        try:
            if (row.get("type") or "").upper() != "GPU":
                continue
            idle = row.get("idle") or 0
            if not idle:
                continue
            name = row.get("hardware_name") or ""
            for field, product in _PRODUCTS:
                price = row.get(field)
                if not price:
                    continue
                offers.append(make_offer(
                    provider=NAME,
                    gpu_model=name,
                    gpu_count=1,
                    vram_gb=_vram(name),
                    price_hr=float(price),
                    region=product,
                    country=None,
                    cls="on_demand",
                    availability=f"{idle} idle of {row.get('total')}",
                    reliability=None,
                    provider_link=LINK,
                    provider_offer_id=f"{row.get('id')}:{field}",
                    fetched_at=fetched_at,
                ))
        except (ValueError, TypeError):
            continue
    return offers

def fetch_offers():
    """-> (offers, http_status, note)"""
    fetched_at = int(time.time())
    payload, status = request_json("GET", URL, params={"hardware_type": "gpu"})
    if status != 200 or not isinstance(payload, dict):
        return [], status, f"HTTP {status}"
    if payload.get("status") not in (None, "succeeded"):
        return [], status, f"api status {payload.get('status')!r}"
    return parse(payload, fetched_at), status, ""
