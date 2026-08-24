"""Live, honest numbers for the site and API. The idle-capacity share is real:
interruptible/spot listings ARE idle hardware looking for work. No invented
carbon figures, ever."""
import time

import config
import db

def snapshot() -> dict:
    now = int(time.time())
    with db.get_db() as conn:
        total = conn.execute("SELECT COUNT(*) c FROM offers_current").fetchone()["c"]
        by_class = {r["class"]: r["c"] for r in conn.execute(
            "SELECT class, COUNT(*) c FROM offers_current GROUP BY class")}
        by_provider = {}
        for r in conn.execute(
                "SELECT provider, COUNT(*) c, MAX(fetched_at) newest "
                "FROM offers_current GROUP BY provider"):
            by_provider[r["provider"]] = {
                "offers": r["c"],
                "newest": r["newest"],
                "age_seconds": now - r["newest"],
                "stale": (now - r["newest"]) > config.STALE_AFTER_SECONDS,
            }
        cheapest = [dict(r) for r in conn.execute("""
            SELECT gpu_model, MIN(price_per_gpu_hr) min_price_per_gpu_hr,
                   COUNT(*) offers
            FROM offers_current GROUP BY gpu_model
            ORDER BY offers DESC LIMIT 12""")]
        gpu_models = conn.execute(
            "SELECT COUNT(DISTINCT gpu_model) c FROM offers_current").fetchone()["c"]
    # the fee seam moves this floor too, so FEE_BPS>0 can't leave a pre-fee
    # number here labelled as the market price beside a non-zero fee_bps
    from core import economics
    for c in cheapest:
        c["min_price_per_gpu_hr"] = economics.apply_price(c["min_price_per_gpu_hr"])
    idle = by_class.get("interruptible", 0)
    return {
        "generated_at": now,
        "total_offers": total,
        "gpu_models": gpu_models,
        "idle_offers": idle,
        "idle_share": round(idle / total, 4) if total else None,
        "by_class": by_class,
        "providers": by_provider,
        "cheapest_by_model": cheapest,
        "fee_bps": config.FEE_BPS,
        "health": db.latest_health(),
    }
