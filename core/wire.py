"""The market wire — notable moves computed from our own tape, on demand.

No new tables, no cron, no external service: current bests come from
offers_current, the 24h-ago reference from offer_history (a ±90-minute band
around the lookback), and results cache in-process for ten minutes. Items are
only ever derived from recorded data — the wire cannot say anything the tape
cannot back.
"""
import hashlib
import time

import db

CACHE_SECONDS = 600
MIN_MOVE_PCT = 5.0
TOP_ITEMS = 12
_cache: dict = {"ts": 0, "payload": None}


def _items(hours: int = 24) -> tuple[list[dict], float]:
    now = int(time.time())
    ref = now - hours * 3600
    band = 5400  # ±90 min around the lookback still lands on real snapshots
    with db.get_db() as conn:
        # a tape younger than the lookback compares against its own start —
        # and the payload says how long the window really was
        oldest = conn.execute(
            "SELECT MIN(snapshot_ts) FROM offer_history").fetchone()[0]
        if oldest is not None and oldest > ref - band:
            ref = oldest + band
        actual_hours = round((now - ref) / 3600, 1)
        cur = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
            "SELECT gpu_model, class, MIN(price_per_gpu_hr), COUNT(*)"
            " FROM offers_current GROUP BY gpu_model, class")}
        prev = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT gpu_model, class, MIN(min_price_per_gpu_hr)"
            " FROM offer_history WHERE snapshot_ts BETWEEN ? AND ?"
            " GROUP BY gpu_model, class", (ref - band, ref + band))}
        floors = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT gpu_model, class, MIN(min_price_per_gpu_hr)"
            " FROM offer_history WHERE snapshot_ts < ?"
            " GROUP BY gpu_model, class", (now - 3600,))}

    items = []
    for key, (price, count) in cur.items():
        model, cls = key
        was = prev.get(key)
        # a book thinner than 3 listings whipsaws when one machine rents out —
        # that is churn, not a price move, and it stays off the wire
        if not (price and was) or count < 3:
            continue
        pct = (price - was) / was * 100
        floor = floors.get(key)
        new_floor = floor is not None and price < floor
        if abs(pct) < MIN_MOVE_PCT and not new_floor:
            continue
        kind = "idle" if cls == "interruptible" else "on-demand"
        if abs(pct) > 100:
            # an extreme "move" usually means the cheap listing was rented
            # away, not that anyone repriced — say the two prices, not a percent
            title = (f"{model} {kind}: cheapest live is ${price:.4f}/GPU/hr"
                     f" — was ${was:.4f} about {round(actual_hours):g}h ago")
        else:
            word = "fell" if pct < 0 else "rose"
            title = (f"{model} {kind} floor {word} {abs(pct):.0f}% to"
                     f" ${price:.4f}/GPU/hr")
        if new_floor:
            title += " — a new low on our tape"
        items.append({
            "gpu_model": model, "offer_class": cls,
            "best_now": round(price, 6), "best_then": round(was, 6),
            "change_pct": round(pct, 1), "new_tape_low": new_floor,
            "title": title,
            "id": hashlib.sha1(
                f"{model}|{cls}|{time.strftime('%Y-%m-%d', time.gmtime(now))}"
                f"|{round(pct)}".encode()).hexdigest()[:16],
        })
    items.sort(key=lambda i: (not i["new_tape_low"], -abs(i["change_pct"])))
    return items[:TOP_ITEMS], actual_hours


def wire(hours: int = 24) -> dict:
    now = int(time.time())
    if _cache["payload"] and now - _cache["ts"] < CACHE_SECONDS:
        return _cache["payload"]
    items, actual_hours = _items(hours)
    payload = {
        "generated_at": now, "window_hours": actual_hours,
        "note": (f"notable moves in each model's best live price vs"
                 f" ~{actual_hours:g}h ago on our own tape; moves under 5%"
                 " don't make the wire"),
        "items": items,
    }
    _cache.update(ts=now, payload=payload)
    return payload


def _slug(m: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "-", m.lower()).strip("-")


def rss_xml(site: str) -> str:
    w = wire()
    now = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(w["generated_at"]))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0"><channel>',
           f"<title>the market wire — compute.pangle.online</title>",
           f"<link>{site}/wire/</link>",
           "<description>Notable moves in GPU spot prices, from the station's own tape."
           " Moves under 5% don't make the wire.</description>",
           f"<lastBuildDate>{now}</lastBuildDate>"]
    for i in w["items"]:
        link = f"{site}/gpu/{_slug(i['gpu_model'])}/"
        title = i["title"].replace("&", "&amp;").replace("<", "&lt;")
        out.append(
            f"<item><title>{title}</title><link>{link}</link>"
            f"<guid isPermaLink=\"false\">{i['id']}</guid>"
            f"<pubDate>{now}</pubDate></item>")
    out.append("</channel></rss>")
    return "\n".join(out)
