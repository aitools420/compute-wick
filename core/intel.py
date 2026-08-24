"""Market intelligence over the tape we already record — no new collection.

reliability(): how dependable each provider's FEED has been for us (poll
success over provider_health) plus, where the provider reports it, average
machine reliability. Named honestly: feed_score is about our data pipeline
to them, not their hardware uptime.

price_position(): where today's best price sits inside its own trailing
distribution. Descriptive percentiles, not a forecast — the verdict says
what the last weeks looked like, never what tomorrow will do.

spread(): what the discount for interruptible capacity actually is, per
model, now and over time.
"""
import time

import db
from core import economics

def reliability(days: int = 30) -> dict:
    days = max(1, min(int(days), 365))
    since = int(time.time()) - days * 86400
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT provider, COUNT(*) polls,
                   SUM(status='ok') ok, SUM(status='low') low,
                   SUM(status='partial') partial, SUM(status='DOWN') down,
                   SUM(status='skipped') skipped
            FROM provider_health WHERE ts>=? GROUP BY provider ORDER BY provider""",
            (since,))]
        machine = {r["provider"]: r["rel"] for r in conn.execute(
            "SELECT provider, ROUND(AVG(reliability), 4) rel FROM offers_current"
            " WHERE reliability IS NOT NULL GROUP BY provider")}
    for r in rows:
        polls = r["polls"] or 1
        # ok full credit; low = feed up but thin; partial = old book still
        # served; skipped = backing off a 429. Weights are judgment, published.
        r["feed_score"] = round(100 * (
            (r["ok"] or 0) + 0.7 * (r["low"] or 0) + 0.3 * (r["partial"] or 0)
            + 0.2 * (r["skipped"] or 0)) / polls, 1)
        r["ok_share"] = round((r["ok"] or 0) / polls, 4)
        if r["provider"] in machine:
            r["machine_reliability_avg"] = machine[r["provider"]]
    return {"generated_at": int(time.time()), "days": days,
            "basis": ("feed_score measures OUR polling success against the "
                      "provider's API (ok=1, low=0.7, partial=0.3, "
                      "skipped=0.2, DOWN=0) — the dependability of this "
                      "market data, not of the provider's machines. "
                      "machine_reliability_avg is the provider's own per-host "
                      "figure where one exists (vast only today)."),
            "providers": rows}

def _mins_by_snapshot(gpu_model: str, cls: str, since: int) -> list[tuple]:
    with db.get_db() as conn:
        return [(r["ts"], r["p"]) for r in conn.execute(
            "SELECT snapshot_ts ts, MIN(min_price_per_gpu_hr) p"
            " FROM offer_history WHERE gpu_model=? AND class=? AND snapshot_ts>=?"
            " GROUP BY snapshot_ts ORDER BY ts", (gpu_model, cls, since))]

def _percentile_of(value: float, population: list[float]):
    if not population:
        return None
    return round(100 * sum(1 for p in population if p <= value) / len(population), 1)

def price_position(gpu_model: str, offer_class: str = "on_demand") -> dict:
    now = int(time.time())
    points = _mins_by_snapshot(gpu_model, offer_class, now - 30 * 86400)
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT MIN(price_per_gpu_hr) p FROM offers_current"
            " WHERE gpu_model=? AND class=?", (gpu_model, offer_class)).fetchone()
    current = row["p"] if row else None
    if current is None:
        return {"error": f"no live {offer_class} offers for {gpu_model!r} "
                "(exact model name from market_stats)"}
    if len(points) < 10:
        return {"error": "not enough tape yet for this model/class — "
                f"{len(points)} snapshots recorded, need 10"}
    week = [p for ts, p in points if ts >= now - 7 * 86400]
    month = [p for _, p in points]
    day_ago = [p for ts, p in points if ts <= now - 86400]
    trend_24h = None
    if day_ago:
        base = day_ago[-1]
        if base:
            trend_24h = round(100 * (current - base) / base, 2)
    pct7 = _percentile_of(current, week)
    pct30 = _percentile_of(current, month)
    anchor = pct7 if pct7 is not None else pct30
    if anchor <= 25:
        verdict = "low — near the bottom of its recent range"
    elif anchor >= 75:
        verdict = "high — the recent range has usually been cheaper"
    else:
        verdict = "typical for its recent range"
    return {
        "generated_at": now, "gpu_model": gpu_model, "offer_class": offer_class,
        "current_best_per_gpu_hr": economics.apply_price(current),
        "percentile_vs_7d": pct7, "percentile_vs_30d": pct30,
        "trend_24h_pct": trend_24h,
        "range_7d": [economics.apply_price(min(week)),
                     economics.apply_price(max(week))] if week else None,
        "range_30d": [economics.apply_price(min(month)),
                      economics.apply_price(max(month))],
        "snapshots": len(points),
        "verdict": verdict,
        "basis": ("descriptive: where the current best price sits inside its "
                  "own trailing distribution. Not a forecast."),
    }

def spread(gpu_model: str = "", hours: int = 168) -> dict:
    """On-demand vs interruptible: the live discount per model, and (for one
    model) how it has moved."""
    now = int(time.time())
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT gpu_model,
                   MIN(CASE WHEN class='on_demand' THEN price_per_gpu_hr END) od,
                   MIN(CASE WHEN class='interruptible' THEN price_per_gpu_hr END) spot,
                   COUNT(*) offers
            FROM offers_current GROUP BY gpu_model
            HAVING od IS NOT NULL AND spot IS NOT NULL
            ORDER BY offers DESC LIMIT 40""")]
    table = []
    for r in rows:
        od, spot = economics.apply_price(r["od"]), economics.apply_price(r["spot"])
        table.append({"gpu_model": r["gpu_model"],
                      "on_demand_min": od, "interruptible_min": spot,
                      "discount_pct": round(100 * (od - spot) / od, 1) if od else None})
    out = {"generated_at": now,
           "basis": ("best live on-demand vs best live interruptible price per "
                     "model, market-wide; discount_pct is what choosing idle "
                     "capacity saves right now"),
           "models": table}
    if gpu_model:
        hours = max(1, min(int(hours), 24 * 90))
        since = now - hours * 3600
        bucket = 1 if hours <= 72 else 3600 if hours <= 168 else 21600
        series = {}
        for cls in ("on_demand", "interruptible"):
            for ts, p in _mins_by_snapshot(gpu_model, cls, since):
                b = (ts // bucket) * bucket if bucket > 1 else ts
                slot = series.setdefault(b, {})
                slot[cls] = min(slot.get(cls, p), p)
        points = []
        for ts in sorted(series):
            od, spot = series[ts].get("on_demand"), series[ts].get("interruptible")
            points.append([
                ts,
                economics.apply_price(od) if od else None,
                economics.apply_price(spot) if spot else None,
                round(100 * (od - spot) / od, 1) if od and spot else None])
        out["history"] = {"gpu_model": gpu_model, "hours": hours,
                          "bucket_seconds": bucket,
                          "point_format": ["ts", "on_demand_min",
                                           "interruptible_min", "discount_pct"],
                          "points": points}
    return out
