"""Agent path: MCP server over the same core the human UI uses. Read/search
tools are always on; the money-moving rent tools register only when
BROKER_ENABLED. Every price goes through core.economics (the fee choke
point) exactly like /api."""
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

import config
from core import economics, rank, stats

mcp = MCPServer(
    "compute-wick",
    instructions=(
        "Live GPU rental market data aggregated across providers (Vast.ai, RunPod, DataCrunch, Akash). "
        "Prices are USD per hour. price_final_per_gpu_hr is the amount the renter pays and the "
        "comparable unit across multi-GPU offers (it equals price_per_gpu_hr while the platform "
        "fee_bps is 0; when fee_bps>0, price_per_gpu_hr is the raw provider price and "
        "price_final_per_gpu_hr is what you pay). class='interruptible' rows are idle capacity "
        "(spot/bid) — cheapest and greenest. Offers marked stale=true come from a provider whose "
        "feed is behind; treat them as indicative only."
    ),
)

@mcp.tool()
def search_offers(gpu: str = "", country: str = "", offer_class: str = "",
                  provider: str = "", max_price_per_gpu_hr: float = 0,
                  min_vram_gb: float = 0, min_reliability: float = 0,
                  min_gpu_count: int = 0, region: str = "",
                  limit: int = 20) -> list[dict]:
    """Search live GPU offers. gpu matches the model name (e.g. '4090', 'H100').
    offer_class: on_demand | interruptible | reserved. country: ISO-2 (e.g. US),
    a comma list ('DE,NL'), or 'EU' for the 27 member states. min_gpu_count
    filters to multi-GPU configs (8 = full 8x nodes). region substring-matches
    the provider's region/datacenter label. Returns offers ranked
    cheapest-first by price_per_gpu_hr."""
    return economics.apply_all(rank.query(
        gpu=gpu or None,
        country=country or None,
        cls=offer_class or None,
        provider=provider or None,
        max_price_per_gpu=max_price_per_gpu_hr or None,
        min_vram=min_vram_gb or None,
        min_reliability=min_reliability or None,
        min_gpu_count=min_gpu_count or None,
        region=region or None,
        limit=limit,
    ))

@mcp.tool()
def get_offer(offer_id: str) -> dict:
    """Fetch one offer by its id (from search_offers)."""
    import time as _t

    import db
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM offers_current WHERE id=?",
                           (offer_id,)).fetchone()
    if not row:
        return {"error": "offer not found (offers churn as the market moves)"}
    offer = dict(row)
    # same staleness signal search_offers carries — an agent re-checking a held
    # id must be able to see the feed went behind
    offer["stale"] = (_t.time() - offer["fetched_at"]) > config.STALE_AFTER_SECONDS
    return economics.apply(offer)

@mcp.tool()
def market_stats() -> dict:
    """Live market snapshot: total offers, GPU models, idle-capacity share,
    per-provider freshness/health, cheapest price per model, current fee_bps."""
    return stats.snapshot()

@mcp.tool()
def price_history(gpu_model: str, offer_class: str = "on_demand",
                  hours: int = 168) -> dict:
    """Price history for one GPU model (exact name from search_offers/market_stats,
    e.g. 'RTX 4090'). offer_class: on_demand | interruptible. Returns per-provider
    series of [ts, min_price_per_gpu_hr, median_price_per_gpu_hr, offer_count];
    ranges past 3 days are bucketed (hourly, then 6-hourly past a week)."""
    import time as _t

    import db
    hours = max(1, min(int(hours), 24 * 90))
    bucket = 1 if hours <= 72 else 3600 if hours <= 168 else 21600
    series = db.history_series(gpu_model, offer_class,
                               int(_t.time()) - hours * 3600, bucket)
    for rows in series.values():
        for r in rows:
            r[1] = economics.apply_price(r[1])
            r[2] = economics.apply_price(r[2])
    return {"gpu_model": gpu_model, "offer_class": offer_class, "hours": hours,
            "bucket_seconds": bucket,
            "point_format": ["ts", "min_price_per_gpu_hr",
                            "median_price_per_gpu_hr", "offer_count"],
            "series": series}

@mcp.tool()
def idle_history(hours: int = 168) -> dict:
    """The idle-capacity index over time: share of listed GPU capacity that is
    interruptible (spot/bid) — idle hardware looking for work. Points are
    [ts, idle_share, idle_offers, total_offers]."""
    import time as _t

    import db
    hours = max(1, min(int(hours), 24 * 90))
    bucket = 300 if hours <= 72 else 3600 if hours <= 168 else 21600
    return {"hours": hours, "bucket_seconds": bucket,
            "point_format": ["ts", "idle_share", "idle_offers", "total_offers"],
            "points": db.idle_history(int(_t.time()) - hours * 3600, bucket)}

@mcp.tool()
def best_value(offer_class: str = "", min_vram_gb: float = 0,
               limit: int = 25) -> dict:
    """Perf-per-dollar board: for each GPU model, best live price vs its FP16
    tensor throughput (dense, vendor spec sheets) -> TFLOPS per dollar-hour.
    The answer to 'most compute for my budget' rather than 'cheapest card'.
    Models without a defensible public spec figure are listed as unrated."""
    from core import perf
    return perf.value_index(offer_class=offer_class or "",
                            min_vram_gb=min_vram_gb or None, limit=limit)

@mcp.tool()
def provider_reliability(days: int = 30) -> dict:
    """How dependable each provider's data feed has been for this station
    (poll success over the trailing window), plus the provider's own average
    machine reliability where reported. feed_score is about the DATA, not
    their hardware."""
    from core import intel
    return intel.reliability(days=days)

@mcp.tool()
def price_position(gpu_model: str, offer_class: str = "on_demand") -> dict:
    """Rent-now-or-wait context: where the current best price for one GPU
    model sits inside its own trailing 7/30-day range (percentile, 24h trend,
    verdict). Descriptive, not a forecast."""
    from core import intel
    return intel.price_position(gpu_model, offer_class)

@mcp.tool()
def spot_spread(gpu_model: str = "", hours: int = 168) -> dict:
    """On-demand vs interruptible spread: the live discount for idle capacity
    per model; pass gpu_model for its history too."""
    from core import intel
    return intel.spread(gpu_model=gpu_model or "", hours=hours)

@mcp.tool()
def register_account(label: str = "") -> dict:
    """Create a metering account: returns a bearer token (shown once, we keep
    only a hash). Pass it as account_token on rent calls to build a usage
    ledger you can read back with account_usage. Optional — renting works
    without one."""
    from core import meter
    return meter.create_account(label)

@mcp.tool()
def account_usage(account_token: str, days: int = 90) -> dict:
    """Your metered usage ledger: placements, destroys, estimated hours and
    USD, and the platform fee at the fee_bps live at event time (0 today)."""
    from core import meter
    out = meter.usage(account_token, days=days)
    return out if out is not None else {"error": "unknown account token"}

@mcp.tool()
def create_watch(gpu: str, max_price_per_gpu_hr: float,
                 offer_class: str = "", webhook_url: str = "") -> dict:
    """Create a price watch (tripwire): fires when the best live fee-adjusted
    price per GPU-hour matching gpu (and optional offer_class) drops to or
    under max_price_per_gpu_hr. Checked every poll (~30 min). Returns the watch
    with its id — the id is the only key; poll it with watch_status, or give a
    public webhook_url to be POSTed on each trip. Watches re-arm when the
    price climbs 2% back over the line."""
    from core import watches
    try:
        w = watches.create(gpu=gpu, max_price=max_price_per_gpu_hr,
                           cls=offer_class or None, webhook_url=webhook_url or None)
    except ValueError as e:
        return {"error": str(e)}
    w["feed_url"] = f"https://compute.wick.pics/api/watches/{w['id']}/feed"
    return w

@mcp.tool()
def watch_status(watch_id: str) -> dict:
    """Current state of a watch: armed/tripped, last price seen, recent
    events, and the best matching offer right now."""
    from core import watches
    w = watches.get(watch_id)
    if not w:
        return {"error": "no such watch"}
    w["best_now"] = watches.best_price(w)
    return w

@mcp.tool()
def delete_watch(watch_id: str) -> dict:
    """Delete a watch by id."""
    from core import watches
    return {"deleted": watches.delete(watch_id)}

@mcp.tool()
def create_limit_order(gpu: str, max_price_per_gpu_hr: float,
                       offer_class: str = "", min_vram_gb: float = 0,
                       min_gpu_count: int = 0, webhook_url: str = "",
                       auto_destroy_budget_usd: float = 0) -> dict:
    """Place a standing LIMIT ORDER: when the best live fee-adjusted price for
    gpu (and optional offer_class) trades at or under max_price_per_gpu_hr,
    the station cuts a signed FILL TICKET naming the exact offer. The station
    never holds your provider key — the ticket is executed by whoever does:
    the open-source keyholder sidecar (/agents/#sidecar), your own agent
    long-polling POST /api/orders/{id}/ticket, or a human with curl. Returns
    the order with order_secret SHOWN ONCE — it authenticates ticket reads and
    the fill call for this order only and can rent nothing by itself. Triggers
    are checked on the poll cadence (~30-min bars, not tick-by-tick); orders
    expire in 30 days; a ticket lasts ~4 minutes then the order re-arms."""
    from core import orders
    try:
        return orders.create(gpu=gpu, max_price=max_price_per_gpu_hr,
                             cls=offer_class or None, min_vram=min_vram_gb or 0,
                             min_gpu_count=min_gpu_count or 0,
                             webhook_url=webhook_url or None,
                             auto_destroy_budget_usd=auto_destroy_budget_usd or None)
    except ValueError as e:
        return {"error": str(e)}

@mcp.tool()
def limit_order_status(order_id: str, order_secret: str = "") -> dict:
    """State of a limit order (armed/ticketed/filled/cancelled/expired), last
    price seen, recent events. Pass order_secret to also read the live fill
    ticket when one is cut."""
    from core import orders
    o = orders.get(order_id, order_secret or None)
    return o if o else {"error": "no such order"}

@mcp.tool()
def cancel_limit_order(order_id: str, order_secret: str) -> dict:
    """Cancel a limit order. Requires the order_secret from create_limit_order."""
    from core import orders
    try:
        return orders.cancel(order_id, order_secret)
    except ValueError as e:
        return {"error": str(e)}

def fill_limit_order(order_id: str, order_secret: str, api_key: str,
                     confirm: bool = False, dry_run: bool = True,
                     image: str = "", disk_gb: float = 10,
                     account_token: str = "") -> dict:
    """Execute a limit order's live fill ticket on YOUR provider key (used for
    this one call, never stored). Only works while the order is ticketed; the
    offer is re-quoted live and refused above the order's line. Idempotent per
    order — a retry returns the first receipt rather than renting a second
    machine. The order's auto_destroy_budget_usd (if set) arms the budget
    guard exactly as rent_offer does."""
    from core import broker, orders
    o = orders.get(order_id, order_secret)
    if not o:
        return {"error": "no such order"}
    if not o["authenticated"]:
        return {"error": "bad order_secret"}
    try:
        ticket = orders.take_ticket(order_id, order_secret)
        if not ticket:
            return {"error": "order has no live ticket — wait for the trigger"}
        receipt = broker.place_rental(
            offer_id=str(ticket["offer"]["id"]), api_key=api_key,
            max_price_per_gpu_hr=o["max_price_per_gpu_hr"], confirm=confirm,
            dry_run=dry_run, idempotency_key=f"order-{order_id}",
            image=image, disk_gb=disk_gb, label=f"limit-order-{order_id[:8]}",
            auto_destroy_budget_usd=o["auto_budget"],
            account_token=account_token)
    except broker.BrokerError as e:
        return {"error": str(e)}
    except (TypeError, ValueError) as e:
        return {"error": f"bad request: {e}"}
    if not receipt.get("dry_run"):
        orders.mark_filled(order_id, {
            "provider": receipt.get("provider"),
            "instance_id": receipt.get("provider_instance_id")
                           or receipt.get("instance_id"),
            "price_per_gpu_hr": receipt.get("price_per_gpu_hr")
                                or receipt.get("live_price_per_gpu_hr")})
    return {"receipt": receipt}

def rent_offer(offer_id: str, api_key: str, max_price_per_gpu_hr: float,
               confirm: bool = False, dry_run: bool = True,
               idempotency_key: str = "", image: str = "",
               disk_gb: float = 10, label: str = "",
               auto_destroy_budget_usd: float = 0,
               account_token: str = "") -> dict:
    """Place a rental on YOUR provider account (BYO key — it is used for this
    one call and never stored). Default is a DRY RUN returning exactly what
    would be executed; a live placement needs dry_run=false AND confirm=true.
    The offer is re-quoted live first and refused if its price exceeds
    max_price_per_gpu_hr. Executes on vast and runpod offers. Pass an
    idempotency_key so retries return the first receipt instead of renting
    twice. auto_destroy_budget_usd arms the budget guard: this station then
    holds your key in PROCESS MEMORY ONLY and destroys the rental when
    estimated spend reaches the cap (a station restart drops the guard — the
    receipt says so; poll rental_status as backstop). account_token (from
    register_account) attributes the rental to your usage ledger."""
    from core import broker
    try:
        return broker.place_rental(
            offer_id=offer_id, api_key=api_key,
            max_price_per_gpu_hr=max_price_per_gpu_hr, confirm=confirm,
            dry_run=dry_run, idempotency_key=idempotency_key, image=image,
            disk_gb=disk_gb, label=label,
            auto_destroy_budget_usd=auto_destroy_budget_usd or None,
            account_token=account_token)
    except broker.BrokerError as e:
        return {"error": str(e)}

def rent_best(api_key: str, max_price_per_gpu_hr: float, gpu: str = "",
              offer_class: str = "", provider: str = "", country: str = "",
              min_vram_gb: float = 0, min_gpu_count: int = 0,
              confirm: bool = False, dry_run: bool = True,
              idempotency_key: str = "", image: str = "", disk_gb: float = 10,
              label: str = "", auto_destroy_budget_usd: float = 0,
              account_token: str = "") -> dict:
    """One-shot rent: search the live book with these filters and place on the
    best (cheapest) matching offer — 'cheapest H100 under $2/hr, go'. Same
    guarantees as rent_offer (dry-run default, live re-quote, your
    max_price_per_gpu_hr is absolute). If the best offer is already gone it
    walks to the next, at most 3, and reports what it skipped."""
    from core import broker
    try:
        return broker.place_best(
            gpu=gpu, api_key=api_key,
            max_price_per_gpu_hr=max_price_per_gpu_hr,
            offer_class=offer_class, provider=provider, country=country,
            min_vram_gb=min_vram_gb or None,
            min_gpu_count=min_gpu_count or None, confirm=confirm,
            dry_run=dry_run, idempotency_key=idempotency_key, image=image,
            disk_gb=disk_gb, label=label,
            auto_destroy_budget_usd=auto_destroy_budget_usd or None,
            account_token=account_token)
    except broker.BrokerError as e:
        return {"error": str(e)}

def rental_status(provider: str, provider_instance_id: str,
                  api_key: str) -> dict:
    """Live state of an instance on your account: running/starting/stopped/
    gone, uptime, price, estimated spend so far, and the budget guard's state
    if one is armed. Read-only; your key passes through and is never stored."""
    from core import broker
    try:
        return broker.rental_status(provider=provider,
                                    provider_instance_id=provider_instance_id,
                                    api_key=api_key)
    except broker.BrokerError as e:
        return {"error": str(e)}

def destroy_rental(provider: str, provider_instance_id: str, api_key: str) -> dict:
    """Destroy an instance previously placed on your account (vast or runpod).
    Your key passes through and is never stored."""
    from core import broker
    try:
        return broker.destroy_rental(provider=provider,
                                     provider_instance_id=provider_instance_id,
                                     api_key=api_key)
    except broker.BrokerError as e:
        return {"error": str(e)}

# The money-moving tools are advertised only when the broker is on, so a
# public endpoint never lists a credential-taking tool that just refuses.
if config.BROKER_ENABLED:
    mcp.tool()(rent_offer)
    mcp.tool()(rent_best)
    mcp.tool()(rental_status)
    mcp.tool()(destroy_rental)
    mcp.tool()(fill_limit_order)

def build_asgi_app():
    return mcp.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False),
    )
