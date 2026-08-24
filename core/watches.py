"""The tripwire: accountless price watches. A watch trips when the best live
(fee-adjusted) price per GPU-hour at its filter crosses under its line, and
re-arms when the price climbs back over line * 1.02 — the hysteresis keeps a
boundary-hugging market from ringing every poll. Webhooks POST from this box,
so targets resolving to private or loopback space are refused at create AND at
send (DNS can change between the two), name resolution is time-bounded off the
event loop, and — critically — webhooks fire with NO database lock held.
"""
import concurrent.futures
import ipaddress
import json
import secrets
import socket
import time
from urllib.parse import urlparse

import httpx

import config
import db
from core import economics, rank

MAX_WATCHES = 1000
MAX_EVENTS = 20
WATCH_TTL_DAYS = 30   # accountless watches expire; the cap is a shared resource
REARM_FACTOR = 1.02
DNS_TIMEOUT = 3.0

# A tiny pool so a black-holed DNS name blocks a bounded worker, never a
# request thread (getaddrinfo itself takes no timeout).
_RESOLVER = concurrent.futures.ThreadPoolExecutor(max_workers=4,
                                                  thread_name_prefix="dns")

def _host_is_private(host: str) -> bool:
    try:
        fut = _RESOLVER.submit(socket.getaddrinfo, host, None)
        infos = fut.result(timeout=DNS_TIMEOUT)
    except (OSError, concurrent.futures.TimeoutError):
        return True   # unresolvable or too slow = refuse
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified
                # RFC 6598 shared address space (CGNAT) — not flagged by is_private
                or ip in ipaddress.ip_network("100.64.0.0/10")):
            return True
    return False

def validate_webhook(url: str) -> str | None:
    """-> error string, or None if acceptable."""
    if not isinstance(url, str):
        return "webhook_url must be a string"
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return "webhook_url must be http(s)"
    if not p.hostname:
        return "webhook_url has no host"
    if _host_is_private(p.hostname):
        return "webhook_url resolves to private address space"
    return None

def create(gpu, max_price, cls, webhook_url) -> dict:
    # strip control chars at the write so the RSS render can never be poisoned
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
    with db.get_db() as conn:
        n = conn.execute("SELECT COUNT(*) c FROM watches").fetchone()["c"]
        if n >= MAX_WATCHES:
            raise ValueError("watch capacity reached — try later")
        wid = secrets.token_hex(12)
        conn.execute(
            "INSERT INTO watches (id, created_at, gpu_model, offer_class,"
            " max_price_per_gpu_hr, webhook_url) VALUES (?,?,?,?,?,?)",
            (wid, int(time.time()), gpu, cls or None, float(max_price),
             webhook_url or None))
        conn.commit()
    return get(wid)

def get(wid: str) -> dict | None:
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM watches WHERE id=?", (wid,)).fetchone()
    if not row:
        return None
    w = dict(row)
    w["events"] = json.loads(w["events"])
    w["expires_at"] = w["created_at"] + WATCH_TTL_DAYS * 86400
    return w

def delete(wid: str) -> bool:
    with db.get_db() as conn:
        cur = conn.execute("DELETE FROM watches WHERE id=?", (wid,))
        conn.commit()
        return cur.rowcount > 0

def best_price(w: dict) -> dict | None:
    offers = rank.query(gpu=w["gpu_model"], cls=w["offer_class"] or None, limit=1)
    return economics.apply(offers[0]) if offers else None

def _post_webhook(url: str, payload: dict) -> str:
    p = urlparse(url)
    if _host_is_private(p.hostname or ""):
        return "refused: private address"
    try:
        r = httpx.post(url, json=payload, timeout=5.0, follow_redirects=False,
                       headers={"User-Agent": config.USER_AGENT})
        return f"HTTP {r.status_code}"
    except Exception as e:
        return f"failed: {type(e).__name__}"

def check_all() -> int:
    """One pass over every watch against the live table. Returns trips fired.

    Structured so NO webhook POST ever runs inside a write transaction: read the
    watches, compute transitions and fire webhooks with no lock held, then apply
    every state change in one short committed batch."""
    now = int(time.time())
    with db.get_db() as conn:
        conn.execute("DELETE FROM watches WHERE created_at < ?",
                     (now - WATCH_TTL_DAYS * 86400,))
        conn.commit()
        watches = [dict(r) for r in conn.execute("SELECT * FROM watches")]

    updates = []   # (wid, price, new_state_or_None, new_event_or_None)
    fired = 0
    for w in watches:
        best = best_price(w)                       # read-only
        price = best["price_final_per_gpu_hr"] if best else None
        if price is None:
            updates.append((w["id"], None, None, None))
            continue
        line = w["max_price_per_gpu_hr"]
        if w["state"] == "armed" and price <= line:
            event = {"ts": now, "kind": "tripped", "price": price,
                     "offer": {k: best[k] for k in
                               ("provider", "gpu_model", "gpu_count", "class",
                                "price_final_per_gpu_hr", "link")}}
            if w["webhook_url"]:                    # fires with NO lock held
                event["webhook"] = _post_webhook(
                    w["webhook_url"],
                    {"watch_id": w["id"], "gpu": w["gpu_model"],
                     "line": line, "best": best, "ts": now})
            updates.append((w["id"], price, "tripped", event))
            fired += 1
        elif w["state"] == "tripped" and price > line * REARM_FACTOR:
            updates.append((w["id"], price, "armed",
                            {"ts": now, "kind": "re-armed", "price": price}))
        else:
            updates.append((w["id"], price, None, None))

    with db.get_db() as conn:
        conn.execute("BEGIN")
        for wid, price, new_state, event in updates:
            conn.execute("UPDATE watches SET last_checked=?, last_price=? WHERE id=?",
                         (now, price, wid))
            if new_state == "tripped":
                conn.execute("UPDATE watches SET state='tripped', tripped_at=? WHERE id=?",
                             (now, wid))
            elif new_state == "armed":
                conn.execute("UPDATE watches SET state='armed' WHERE id=?", (wid,))
            if event is not None:
                row = conn.execute("SELECT events FROM watches WHERE id=?",
                                   (wid,)).fetchone()
                events = json.loads(row["events"]) if row else []
                events.append(event)
                conn.execute("UPDATE watches SET events=? WHERE id=?",
                             (json.dumps(events[-MAX_EVENTS:]), wid))
        conn.commit()
    return fired
