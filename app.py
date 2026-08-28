"""compute.pangle.online — one process: poll scheduler + JSON API + MCP + static site.
Run: venv/bin/uvicorn app:app --port $PORT   (or python app.py)"""
import asyncio
import contextlib
import logging
import threading
import time
from collections import Counter

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles

import config
import db
import poller
from core import broker, economics, guard, intel, meter, orders, perf, rank, spot_index, stats, watches, wire
from mcp_app import build_asgi_app, mcp

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("app")

mcp_asgi = build_asgi_app()

# In-process traffic tally, flushed to SQLite each poll cycle. The ask was
# "do we get any traffic at all" — day x kind counts answer that without
# logging visitors.
_traffic: Counter = Counter()

def _traffic_kind(path: str):
    if path.startswith("/mcp"):
        return "mcp"
    if path.startswith("/api/watches"):
        return "tripwire"
    if path.startswith("/api/orders"):
        return "orders"
    if path == "/api/traffic":
        return None
    if path.startswith("/api/"):
        return "api"
    if path == "/llms.txt":
        return "llms.txt"
    if path == "/" or path.endswith("/") or path.endswith(".html"):
        return "page"
    return None

# Per-IP watch-creation limit: one client can't exhaust the global watch table
# or hammer the DNS resolver behind the SSRF guard.
_watch_rl_lock = threading.Lock()
_watch_rl: dict = {}
WATCH_RL_MAX = 30
WATCH_RL_WINDOW = 3600

def _watch_rate_ok(ip: str) -> bool:
    now = time.time()
    with _watch_rl_lock:
        hits = [t for t in _watch_rl.get(ip, []) if now - t < WATCH_RL_WINDOW]
        if len(hits) >= WATCH_RL_MAX:
            _watch_rl[ip] = hits
            return False
        hits.append(now)
        _watch_rl[ip] = hits
        if len(_watch_rl) > 10000:      # opportunistic cleanup of idle buckets
            for k in [k for k, v in list(_watch_rl.items())
                      if all(now - t >= WATCH_RL_WINDOW for t in v)]:
                _watch_rl.pop(k, None)
        return True

async def _guard_loop():
    # budget guards poll every 5 minutes — a 30-min cadence against a USD cap
    # would overshoot small caps by most of an hour's billing
    while True:
        try:
            n = await asyncio.to_thread(guard.check_all)
            if n:
                log.info("guard: destroyed %d rental(s) at budget cap", n)
        except Exception:
            log.exception("guard pass failed")
        await asyncio.sleep(guard.CHECK_SECONDS)

async def _schedule():
    # Tiered cadence: every FAST_POLL_SECONDS the keyed money feeds refresh the
    # live book (and watches/orders run); every POLL_SECONDS a full pass covers
    # all providers and records the history tape.
    last_full = 0.0
    while True:
        try:
            now = time.time()
            if now - last_full >= config.POLL_SECONDS - 5:
                results = await asyncio.to_thread(poller.poll_all)
                last_full = now
            else:
                results = await asyncio.to_thread(
                    poller.poll_all, config.FAST_PROVIDERS, False)
            log.info("poll: %s", results)
            fired = await asyncio.to_thread(watches.check_all)
            if fired:
                log.info("tripwire: %d watch(es) fired", fired)
            cut = await asyncio.to_thread(orders.check_all)
            if cut:
                log.info("orders: %d fill ticket(s) cut", cut)
            snap = dict(_traffic)
            _traffic.clear()
            await asyncio.to_thread(db.add_traffic, snap)
        except Exception:
            log.exception("poll pass failed")
        await asyncio.sleep(min(config.FAST_POLL_SECONDS, config.POLL_SECONDS))

@contextlib.asynccontextmanager
async def lifespan(_app):
    db.init_db()
    task = asyncio.create_task(_schedule())
    guard_task = asyncio.create_task(_guard_loop())
    # MCP session manager must run inside the host app's lifespan (mounted
    # sub-apps never get their own lifespan).
    async with mcp.session_manager.run():
        yield
    task.cancel()
    guard_task.cancel()

app = FastAPI(title="compute.pangle.online", lifespan=lifespan, docs_url=None,
              redoc_url=None, openapi_url="/api/openapi.json")

@app.middleware("http")
async def count_traffic(request, call_next):
    response = await call_next(request)
    kind = _traffic_kind(request.url.path)
    if kind and response.status_code < 500:
        _traffic[(time.strftime("%Y-%m-%d", time.gmtime()), kind)] += 1
    return response

@app.get("/api/offers")
def api_offers(gpu: str = "", country: str = "", offer_class: str = "",
               provider: str = "", max_price: float = 0, min_vram: float = 0,
               min_reliability: float = 0, min_gpu_count: int = 0,
               region: str = "", per_model: int = 0, limit: int = 50):
    offers = economics.apply_all(rank.query(
        gpu=gpu or None, country=country or None, cls=offer_class or None,
        provider=provider or None, max_price_per_gpu=max_price or None,
        min_vram=min_vram or None, min_reliability=min_reliability or None,
        min_gpu_count=min_gpu_count or None, region=region or None,
        per_model=bool(per_model), limit=limit))
    return {"generated_at": int(time.time()), "count": len(offers),
            "fee_bps": config.FEE_BPS, "offers": offers}

@app.get("/api/specs")
def api_specs():
    """The spec tables behind the workload lens: dense FP16 tensor TFLOPS and
    peak memory bandwidth per model. Spec-sheet ceilings, not benchmarks;
    absent model = unrated (ambiguous variants deliberately so)."""
    return {"basis": perf._BASIS, "fp16_dense_tflops": perf.FP16_DENSE_TFLOPS,
            "mem_bw_gbs": perf.MEM_BW_GBS}

@app.get("/api/sparks")
def api_sparks(models: str = "", hours: int = 24, offer_class: str = ""):
    """Bulk mini-tape: market-wide best price per snapshot for each named model —
    feeds the per-row sparklines without one /api/history call per row."""
    names = [m.strip() for m in models.split(",") if m.strip()][:40]
    since = int(time.time()) - min(max(hours, 1), 168) * 3600
    sql = ("SELECT snapshot_ts, MIN(min_price_per_gpu_hr) FROM offer_history"
           " WHERE gpu_model=? AND snapshot_ts>=?")
    out = {}
    with db.get_db() as conn:
        for m in names:
            args = [m, since]
            q = sql
            if offer_class:
                q += " AND class=?"
                args.append(offer_class)
            rows = conn.execute(q + " GROUP BY snapshot_ts ORDER BY snapshot_ts",
                                args).fetchall()
            out[m] = [[r[0], round(r[1], 6)] for r in rows if r[1] is not None]
    return {"generated_at": int(time.time()), "hours": hours, "models": out}

@app.get("/api/stats")
def api_stats():
    return stats.snapshot()

@app.get("/api/gpus")
def api_gpus():
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute("""
            SELECT gpu_model, COUNT(*) offers, MIN(price_per_gpu_hr) min_price,
                   MAX(vram_gb) vram_gb,
                   SUM(CASE WHEN class='interruptible' THEN 1 ELSE 0 END) idle
            FROM offers_current GROUP BY gpu_model ORDER BY offers DESC""")]
    for r in rows:                      # min_price rides the fee seam like every price
        r["min_price"] = economics.apply_price(r["min_price"])
    return {"generated_at": int(time.time()), "fee_bps": config.FEE_BPS, "gpus": rows}

@app.get("/api/history")
def api_history(gpu: str, offer_class: str = "on_demand", hours: int = 24):
    hours = max(1, min(int(hours), 24 * 90))
    # raw points up to 3 days; hourly to a week; 6-hourly beyond
    bucket = 1 if hours <= 72 else 3600 if hours <= 168 else 21600
    since = int(time.time()) - hours * 3600
    series = db.history_series(gpu, offer_class, since, bucket)
    for rows in series.values():
        for r in rows:
            r[1] = economics.apply_price(r[1])
            r[2] = economics.apply_price(r[2])
    return {"generated_at": int(time.time()), "gpu_model": gpu,
            "offer_class": offer_class, "hours": hours, "bucket_seconds": bucket,
            "fee_bps": config.FEE_BPS,
            "point_format": ["ts", "min_price_per_gpu_hr",
                            "median_price_per_gpu_hr", "offer_count"],
            "series": series}

@app.get("/api/idle-history")
def api_idle_history(hours: int = 24):
    hours = max(1, min(int(hours), 24 * 90))
    bucket = 300 if hours <= 72 else 3600 if hours <= 168 else 21600
    points = db.idle_history(int(time.time()) - hours * 3600, bucket)
    return {"generated_at": int(time.time()), "hours": hours,
            "bucket_seconds": bucket, "index_since": config.IDLE_INDEX_EPOCH,
            "point_format": ["ts", "idle_share", "idle_offers", "total_offers"],
            "points": points}

@app.post("/api/watches")
def api_watch_create(request: Request, payload: dict = Body(...)):
    ip = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "?")
    if not _watch_rate_ok(ip):
        raise HTTPException(429, "too many watches from your address; try later")
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")
    try:
        w = watches.create(
            gpu=payload.get("gpu", ""),
            max_price=payload.get("max_price_per_gpu_hr"),
            cls=payload.get("offer_class") or None,
            webhook_url=payload.get("webhook_url") or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"watch": w,
            "status_url": f"/api/watches/{w['id']}",
            "feed_url": f"/api/watches/{w['id']}/feed",
            "note": "Keep the id — it is the only key to this watch."}

@app.get("/api/watches/{wid}")
def api_watch_get(wid: str):
    w = watches.get(wid)
    if not w:
        raise HTTPException(404, "no such watch")
    w["best_now"] = watches.best_price(w)
    return w

@app.delete("/api/watches/{wid}")
def api_watch_delete(wid: str):
    if not watches.delete(wid):
        raise HTTPException(404, "no such watch")
    return {"deleted": wid}

@app.get("/api/watches/{wid}/feed")
def api_watch_feed(wid: str):
    w = watches.get(wid)
    if not w:
        raise HTTPException(404, "no such watch")
    from xml.sax.saxutils import escape
    title = escape(f"{w['gpu_model']} under ${w['max_price_per_gpu_hr']:g}/GPU/hr")
    items = []
    for ev in reversed(w["events"]):
        ts = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(ev["ts"]))
        if ev["kind"] == "tripped":
            o = ev.get("offer", {})
            desc = (f"{o.get('provider','?')} has {o.get('gpu_model','?')} at "
                    f"${ev['price']:g}/GPU/hr ({o.get('class','?')})")
            link = escape(o.get("link") or "https://compute.pangle.online/", {'"': "&quot;"})
        else:
            desc = f"price back over the line at ${ev['price']:g}/GPU/hr — watch re-armed"
            link = "https://compute.pangle.online/"
        items.append(
            f"<item><title>{escape(desc)}</title><link>{link}</link>"
            f"<pubDate>{ts}</pubDate>"
            f"<guid isPermaLink=\"false\">{wid}-{ev['ts']}-{ev['kind']}</guid></item>")
    xml = (f"<?xml version=\"1.0\" encoding=\"UTF-8\"?><rss version=\"2.0\"><channel>"
           f"<title>{title} — compute.pangle.online tripwire</title>"
           f"<link>https://compute.pangle.online/</link>"
           f"<description>Fires when the best live fee-adjusted price crosses the line.</description>"
           + "".join(items) + "</channel></rss>")
    return Response(content=xml, media_type="application/rss+xml")

@app.get("/api/traffic")
async def api_traffic():
    # snapshot+clear on the event loop, where the middleware also increments, so
    # the Counter is only ever touched from one thread (no lock, no lost counts)
    snap = dict(_traffic)
    _traffic.clear()
    await asyncio.to_thread(db.add_traffic, snap)
    return {"generated_at": int(time.time()), "days": db.traffic_days(30),
            "note": "aggregate request counts by UTC day and kind; no visitor data"}


# ---------- limit orders (the keyholder-sidecar rail) ----------

@app.post("/api/orders")
def api_order_create(request: Request, payload: dict = Body(...)):
    ip = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "?")
    if not _watch_rate_ok(ip):
        raise HTTPException(429, "too many orders from your address; try later")
    if not isinstance(payload, dict):
        raise HTTPException(400, "body must be a JSON object")
    try:
        o = orders.create(
            gpu=payload.get("gpu", ""),
            max_price=payload.get("max_price_per_gpu_hr"),
            cls=payload.get("offer_class") or None,
            min_vram=payload.get("min_vram_gb") or 0,
            min_gpu_count=payload.get("min_gpu_count") or 0,
            webhook_url=payload.get("webhook_url") or None,
            auto_destroy_budget_usd=payload.get("auto_destroy_budget_usd"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"order": o,
            "status_url": f"/api/orders/{o['id']}",
            "ticket_url": f"/api/orders/{o['id']}/ticket",
            "fill_url": f"/api/orders/{o['id']}/fill",
            "sidecar": "https://compute.pangle.online/agents/#sidecar",
            "cadence_note": ("the book refreshes every poll cycle; triggers are"
                             " checked on that cadence, not tick-by-tick")}

@app.get("/api/index")
def api_index():
    """The compute spot index — chained, methodology-published, free."""
    return spot_index.snapshot()

@app.get("/api/wire")
def api_wire():
    """Notable moves from the tape — content that publishes itself."""
    return wire.wire()

@app.get("/wire.rss")
def wire_rss():
    return Response(wire.rss_xml("https://compute.pangle.online"),
                    media_type="application/rss+xml")

@app.get("/api/receipts")
def api_receipts(limit: int = 100):
    """The station's own execution tape: usage events of the designated house
    account (renter-1), published. Public-safe fields only — no tokens, no
    keys; a broker that publishes its own fills."""
    now = int(time.time())
    if not config.RECEIPTS_ACCOUNT_HASH:
        return {"generated_at": now, "receipts": [],
                "note": "no house account designated yet"}
    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ts, kind, provider, instance_id, gpu_model, gpu_count,"
            " price_hr, hours, amount_usd, fee_bps, fee_usd FROM usage_events"
            " WHERE account_hash=? ORDER BY ts DESC LIMIT ?",
            (config.RECEIPTS_ACCOUNT_HASH, max(1, min(int(limit), 200))))]
    return {"generated_at": now,
            "note": ("the station's own rentals — every placement the broker"
                     " executes with house money, published as it happens"),
            "receipts": rows}

@app.get("/api/book")
def api_book():
    """The resting book in aggregate — open interest for the venue half.
    Aggregates only; individual orders are never enumerable."""
    now = int(time.time())
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT gpu_model) FROM orders"
            " WHERE state='armed' AND expires_at > ?", (now,)).fetchone()
    return {"generated_at": now, "open_orders": row[0], "models": row[1]}

@app.get("/api/orders/{oid}")
def api_order_get(oid: str):
    o = orders.get(oid)
    if not o:
        raise HTTPException(404, "no such order")
    return o

@app.delete("/api/orders/{oid}")
def api_order_cancel(oid: str, payload: dict = Body(...)):
    try:
        return orders.cancel(oid, str(payload.get("order_secret", "")))
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.post("/api/orders/{oid}/ticket")
async def api_order_ticket(oid: str, payload: dict = Body(...)):
    # POST, secret in body — secrets never ride GET/query strings
    secret = str(payload.get("order_secret", ""))
    wait = min(max(int(payload.get("wait", 25) or 0), 0), 55)
    deadline = time.time() + wait
    while True:
        try:
            t = await asyncio.to_thread(orders.take_ticket, oid, secret)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if t:
            return {"ticket": t}
        o = await asyncio.to_thread(orders.get, oid)
        if not o or o["state"] not in ("armed", "ticketed"):
            return {"ticket": None, "state": o["state"] if o else "gone"}
        if time.time() >= deadline:
            return {"ticket": None, "state": o["state"]}
        await asyncio.sleep(2.0)

@app.post("/api/orders/{oid}/fill")
def api_order_fill(oid: str, payload: dict = Body(...)):
    secret = str(payload.get("order_secret", ""))
    o = orders.get(oid, secret)
    if not o:
        raise HTTPException(404, "no such order")
    if not o["authenticated"]:
        raise HTTPException(400, "bad order_secret")
    try:
        ticket = orders.take_ticket(oid, secret)
        if not ticket:
            raise ValueError("order has no live ticket — wait for the trigger")
        receipt = broker.place_rental(
            offer_id=str(ticket["offer"]["id"]),
            api_key=str(payload.get("api_key", "")),
            max_price_per_gpu_hr=o["max_price_per_gpu_hr"],
            confirm=bool(payload.get("confirm", False)),
            dry_run=bool(payload.get("dry_run", True)),
            idempotency_key=f"order-{oid}",
            image=str(payload.get("image", "") or ""),
            disk_gb=payload.get("disk_gb") or 10,
            label=str(payload.get("label", "") or f"limit-order-{oid[:8]}"),
            auto_destroy_budget_usd=o["auto_budget"],
            account_token=str(payload.get("account_token", "") or ""))
    except broker.BrokerError as e:
        raise HTTPException(400, str(e))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"bad request: {e}")
    if not receipt.get("dry_run"):
        orders.mark_filled(oid, {
            "provider": receipt.get("provider"),
            "instance_id": receipt.get("provider_instance_id")
                           or receipt.get("instance_id"),
            "price_per_gpu_hr": receipt.get("price_per_gpu_hr")
                                or receipt.get("live_price_per_gpu_hr")})
    return {"receipt": receipt, "order_state": orders.get(oid)["state"]}

@app.post("/api/rentals")
def api_rental_create(payload: dict = Body(...)):
    try:
        return broker.place_rental(
            offer_id=str(payload.get("offer_id", "")),
            api_key=str(payload.get("api_key", "")),
            max_price_per_gpu_hr=payload.get("max_price_per_gpu_hr"),
            confirm=bool(payload.get("confirm", False)),
            dry_run=bool(payload.get("dry_run", True)),
            idempotency_key=str(payload.get("idempotency_key", "") or ""),
            image=str(payload.get("image", "") or ""),
            disk_gb=payload.get("disk_gb") or 10,
            label=str(payload.get("label", "") or ""),
            auto_destroy_budget_usd=payload.get("auto_destroy_budget_usd"),
            account_token=str(payload.get("account_token", "") or ""))
    except broker.BrokerError as e:
        raise HTTPException(400, str(e))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"bad request: {e}")

@app.delete("/api/rentals/{provider}/{instance_id}")
def api_rental_destroy(provider: str, instance_id: str, payload: dict = Body(...)):
    try:
        return broker.destroy_rental(provider=provider,
                                     provider_instance_id=instance_id,
                                     api_key=str(payload.get("api_key", "")))
    except broker.BrokerError as e:
        raise HTTPException(400, str(e))

@app.post("/api/rentals/{provider}/{instance_id}/status")
def api_rental_status(provider: str, instance_id: str, payload: dict = Body(...)):
    # POST, key in body — keys never ride GET/query strings
    try:
        return broker.rental_status(provider=provider,
                                    provider_instance_id=instance_id,
                                    api_key=str(payload.get("api_key", "")))
    except broker.BrokerError as e:
        raise HTTPException(400, str(e))

@app.post("/api/rentals/best")
def api_rental_best(payload: dict = Body(...)):
    try:
        return broker.place_best(
            gpu=str(payload.get("gpu", "") or ""),
            api_key=str(payload.get("api_key", "")),
            max_price_per_gpu_hr=payload.get("max_price_per_gpu_hr"),
            offer_class=str(payload.get("offer_class", "") or ""),
            provider=str(payload.get("provider", "") or ""),
            country=str(payload.get("country", "") or ""),
            min_vram_gb=payload.get("min_vram_gb"),
            min_gpu_count=payload.get("min_gpu_count"),
            confirm=bool(payload.get("confirm", False)),
            dry_run=bool(payload.get("dry_run", True)),
            idempotency_key=str(payload.get("idempotency_key", "") or ""),
            image=str(payload.get("image", "") or ""),
            disk_gb=payload.get("disk_gb") or 10,
            label=str(payload.get("label", "") or ""),
            auto_destroy_budget_usd=payload.get("auto_destroy_budget_usd"),
            account_token=str(payload.get("account_token", "") or ""))
    except broker.BrokerError as e:
        raise HTTPException(400, str(e))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"bad request: {e}")

@app.post("/api/account")
def api_account_create(request: Request, payload: dict = Body(default={})):
    ip = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "?")
    if not _watch_rate_ok(ip):
        raise HTTPException(429, "too many account creations from your address; try later")
    return meter.create_account(str((payload or {}).get("label", "") or ""))

@app.get("/api/account/usage")
def api_account_usage(request: Request, days: int = 90):
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    out = meter.usage(token, days=days)
    if out is None:
        raise HTTPException(401, "unknown or missing account token (Authorization: Bearer)")
    return out

@app.get("/api/value")
def api_value(offer_class: str = "", min_vram: float = 0, limit: int = 25):
    return perf.value_index(offer_class=offer_class or "",
                            min_vram_gb=min_vram or None, limit=limit)

@app.get("/api/reliability")
def api_reliability(days: int = 30):
    return intel.reliability(days=days)

@app.get("/api/timing")
def api_timing(gpu: str, offer_class: str = "on_demand"):
    return intel.price_position(gpu, offer_class)

@app.get("/api/spread")
def api_spread(gpu: str = "", hours: int = 168):
    return intel.spread(gpu_model=gpu or "", hours=hours)

@app.get("/api/health")
def api_health():
    now = int(time.time())
    providers = db.latest_health()
    for p in providers:
        p["age_seconds"] = now - p["ts"]
    # a wedged poller writes no new rows, so a time-blind check would report ok
    # forever — fold staleness in, and treat DOWN as not-ok
    fresh = all(p["age_seconds"] <= config.STALE_AFTER_SECONDS * 2 for p in providers)
    ok = bool(providers) and fresh and all(p["status"] != "DOWN" for p in providers)
    return {"ok": ok, "providers": providers, "active_guards": guard.active_count()}

app.mount("/mcp", mcp_asgi)
app.mount("/", StaticFiles(directory=config.BASE_DIR + "/web", html=True),
          name="web")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=config.PORT)
