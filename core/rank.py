"""Filter + rank over offers_current. Ranking basis is price_per_gpu_hr
(the only number comparable across a 1x host offer and an 8x cluster row),
tie-broken by reliability."""
import time

import config
import db

# "EU" as a country filter means the 27 member states
EU_COUNTRIES = ("AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
                "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
                "PL", "PT", "RO", "SK", "SI", "ES", "SE")

def query(gpu=None, country=None, cls=None, max_price_per_gpu=None,
          min_vram=None, min_reliability=None, provider=None,
          min_gpu_count=None, region=None, per_model=False, limit=50) -> list[dict]:
    # per_model: one row per gpu_model — its cheapest live offer. Uses SQLite's
    # bare-column-with-MIN rule, so the whole row comes from the minimum-price offer.
    sql = ("SELECT *, MIN(price_per_gpu_hr) AS _pm FROM offers_current WHERE 1=1"
           if per_model else "SELECT * FROM offers_current WHERE 1=1")
    args: list = []
    if gpu:
        # escape LIKE wildcards so a literal % or _ in the query filters by that
        # character rather than matching the whole table
        needle = gpu.upper().replace("_", " ").strip()
        needle = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND gpu_model LIKE ? ESCAPE '\\'"
        args.append(f"%{needle}%")
    if country:
        # accepts one ISO-2 code, a comma list ("DE,NL,FR"), or "EU" (the 27)
        codes = [c.strip().upper() for c in country.split(",") if c.strip()]
        if codes == ["EU"]:
            codes = list(EU_COUNTRIES)
        if len(codes) == 1:
            sql += " AND country = ?"
            args.append(codes[0][:2])
        elif codes:
            sql += f" AND country IN ({','.join('?' * len(codes))})"
            args.extend(c[:2] for c in codes)
    if region:
        needle = region.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        sql += " AND region LIKE ? ESCAPE '\\'"
        args.append(f"%{needle}%")
    if min_gpu_count is not None:
        sql += " AND gpu_count >= ?"
        args.append(int(min_gpu_count))
    if cls:
        sql += " AND class = ?"
        args.append(cls)
    if provider:
        sql += " AND provider = ?"
        args.append(provider)
    if max_price_per_gpu is not None:
        sql += " AND price_per_gpu_hr <= ?"
        args.append(float(max_price_per_gpu))
    if min_vram is not None:
        sql += " AND vram_gb >= ?"
        args.append(float(min_vram))
    if min_reliability is not None:
        sql += " AND reliability >= ?"
        args.append(float(min_reliability))
    if per_model:
        sql += " GROUP BY gpu_model"
    sql += " ORDER BY price_per_gpu_hr ASC, reliability DESC NULLS LAST LIMIT ?"
    args.append(max(1, min(int(limit), 200)))   # floor too: a negative LIMIT is unbounded in SQLite
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    now = int(time.time())
    for r in rows:
        r.pop("_pm", None)
        r["stale"] = (now - r["fetched_at"]) > config.STALE_AFTER_SECONDS
    return rows
