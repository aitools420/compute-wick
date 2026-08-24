"""THE FEE LEVER — the single choke point every outbound price passes through.

Contract: no API route and no MCP tool may emit an offer that has not been through
apply(). At launch FEE_BPS=0 so price_final_hr == price_hr and the platform fee is
literally zero. Turning fees on later = set FEE_BPS in .env and restart — no surgery.
Referral tags ride the same seam: empty REF_* means plain provider links.
"""
from urllib.parse import urlencode

import config

_REF_PARAMS = {
    "vast": ("ref_id", lambda: config.REF_VAST),
    "runpod": ("ref", lambda: config.REF_RUNPOD),
}

def apply(offer: dict) -> dict:
    fee_bps = config.FEE_BPS
    out = dict(offer)
    out["fee_bps"] = fee_bps
    out["price_final_hr"] = round(offer["price_hr"] * (1 + fee_bps / 10000), 6)
    out["price_final_per_gpu_hr"] = round(
        offer["price_per_gpu_hr"] * (1 + fee_bps / 10000), 6)
    link = offer.get("provider_link")
    param = _REF_PARAMS.get(offer["provider"])
    if link and param:
        name, getter = param
        code = getter()
        if code:
            sep = "&" if "?" in link else "?"
            link = f"{link}{sep}{urlencode({name: code})}"
    out["link"] = link
    return out

def apply_all(offers: list[dict]) -> list[dict]:
    return [apply(o) for o in offers]

def apply_price(price: float) -> float:
    """The same fee seam for bare prices (history aggregates)."""
    return round(price * (1 + config.FEE_BPS / 10000), 6)
