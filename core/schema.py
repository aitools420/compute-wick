"""Canonical Offer schema — the one shape both humans and agents consume.

An Offer is a plain dict (JSON-native, survives SQLite round-trips). Two provider
realities must both fit honestly:
  - Vast: true per-host offers (reliability, geolocation, interruptible bids)
  - RunPod: fixed GPU-class prices synthesized into offers (availability level, no host)
"""
import hashlib
import re

CLASSES = ("on_demand", "interruptible", "reserved")
FIELDS = ("id", "provider", "gpu_model", "gpu_count", "vram_gb", "price_hr",
          "price_per_gpu_hr", "region", "country", "class", "availability",
          "reliability", "provider_link", "provider_offer_id", "fetched_at")

def normalize_gpu_model(raw: str) -> str:
    """'RTX_4090' / 'NVIDIA GeForce RTX 4090' / 'rtx4090' -> 'RTX 4090'."""
    s = (raw or "").replace("_", " ").strip()
    s = re.sub(r"(?i)^nvidia\s+", "", s)
    s = re.sub(r"(?i)^geforce\s+", "", s)
    s = re.sub(r"(?i)\bten?sorrt\b", "", s)
    s = re.sub(r"(?i)^(rtx|gtx)(\d)", r"\1 \2", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s.upper() if s else "UNKNOWN"

def make_offer(*, provider: str, gpu_model: str, gpu_count: int, vram_gb,
               price_hr: float, region, country, cls: str, availability,
               reliability, provider_link, provider_offer_id, fetched_at: int) -> dict:
    if cls not in CLASSES:
        raise ValueError(f"bad offer class: {cls}")
    if not (isinstance(price_hr, (int, float)) and price_hr > 0):
        raise ValueError(f"bad price_hr: {price_hr!r}")
    gpu_count = int(gpu_count) if gpu_count else 1
    if gpu_count < 1:
        raise ValueError(f"bad gpu_count: {gpu_count}")
    model = normalize_gpu_model(gpu_model)
    oid = hashlib.sha1(
        f"{provider}|{provider_offer_id}|{model}|{cls}".encode()).hexdigest()[:16]
    return {
        "id": oid,
        "provider": provider,
        "gpu_model": model,
        "gpu_count": gpu_count,
        "vram_gb": round(float(vram_gb), 1) if vram_gb else None,
        "price_hr": round(float(price_hr), 6),
        "price_per_gpu_hr": round(float(price_hr) / gpu_count, 6),
        "region": (region or "").strip(", ") or None,
        "country": (country or "").upper()[:2] or None,
        "class": cls,
        "availability": availability,
        "reliability": round(float(reliability), 4) if reliability is not None else None,
        "provider_link": provider_link,
        "provider_offer_id": str(provider_offer_id),
        "fetched_at": int(fetched_at),
    }
