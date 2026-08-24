"""Compute limit orders — the keyholder-sidecar architecture (approved 2026-08-24).

An order is a standing intent: "when <gpu>/<class> trades at or under <line>,
I want it". We hold the ORDER, never the provider key. When the line is
crossed, check_all() cuts a FILL TICKET — the exact offer, the live price, a
short expiry, HMAC-signed with the order's shared secret — and delivers it by
webhook and/or long-poll. Whoever holds the key (the open-source sidecar, the
user's own agent, or a human with curl) presents the ticket + their key to the
existing broker rail, which re-quotes live before any money moves.

Custody story: the order secret authorizes THIS order's lifecycle only. It is
stored plaintext (like the tripwire, the id-is-the-key doctrine) because HMAC
needs the shared secret on both ends, and it can place nothing by itself — a
fill still requires the renter's own provider key at their end.
"""
import hashlib
import hmac
import json
import secrets
import time

import db
from core import economics, rank
from core.watches import validate_webhook, _post_webhook

MAX_ORDERS = 200
ORDER_TTL_DAYS = 30
TICKET_TTL_SECONDS = 240
MAX_EVENTS = 20
STATES = ("armed", "ticketed", "filled", "cancelled", "expired")


def _sign(secret: str, payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def create(gpu, max_price, cls=None, min_vram=0, min_gpu_count=0,
           webhook_url=None, auto_destroy_budget_usd=None) -> dict:
    gpu = "".join(c for c in str(gpu or "") if c.isprintable()).strip()[:40]
    if not gpu:
        raise ValueError("gpu is required")
    if not (isinstance(max_price, (int, float)) and not isinstance(max_price, bool)
            and 0 < max_price < 10000):
        raise ValueError("max_price_per_gpu_hr must be a positive number")
    if cls and cls not in ("on_demand", "interruptible", "reserved"):
        raise ValueError("bad offer_class")
    if webhook_url:
        err = validate_webhook(webhook_url)
        if err:
            raise ValueError(err)
    if auto_destroy_budget_usd is not None and not (
            isinstance(auto_destroy_budget_usd, (int, float))
            and 0 < auto_destroy_budget_usd <= 1000):
        raise ValueError("auto_destroy_budget_usd must be 0-1000")
    now = int(time.time())
    with db.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM orders WHERE state IN"
                         " ('armed','ticketed')").fetchone()["c"]
        if n >= MAX_ORDERS:
            raise ValueError("order capacity reached — try later")
        oid = secrets.token_hex(12)
        osecret = secrets.token_hex(24)
        conn.execute(
            "INSERT INTO orders (id, secret, created_at, expires_at, gpu_model,"
            " offer_class, max_price_per_gpu_hr, min_vram, min_gpu_count,"
            " webhook_url, auto_budget) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (oid, osecret, now, now + ORDER_TTL_DAYS * 86400, gpu, cls or None,
             float(max_price), float(min_vram or 0), int(min_gpu_count or 0),
             webhook_url or None, auto_destroy_budget_usd))
        conn.commit()
    o = get(oid)
    o["order_secret"] = osecret   # shown once, at create
    o["note"] = ("Keep order_secret: it authenticates ticket delivery and the"
                 " fill call for this order. It cannot rent anything by itself.")
    return o


def get(oid: str, secret: str = None) -> dict | None:
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not row:
        return None
    o = dict(row)
    ok = secret is not None and hmac.compare_digest(secret, o["secret"])
    o.pop("secret")
    o["events"] = json.loads(o["events"])
    o["ticket"] = json.loads(o["ticket_json"]) if (o["ticket_json"] and ok) else None
    if o["ticket_json"] and not ok:
        o["ticket"] = "present — pass order_secret to read it"
    o.pop("ticket_json")
    o["authenticated"] = ok
    return o


def _auth(oid: str, secret: str):
    with db.get_db() as conn:
        row = conn.execute("SELECT secret FROM orders WHERE id=?", (oid,)).fetchone()
    if not row or not hmac.compare_digest(str(secret or ""), row["secret"]):
        raise ValueError("unknown order or bad order_secret")


def cancel(oid: str, secret: str) -> dict:
    _auth(oid, secret)
    _transition(oid, "cancelled", {"kind": "cancelled"})
    return get(oid, secret)


def _transition(oid: str, state: str, event: dict = None):
    now = int(time.time())
    with db.get_db() as conn:
        if event is not None:
            row = conn.execute("SELECT events FROM orders WHERE id=?", (oid,)).fetchone()
            events = json.loads(row["events"]) if row else []
            events.append({"ts": now, **event})
            conn.execute("UPDATE orders SET events=? WHERE id=?",
                         (json.dumps(events[-MAX_EVENTS:]), oid))
        conn.execute("UPDATE orders SET state=? WHERE id=?", (state, oid))
        if state != "ticketed":
            conn.execute("UPDATE orders SET ticket_json=NULL, ticket_expires=NULL"
                         " WHERE id=?", (oid,))
        conn.commit()


def best_offer(o: dict) -> dict | None:
    offers = rank.query(gpu=o["gpu_model"], cls=o["offer_class"] or None,
                        min_vram=o["min_vram"] or None,
                        min_gpu_count=o["min_gpu_count"] or None, limit=1)
    return economics.apply(offers[0]) if offers else None


def mark_filled(oid: str, receipt_summary: dict):
    """Called by the rentals route after a confirmed (non-dry) fill succeeds."""
    _transition(oid, "filled", {"kind": "filled", "receipt": receipt_summary})


def take_ticket(oid: str, secret: str) -> dict | None:
    """Authenticated read of the live ticket (long-poll target)."""
    _auth(oid, secret)
    with db.get_db() as conn:
        row = conn.execute("SELECT ticket_json, ticket_expires, state FROM orders"
                           " WHERE id=?", (oid,)).fetchone()
    if not row or row["state"] != "ticketed" or not row["ticket_json"]:
        return None
    if row["ticket_expires"] and row["ticket_expires"] < time.time():
        return None
    return json.loads(row["ticket_json"])


def verify_ticket(oid: str, secret: str, offer_id: str) -> dict:
    """A fill call must name the offer its live ticket names. -> the ticket."""
    t = take_ticket(oid, secret)
    if not t:
        raise ValueError("order has no live ticket — wait for the trigger")
    if t["offer"]["id"] != offer_id:
        raise ValueError("offer does not match the live ticket")
    return t


def check_all() -> int:
    """One pass; cuts tickets for armed orders whose line is crossed. Webhooks
    fire with no lock held (same discipline as the tripwire)."""
    now = int(time.time())
    with db.get_db() as conn:
        conn.execute("UPDATE orders SET state='expired' WHERE expires_at < ?"
                     " AND state IN ('armed','ticketed')", (now,))
        conn.commit()
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE state IN ('armed','ticketed')")]

    cut = 0
    updates = []   # (oid, ticket_json|None, expires|None, event|None, state|None)
    for o in rows:
        if o["state"] == "ticketed":
            if o["ticket_expires"] and o["ticket_expires"] < now:
                updates.append((o["id"], None, None,
                                {"kind": "ticket-expired-rearmed"}, "armed"))
            continue
        best = best_offer(o)
        price = best["price_final_per_gpu_hr"] if best else None
        with db.get_db() as conn:
            conn.execute("UPDATE orders SET last_checked=?, last_price=? WHERE id=?",
                         (now, price, o["id"]))
            conn.commit()
        if price is None or price > o["max_price_per_gpu_hr"]:
            continue
        payload = {
            "order_id": o["id"], "issued_at": now,
            "expires_at": now + TICKET_TTL_SECONDS,
            "nonce": secrets.token_hex(8),
            "offer": {k: best[k] for k in
                      ("id", "provider", "gpu_model", "gpu_count", "class",
                       "price_final_per_gpu_hr") if k in best},
            "line": o["max_price_per_gpu_hr"],
            "auto_destroy_budget_usd": o["auto_budget"],
            "fill": {"endpoint": "POST /api/orders/{id}/fill",
                     "note": "present order_secret + your provider api_key;"
                             " price is re-quoted live before money moves"},
        }
        ticket = {**payload, "sig": _sign(o["secret"], payload)}
        event = {"kind": "ticket-cut", "price": price,
                 "offer": payload["offer"]}
        if o["webhook_url"]:
            event["webhook"] = _post_webhook(
                o["webhook_url"],
                {"order_id": o["id"], "ticket": ticket})
        updates.append((o["id"], json.dumps(ticket),
                        payload["expires_at"], event, "ticketed"))
        cut += 1

    with db.get_db() as conn:
        conn.execute("BEGIN")
        for oid, tjson, texp, event, state in updates:
            if event is not None:
                row = conn.execute("SELECT events FROM orders WHERE id=?",
                                   (oid,)).fetchone()
                events = json.loads(row["events"]) if row else []
                events.append({"ts": now, **event})
                conn.execute("UPDATE orders SET events=? WHERE id=?",
                             (json.dumps(events[-MAX_EVENTS:]), oid))
            conn.execute("UPDATE orders SET state=?, ticket_json=?,"
                         " ticket_expires=? WHERE id=?", (state, tjson, texp, oid))
        conn.commit()
    return cut
