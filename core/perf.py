"""Perf-per-dollar — the value index. Changes the question from "cheapest"
to "most compute per dollar".

Basis: FP16 tensor-core throughput, DENSE (no sparsity marketing), FP16
accumulate, from vendor spec sheets — a static hardware fact, not a measured
benchmark, and labelled as such on every surface. Models without a defensible
public figure (no tensor cores, or no published dense number) are simply
unrated rather than guessed: no invented figures, ever.

value = fp16_tflops / price_final_per_gpu_hr  (TFLOPS per dollar-hour)
"""
import time

import db
from core import economics

# vendor spec sheets, dense FP16 tensor TFLOPS (FP16 accumulate).
# consumer Ada = FP32 shader TFLOPS x4; consumer Ampere x4 at half FP32-acc
# rate — the FP16-acc figures below are the comparable ones.
FP16_DENSE_TFLOPS = {
    "B200": 2250, "B200 CC": 2250,
    "H200": 989, "H200 NVL": 989, "H200 SXM": 989, "GH200": 989,
    "H100": 756, "H100 SXM": 989, "H100 PCIE": 756, "H100 NVL": 989,
    "A100 SXM4": 312, "A100 PCIE": 312, "A100": 312, "A100 40GB": 312,
    "A100 80GB": 312, "A100 SXM": 312, "A100 SXM 40GB": 312, "A800 PCIE": 312,
    "MI300X": 1307,
    "L40S": 181, "L40": 90, "L4": 60,
    "A40": 75, "A10": 63,
    "TESLA V100": 112, "V100 16GB": 112, "V100 32GB": 112,
    "RTX 6000 ADA": 182, "RTX 6000ADA": 182,
    "RTX A6000": 77, "RTX A5000": 56, "RTX A4000": 38, "RTX A2000": 32,
    "RTX PRO 6000": 500, "RTX PRO 6000 WS": 500, "RTX PRO 6000 S": 470,
    "RTX 5090": 419, "RTX 5080": 225, "RTX 5070 TI": 176, "RTX 5070": 123,
    "RTX 5060 TI": 95, "RTX 5060": 77,
    "RTX 4090": 330, "RTX 4080S": 209, "RTX 4080": 195,
    "RTX 4070S TI": 176, "RTX 4070 TI": 160, "RTX 4070S": 142, "RTX 4070": 116,
    "RTX 3090 TI": 160, "RTX 3090": 142, "RTX 3080 TI": 137, "RTX 3080": 119,
    "RTX 3070 TI": 87, "RTX 3070": 81, "RTX 3060 TI": 65, "RTX 3060": 51,
    "RTX 2080 TI": 108, "RTX 8000": 130,
}

_BASIS = ("fp16_tflops is dense FP16 tensor throughput (FP16 accumulate) from "
          "vendor spec sheets — a hardware ceiling, not a measured benchmark. "
          "Models without a defensible public figure are unrated, not guessed.")

# vendor spec sheets, peak memory bandwidth in GB/s. The ceiling for
# memory-bound work (hashing, KV-cache-bound inference, HPC stencils).
# STRICT: variants whose listing name doesn't pin the memory config
# (bare "A100", "A100 PCIE", "A800 PCIE", "GH200", "H100 NVL") are left
# out — an ambiguous model stays unrated rather than guessed at.
# Spot-verified 2026-08-24 against vendor/press specs: RTX 5080 960,
# RTX 5070 TI 896, RTX 5060 TI 448, RTX PRO 6000 1792, MI300X 5325.
MEM_BW_GBS = {
    "B200": 8000, "B200 CC": 8000,
    "H200": 4800, "H200 NVL": 4800, "H200 SXM": 4800,
    "H100 SXM": 3350, "H100": 3350, "H100 PCIE": 2000,
    "A100 SXM4": 2039, "A100 80GB": 1935, "A100 40GB": 1555, "A100 SXM 40GB": 1555,
    "MI300X": 5325,
    "L40S": 864, "L40": 864, "L4": 300,
    "A40": 696, "A10": 600,
    "TESLA V100": 900, "V100 16GB": 900, "V100 32GB": 900,
    "RTX 6000 ADA": 960, "RTX 6000ADA": 960,
    "RTX A6000": 768, "RTX A5000": 768, "RTX A4000": 448, "RTX A2000": 288,
    "RTX PRO 6000": 1792, "RTX PRO 6000 WS": 1792, "RTX PRO 6000 S": 1792,
    "RTX 5090": 1792, "RTX 5080": 960, "RTX 5070 TI": 896, "RTX 5070": 672,
    "RTX 5060 TI": 448, "RTX 5060": 448,
    "RTX 4090": 1008, "RTX 4080S": 736, "RTX 4080": 717,
    "RTX 4070S TI": 672, "RTX 4070 TI": 504, "RTX 4070S": 504, "RTX 4070": 504,
    "RTX 3090 TI": 1008, "RTX 3090": 936, "RTX 3080 TI": 912, "RTX 3080": 760,
    "RTX 3070 TI": 608, "RTX 3070": 448, "RTX 3060 TI": 448, "RTX 3060": 360,
    "RTX 3060 LAPTOP": 336, "RTX 3070 LAPTOP": 448,
    "RTX 2080 TI": 616, "RTX 8000": 672,
    "TESLA T4": 320, "TESLA P40": 346, "TESLA P100": 732,
    "GTX 1080 TI": 484, "GTX 1080": 320, "GTX 1070": 256, "GTX 1060": 192,
}

# spec-sheet lookups tolerate spacing drift between providers ('RTX 6000ADA'
# vs 'RTX 6000 ADA') by comparing de-spaced names
_DESPACED = {k.replace(" ", ""): v for k, v in FP16_DENSE_TFLOPS.items()}

def tflops_for(model: str):
    return _DESPACED.get((model or "").replace(" ", ""))

def value_index(offer_class: str = "", min_vram_gb=None, limit: int = 25) -> dict:
    """Per-model value board: best live price vs spec throughput."""
    sql = ("SELECT gpu_model, class, MIN(price_per_gpu_hr) best_price,"
           " MAX(vram_gb) vram_gb, COUNT(*) offers"
           " FROM offers_current WHERE 1=1")
    args: list = []
    if offer_class:
        sql += " AND class=?"
        args.append(offer_class)
    if min_vram_gb:
        sql += " AND vram_gb>=?"
        args.append(float(min_vram_gb))
    sql += " GROUP BY gpu_model, class"
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args)]
    rated, unrated = [], set()
    for r in rows:
        tflops = tflops_for(r["gpu_model"])
        if tflops is None:
            unrated.add(r["gpu_model"])
            continue
        price = economics.apply_price(r["best_price"])
        rated.append({
            "gpu_model": r["gpu_model"], "offer_class": r["class"],
            "fp16_tflops": tflops, "vram_gb": r["vram_gb"],
            "best_price_per_gpu_hr": price, "offers": r["offers"],
            "tflops_per_usd_hr": round(tflops / price, 1) if price else None,
        })
    rated.sort(key=lambda r: -(r["tflops_per_usd_hr"] or 0))
    return {"generated_at": int(time.time()), "basis": _BASIS,
            "count": len(rated[:max(1, min(int(limit), 100))]),
            "unrated_models": sorted(unrated),
            "value": rated[:max(1, min(int(limit), 100))]}
