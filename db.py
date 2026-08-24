"""SQLite store. House style: stdlib sqlite3, WAL, Row factory, inline schema.

Tables:
  offers_current  — the live table, replaced per provider per poll (kept as-is on an empty/partial fetch)
  offer_history   — append-only AGGREGATES per (provider, gpu_model, class) per poll
                    (per-offer history at Vast scale would be ~1M rows/day; aggregates
                    keep Stage-1 charts possible at ~thousands/day)
  provider_health — one row per provider per poll: ok / low / DOWN, never silent
"""
import sqlite3
from contextlib import contextmanager
from sqlite3 import IntegrityError   # noqa: F401 — re-exported for broker idempotency

import config

@contextmanager
def get_db():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()

def init_db():
    with get_db() as conn:
        conn.executescript("""
    CREATE TABLE IF NOT EXISTS offers_current (
        id TEXT PRIMARY KEY,
        provider TEXT NOT NULL,
        gpu_model TEXT NOT NULL,
        gpu_count INTEGER NOT NULL,
        vram_gb REAL,
        price_hr REAL NOT NULL,
        price_per_gpu_hr REAL NOT NULL,
        region TEXT,
        country TEXT,
        class TEXT NOT NULL,
        availability TEXT,
        reliability REAL,
        provider_link TEXT,
        provider_offer_id TEXT,
        fetched_at INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_offers_gpu ON offers_current(gpu_model);
    CREATE INDEX IF NOT EXISTS idx_offers_provider ON offers_current(provider);

    CREATE TABLE IF NOT EXISTS offer_history (
        snapshot_ts INTEGER NOT NULL,
        provider TEXT NOT NULL,
        gpu_model TEXT NOT NULL,
        class TEXT NOT NULL,
        offer_count INTEGER NOT NULL,
        min_price_per_gpu_hr REAL,
        median_price_per_gpu_hr REAL
    );
    CREATE INDEX IF NOT EXISTS idx_history_ts ON offer_history(snapshot_ts);
    CREATE INDEX IF NOT EXISTS idx_history_series
        ON offer_history(gpu_model, class, snapshot_ts);

    CREATE TABLE IF NOT EXISTS watches (
        id TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL,
        gpu_model TEXT NOT NULL,
        offer_class TEXT,
        max_price_per_gpu_hr REAL NOT NULL,
        webhook_url TEXT,
        state TEXT NOT NULL DEFAULT 'armed',
        last_checked INTEGER,
        last_price REAL,
        tripped_at INTEGER,
        events TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS traffic (
        day TEXT NOT NULL,
        kind TEXT NOT NULL,
        count INTEGER NOT NULL,
        PRIMARY KEY (day, kind)
    );

    CREATE TABLE IF NOT EXISTS rentals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        idempotency_key TEXT UNIQUE,
        provider TEXT NOT NULL,
        provider_offer_id TEXT,
        instance_id TEXT,
        price_hr REAL,
        dry_run INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        label TEXT,
        receipt TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS orders (
        id TEXT PRIMARY KEY,
        secret TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        gpu_model TEXT NOT NULL,
        offer_class TEXT,
        max_price_per_gpu_hr REAL NOT NULL,
        min_vram REAL DEFAULT 0,
        min_gpu_count INTEGER DEFAULT 0,
        webhook_url TEXT,
        auto_budget REAL,
        state TEXT NOT NULL DEFAULT 'armed',
        last_checked INTEGER,
        last_price REAL,
        ticket_json TEXT,
        ticket_expires INTEGER,
        events TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE IF NOT EXISTS provider_health (
        ts INTEGER NOT NULL,
        provider TEXT NOT NULL,
        status TEXT NOT NULL,
        offer_count INTEGER NOT NULL,
        http_status INTEGER,
        note TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_health_ts ON provider_health(ts);

    CREATE TABLE IF NOT EXISTS accounts (
        token_hash TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL,
        last_used INTEGER,
        label TEXT
    );

    CREATE TABLE IF NOT EXISTS usage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_hash TEXT,
        ts INTEGER NOT NULL,
        kind TEXT NOT NULL,
        provider TEXT,
        instance_id TEXT,
        gpu_model TEXT,
        gpu_count INTEGER,
        price_hr REAL,
        hours REAL,
        amount_usd REAL,
        fee_bps INTEGER NOT NULL,
        fee_usd REAL
    );
    CREATE INDEX IF NOT EXISTS idx_usage_account ON usage_events(account_hash, ts);
    """)
        # pre-metering databases lack the attribution column on rentals
        cols = [r[1] for r in conn.execute("PRAGMA table_info(rentals)")]
        if "account_hash" not in cols:
            conn.execute("ALTER TABLE rentals ADD COLUMN account_hash TEXT")
        conn.commit()

def replace_provider_offers(provider: str, offers: list[dict], snapshot_ts: int = None,
                            record_history: bool = True):
    """Swap in this provider's fresh offers atomically; append history aggregates.
    snapshot_ts (one stamp per poll pass) buckets the history rows so every
    provider in a pass lands together; offers keep their own fetched_at.
    record_history=False (fast tier) refreshes the live book without touching
    the tape, so history density stays one row-set per POLL_SECONDS."""
    if not offers:
        return
    ts = snapshot_ts if snapshot_ts is not None else offers[0]["fetched_at"]
    cols = ("id", "provider", "gpu_model", "gpu_count", "vram_gb", "price_hr",
            "price_per_gpu_hr", "region", "country", "class", "availability",
            "reliability", "provider_link", "provider_offer_id", "fetched_at")
    rows = [tuple(o.get(c) for c in cols) for o in offers]
    groups: dict[tuple, list[float]] = {}
    for o in offers:
        groups.setdefault((o["gpu_model"], o["class"]), []).append(o["price_per_gpu_hr"])
    with get_db() as conn:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM offers_current WHERE provider=?", (provider,))
        conn.executemany(
            f"INSERT OR REPLACE INTO offers_current ({','.join(cols)}) VALUES ({','.join('?'*len(cols))})",
            rows)
        if record_history:
            for (model, cls), prices in groups.items():
                prices.sort()
                conn.execute(
                    "INSERT INTO offer_history VALUES (?,?,?,?,?,?,?)",
                    (ts, provider, model, cls, len(prices), prices[0],
                     prices[len(prices)//2]))
        conn.commit()

def history_series(gpu_model: str, cls: str, since: int, bucket: int = 1) -> dict:
    """Per-provider time series for one (model, class). bucket=1 returns raw
    per-poll snapshots; larger buckets floor timestamps and take MIN of mins /
    AVG of medians inside each bucket."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT (snapshot_ts / ?) * ? AS ts, provider,
                   MIN(min_price_per_gpu_hr) AS min_price,
                   AVG(median_price_per_gpu_hr) AS median_price,
                   MAX(offer_count) AS offers
            FROM offer_history
            WHERE gpu_model = ? AND class = ? AND snapshot_ts >= ?
            GROUP BY ts, provider ORDER BY ts""",
            (bucket, bucket, gpu_model, cls, since)).fetchall()
    series: dict[str, list] = {}
    for r in rows:
        series.setdefault(r["provider"], []).append(
            [r["ts"], r["min_price"], round(r["median_price"], 6), r["offers"]])
    return series

def idle_history(since: int, bucket: int = 300) -> list[list]:
    """Market-wide idle share over time from the history aggregates. Buckets
    floor to >=300s; with a shared per-pass snapshot_ts every provider lands together."""
    bucket = max(bucket, 300)
    since = max(since, config.IDLE_INDEX_EPOCH)   # pre-key data undercounts idle
    with get_db() as conn:
        # latest snapshot per provider inside each bucket — a bucket holding two
        # polls of one provider (restart + scheduler) must not double its counts
        rows = conn.execute("""
            WITH b AS (
                SELECT (snapshot_ts / :bk) * :bk AS ts, provider,
                       MAX(snapshot_ts) AS mts
                FROM offer_history WHERE snapshot_ts >= :since
                GROUP BY ts, provider
            )
            SELECT b.ts,
                   SUM(CASE WHEN h.class='interruptible' THEN h.offer_count ELSE 0 END) idle,
                   SUM(h.offer_count) total
            FROM offer_history h
            JOIN b ON h.provider = b.provider AND h.snapshot_ts = b.mts
            GROUP BY b.ts ORDER BY b.ts""",
            {"bk": bucket, "since": since}).fetchall()
    return [[r["ts"], round(r["idle"] / r["total"], 4), r["idle"], r["total"]]
            for r in rows if r["total"]]

def add_traffic(counts: dict):
    """counts: {(day, kind): n} — accumulated in-process, flushed periodically."""
    if not counts:
        return
    with get_db() as conn:
        conn.executemany(
            "INSERT INTO traffic (day, kind, count) VALUES (?,?,?) "
            "ON CONFLICT(day, kind) DO UPDATE SET count = count + excluded.count",
            [(day, kind, n) for (day, kind), n in counts.items()])
        conn.commit()

def traffic_days(days: int = 30) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT day, kind, count FROM traffic ORDER BY day DESC, kind LIMIT ?",
            (days * 8,)).fetchall()
    return [dict(r) for r in rows]

def record_health(provider: str, ts: int, status: str, offer_count: int,
                  http_status=None, note: str = ""):
    with get_db() as conn:
        conn.execute("INSERT INTO provider_health VALUES (?,?,?,?,?,?)",
                     (ts, provider, status, offer_count, http_status, note))
        conn.commit()

def latest_health() -> list[dict]:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT h.* FROM provider_health h
            JOIN (SELECT provider, MAX(ts) mts FROM provider_health GROUP BY provider) m
              ON h.provider = m.provider AND h.ts = m.mts
        """).fetchall()
        return [dict(r) for r in rows]
