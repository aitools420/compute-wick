"""Stage 3 groundwork — usage metering + account plumbing.

The fee switch (FEE_BPS) has always existed; this gives it something to bill.
Every broker placement/destroy lands in usage_events, attributed to an account
when the caller passed account_token, anonymous otherwise. Accounts are a
bearer token shown ONCE at creation; we store only its SHA-256 — a DB read
can name accounts but never impersonate one. fee_usd is computed at event
time from the live FEE_BPS (0 today), so the day fees turn on, the ledger
starts pricing itself with no code change.
"""
import hashlib
import secrets
import time

import config
import db

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def create_account(label: str = "") -> dict:
    token = "cwk_" + secrets.token_urlsafe(24)
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO accounts (token_hash, created_at, label) VALUES (?,?,?)",
            (_hash(token), int(time.time()), (label or "")[:80]))
        conn.commit()
    return {"account_token": token,
            "label": (label or "")[:80],
            "note": ("shown once — we store only a hash. Pass it as "
                     "account_token on rent calls to build your usage ledger; "
                     "read the ledger with it at GET /api/account/usage "
                     "(Authorization: Bearer) or the account_usage MCP tool.")}

def resolve(token: str):
    """Token -> account hash, or None. Touches last_used on a hit."""
    if not token:
        return None
    h = _hash(token)
    with db.get_db() as conn:
        row = conn.execute("SELECT token_hash FROM accounts WHERE token_hash=?",
                           (h,)).fetchone()
        if not row:
            return None
        conn.execute("UPDATE accounts SET last_used=? WHERE token_hash=?",
                     (int(time.time()), h))
        conn.commit()
    return h

def record(account_hash, kind: str, *, provider=None, instance_id=None,
           gpu_model=None, gpu_count=None, price_hr=None, hours=None,
           amount_usd=None):
    fee_bps = config.FEE_BPS
    fee_usd = round(amount_usd * fee_bps / 10000, 6) if amount_usd else 0.0
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO usage_events (account_hash, ts, kind, provider,"
            " instance_id, gpu_model, gpu_count, price_hr, hours, amount_usd,"
            " fee_bps, fee_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (account_hash, int(time.time()), kind, provider, instance_id,
             gpu_model, gpu_count, price_hr, hours, amount_usd, fee_bps, fee_usd))
        conn.commit()

def record_destroy(provider: str, instance_id: str, *, source: str):
    """Close out a rental in the ledger. Attribution and pricing come from the
    placement row; a destroy of an instance we never placed still logs, bare."""
    now = int(time.time())
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT created_at, price_hr, account_hash FROM rentals"
            " WHERE provider=? AND instance_id=? ORDER BY id DESC LIMIT 1",
            (provider, str(instance_id))).fetchone()
    hours = amount = account_hash = None
    if row:
        account_hash = row["account_hash"]
        hours = round((now - row["created_at"]) / 3600, 4)
        if row["price_hr"]:
            amount = round(row["price_hr"] * hours, 4)
    kind = "guard_destroyed" if source == "budget_guard" else "rental_destroyed"
    record(account_hash, kind, provider=provider, instance_id=str(instance_id),
           hours=hours, amount_usd=amount)

def usage(token: str, days: int = 90):
    """The caller's ledger, or None on a bad token."""
    h = resolve(token)
    if not h:
        return None
    days = max(1, min(int(days), 730))
    since = int(time.time()) - days * 86400
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ts, kind, provider, instance_id, gpu_model, gpu_count,"
            " price_hr, hours, amount_usd, fee_bps, fee_usd FROM usage_events"
            " WHERE account_hash=? AND ts>=? ORDER BY ts DESC LIMIT 500",
            (h, since))]
    totals = {
        "rentals_placed": sum(1 for r in rows if r["kind"] == "rental_placed"),
        "rentals_destroyed": sum(1 for r in rows if r["kind"].endswith("destroyed")),
        "billed_hours": round(sum(r["hours"] or 0 for r in rows), 4),
        "amount_usd": round(sum(r["amount_usd"] or 0 for r in rows), 4),
        "fee_usd": round(sum(r["fee_usd"] or 0 for r in rows), 6),
    }
    return {"days": days, "totals": totals, "events": rows,
            "note": ("amounts are price_hr x elapsed estimates; the provider's "
                     "own bill is authoritative. fee_usd prices the platform fee "
                     f"at the fee_bps live at event time (now {config.FEE_BPS}).")}

def platform_totals(days: int = 30) -> dict:
    """Anonymous platform-wide broker volume — the number the fee lever bills."""
    since = int(time.time()) - max(1, min(int(days), 730)) * 86400
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT SUM(CASE WHEN kind='rental_placed' THEN 1 ELSE 0 END) placed,"
            " SUM(CASE WHEN kind LIKE '%destroyed' THEN 1 ELSE 0 END) destroyed,"
            " SUM(hours) hours, SUM(amount_usd) amount, SUM(fee_usd) fees"
            " FROM usage_events WHERE ts>=?", (since,)).fetchone()
    return {"days": days,
            "rentals_placed": row["placed"] or 0,
            "rentals_destroyed": row["destroyed"] or 0,
            "billed_hours": round(row["hours"] or 0, 4),
            "amount_usd": round(row["amount"] or 0, 4),
            "fee_usd": round(row["fees"] or 0, 6)}
