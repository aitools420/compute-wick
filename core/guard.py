"""Budget guard — opt-in auto-destroy at a USD cap (auto_destroy_budget_usd).

The one sanctioned deviation from pass-through keys: an armed guard holds the
renter's key in PROCESS MEMORY ONLY — never disk, never DB, never logs (the
broker's record-factory scrubber covers these code paths too). A restart drops
every guard silently for us but LOUDLY for the renter: the receipt that armed
it says exactly that, and tells them to poll rental_status as the backstop.

Spend is estimated as max(provider-reported estimate, wall-clock x price_hr) —
conservative on purpose: the guard may destroy a machine slightly early; it
must never let one run long past the cap because a provider under-reported.
"""
import logging
import threading
import time

log = logging.getLogger("guard")

CHECK_SECONDS = 300

_lock = threading.Lock()
_guards: dict = {}   # (provider, instance_id) -> guard dict (holds the key)

def register(provider: str, instance_id: str, api_key: str, budget_usd: float,
             price_hr: float, label: str = ""):
    with _lock:
        _guards[(provider, instance_id)] = {
            "api_key": api_key,
            "budget_usd": float(budget_usd),
            "price_hr": float(price_hr or 0),
            "label": label,
            "registered_at": int(time.time()),
            "last_checked": None,
            "last_spend_est": 0.0,
            "errors": 0,
        }
    log.info("guard: armed %s/%s cap $%.2f", provider, instance_id, budget_usd)

def release(provider: str, instance_id: str):
    with _lock:
        _guards.pop((provider, instance_id), None)

def status(provider: str, instance_id: str):
    """Key-free view of one guard, for receipts and rental_status."""
    with _lock:
        g = _guards.get((provider, instance_id))
        if not g:
            return None
        return {"armed": True, "budget_usd": g["budget_usd"],
                "spend_est_usd": round(g["last_spend_est"], 4),
                "registered_at": g["registered_at"],
                "last_checked": g["last_checked"]}

def active_count() -> int:
    with _lock:
        return len(_guards)

def check_all() -> int:
    """One pass over every armed guard; returns how many rentals were
    destroyed. Called from the app's guard loop every CHECK_SECONDS."""
    from core import broker
    with _lock:
        snapshot = list(_guards.items())
    destroyed = 0
    now = int(time.time())
    for (provider, iid), g in snapshot:
        try:
            st = broker._status_raw(provider, iid, g["api_key"])
        except broker.BrokerError as e:
            g["errors"] += 1
            log.warning("guard: status check failed for %s/%s (%d): %s",
                        provider, iid, g["errors"], e)
            continue
        if st.get("state") == "gone":
            log.info("guard: %s/%s ended on its own — released", provider, iid)
            release(provider, iid)
            continue
        clock_spend = (now - g["registered_at"]) / 3600 * g["price_hr"]
        spend = max(float(st.get("spend_est_usd") or 0), clock_spend)
        with _lock:
            live = _guards.get((provider, iid))
            if live is None:
                continue                     # destroyed/released while we polled
            live["last_checked"] = now
            live["last_spend_est"] = spend
            live["errors"] = 0
        if spend < g["budget_usd"]:
            continue
        try:
            broker._destroy_raw(provider, iid, g["api_key"])
        except broker.BrokerError as e:
            # keep the guard armed; retry next cycle — a failed destroy with
            # the cap breached is the one state that must not be dropped
            log.error("guard: cap hit ($%.2f >= $%.2f) but destroy FAILED for "
                      "%s/%s: %s", spend, g["budget_usd"], provider, iid, e)
            continue
        release(provider, iid)
        destroyed += 1
        from core import meter
        meter.record_destroy(provider, iid, source="budget_guard")
        log.info("guard: destroyed %s/%s at $%.2f (cap $%.2f)",
                 provider, iid, spend, g["budget_usd"])
    return destroyed
