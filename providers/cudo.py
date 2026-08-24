"""Cudo Compute adapter — keyless catalog with live per-DC free counts.

GET https://rest.compute.cudo.org/v1/vms/machine-types  (VM, per-GPU price)
GET https://rest.compute.cudo.org/v1/machines-types     (bare metal, whole box)
GET https://rest.compute.cudo.org/v1/vms/gpu-models     (VRAM join)
All answer 200 without a key (verified 2026-08-24). Their public on-demand
signup closed 2026-03 — access is by request, so availability says so; the
prices themselves are live and real.

VM rows price the GPU component (gpuPriceHr — how cudo itself quotes);
bare-metal rows carry the whole machine's priceHr like a vast host offer.
"""
import time

from core.schema import make_offer
from providers.common import request_json

NAME = "cudo"
BASE = "https://rest.compute.cudo.org"
LINK = "https://www.cudocompute.com/"

def _country(dc_id: str):
    return (dc_id or "")[:2].upper() or None

def _vram_map(payload) -> dict:
    return {g.get("id"): g.get("memoryGib")
            for g in (payload or {}).get("gpuModels", []) if g.get("id")}

def parse(vm_payload, bm_payload, gpu_payload, fetched_at: int) -> list[dict]:
    vram = _vram_map(gpu_payload)
    offers = []
    for m in (vm_payload or {}).get("machineTypes", []):
        try:
            free = m.get("totalGpuFree") or 0
            price = float(((m.get("gpuPriceHr") or {}).get("value")) or 0)
            if not free or not price or not m.get("gpuModel"):
                continue
            offers.append(make_offer(
                provider=NAME,
                gpu_model=m["gpuModel"],
                gpu_count=1,
                vram_gb=vram.get(m.get("gpuModelId")),
                price_hr=price,
                region=m.get("dataCenterId"),
                country=_country(m.get("dataCenterId")),
                cls="on_demand",
                availability=f"{free} GPUs free; access by request",
                reliability=None,
                provider_link=LINK,
                provider_offer_id=f"vm:{m.get('dataCenterId')}:{m.get('machineType')}",
                fetched_at=fetched_at,
            ))
        except (ValueError, TypeError):
            continue
    for m in (bm_payload or {}).get("machineTypes", []):
        try:
            if not (m.get("machinesFree") or 0):
                continue
            price = None
            for p in m.get("prices", []):
                if p.get("commitmentTerm") == "COMMITMENT_TERM_NONE":
                    price = float((p.get("priceHr") or {}).get("value") or 0)
            gpus = m.get("gpus") or 0
            if not price or not gpus or not m.get("gpuModelId"):
                continue
            offers.append(make_offer(
                provider=NAME,
                gpu_model=m["gpuModelId"].replace("nvidia-", ""),
                gpu_count=gpus,
                vram_gb=vram.get(m.get("gpuModelId")),
                price_hr=price,
                region=m.get("dataCenterId"),
                country=_country(m.get("dataCenterId")),
                cls="on_demand",
                availability=f"{m['machinesFree']} machines free; access by request",
                reliability=None,
                provider_link=LINK,
                provider_offer_id=f"bm:{m.get('dataCenterId')}:{m.get('id')}",
                fetched_at=fetched_at,
            ))
        except (ValueError, TypeError):
            continue
    return offers

def fetch_offers():
    """-> (offers, http_status, note, complete)"""
    fetched_at = int(time.time())
    vm, s1 = request_json("GET", f"{BASE}/v1/vms/machine-types")
    bm, s2 = request_json("GET", f"{BASE}/v1/machines-types",
                          params={"pageSize": 100})
    gm, s3 = request_json("GET", f"{BASE}/v1/vms/gpu-models")
    statuses = (s1, s2, s3)
    if all(s != 200 for s in statuses[:2]):
        return [], s1, f"vm HTTP {s1}, bare-metal HTTP {s2}", True
    # one leg down = a fragment; refuse the swap rather than halve the book
    complete = s1 == 200 and s2 == 200
    note = "" if complete else f"partial: vm HTTP {s1}, bare-metal HTTP {s2}"
    if s3 != 200:
        note = (note + "; gpu-models fetch failed, VRAM omitted").strip("; ")
        gm = None
    return parse(vm if s1 == 200 else None, bm if s2 == 200 else None, gm,
                 fetched_at), s1, note, complete
