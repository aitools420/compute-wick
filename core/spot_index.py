"""The compute spot index — one citable number for GPU spot pricing.

Methodology v1 (frozen; any change is an announced version bump):
- BASKET: ten fixed models spanning datacenter, workstation and consumer,
  chosen for multi-provider coverage (>=4 feeds each at inception).
- CLASS: on_demand only for the headline — firm prices, not bid churn. The
  same construction over interruptible is published as the idle sub-index,
  labeled volatile.
- PER MODEL, PER SNAPSHOT: per-provider MEDIAN prices ($/GPU/hr) — robust to
  any one provider's outlier listing.
- CHAINED: between adjacent snapshots, each model compares ONLY providers
  present in BOTH (median over the common set on each side); the snapshot
  ratio is the geometric mean of model ratios, and levels chain from 100.
  A new feed joining the panel therefore CANNOT move the index — only prices
  moving on feeds we already watched can. This is the difference between an
  index and a coverage counter.
- GAPS: a step with fewer than MIN_CHAIN chainable models carries the level
  flat and is marked; the payload counts such gaps.
- HONESTY: the index is young and the page says exactly how young.

Everything derives from offer_history — no new tables, no cron; ten-minute
in-process cache. VPS-portable by construction.
"""
import math
import statistics
import time

import db

VERSION = "1"
BASKET = ["H100 SXM", "H100 PCIE", "A100 PCIE", "A100 SXM4", "L40S",
          "RTX 6000 ADA", "RTX A6000", "RTX 4090", "RTX 5090", "RTX 3090"]
MIN_CHAIN = 5
CACHE_SECONDS = 600
_cache: dict = {"ts": 0, "payload": None}


def _series(cls: str) -> tuple[list[dict], int]:
    ph = ",".join("?" * len(BASKET))
    with db.get_db() as conn:
        rows = conn.execute(
            f"SELECT snapshot_ts, provider, gpu_model, median_price_per_gpu_hr"
            f" FROM offer_history WHERE class=? AND gpu_model IN ({ph})"
            f" AND median_price_per_gpu_hr > 0 ORDER BY snapshot_ts",
            [cls] + BASKET).fetchall()
    # 30-min bin -> model -> {provider: median} — partial passes merge into
    # complete panels at the tape's official cadence (latest value wins in-bin)
    snaps: dict = {}
    for ts, prov, model, med in rows:      # rows arrive ts-ascending
        snaps.setdefault(ts // 1800 * 1800, {}).setdefault(model, {})[prov] = med
    stamps = sorted(snaps)
    if not stamps:
        return [], 0
    out = [{"ts": stamps[0], "level": 100.0, "chained_models": None}]
    gaps = 0
    for prev_ts, ts in zip(stamps, stamps[1:]):
        a, b = snaps[prev_ts], snaps[ts]
        ratios = []
        for m in BASKET:
            common = set(a.get(m, {})) & set(b.get(m, {}))
            if not common:
                continue
            m0 = statistics.median(a[m][p] for p in common)
            m1 = statistics.median(b[m][p] for p in common)
            if m0 > 0 and m1 > 0:
                ratios.append(m1 / m0)
        level = out[-1]["level"]
        if len(ratios) >= MIN_CHAIN:
            step = math.exp(sum(math.log(r) for r in ratios) / len(ratios))
            level = level * step
        else:
            gaps += 1   # carried flat — the payload owns up to it
        out.append({"ts": ts, "level": round(level, 2),
                    "chained_models": len(ratios)})
    return out, gaps


def snapshot() -> dict:
    now = int(time.time())
    if _cache["payload"] and now - _cache["ts"] < CACHE_SECONDS:
        return _cache["payload"]
    head, head_gaps = _series("on_demand")
    idle, idle_gaps = _series("interruptible")
    payload = {
        "generated_at": now, "version": VERSION,
        "basket": BASKET, "min_chained_models": MIN_CHAIN,
        "methodology": ("chained index: between adjacent snapshots each basket"
                        " model compares only providers present in BOTH sides"
                        " (median over the common set); the step is the"
                        " geometric mean of model ratios and levels chain from"
                        " 100 at inception. A new feed joining cannot move the"
                        " index — only prices moving on already-watched feeds"
                        " can. Steps with fewer than min_chained_models"
                        " chainable models carry flat (counted as gaps)."
                        " Headline = on_demand. Basket changes only with an"
                        " announced version bump."),
        "on_demand": {"series": head, "gaps": head_gaps,
                      "level": head[-1]["level"] if head else None,
                      "base_ts": head[0]["ts"] if head else None},
        "idle_sub_index": {"series": idle, "gaps": idle_gaps,
                           "level": idle[-1]["level"] if idle else None,
                           "base_ts": idle[0]["ts"] if idle else None,
                           "note": "interruptible bid-market floors — volatile"
                                   " by nature, published for context"},
    }
    _cache.update(ts=now, payload=payload)
    return payload
