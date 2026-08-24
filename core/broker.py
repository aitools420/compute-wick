"""Stage 2 broker — BYO-key rental execution. See BROKER-DESIGN.md.

Doctrine enforced here, not in prose: the caller's provider key passes through
one placement call and is never persisted; every log line is scrubbed of any
secret active in the request (redaction as code); dry-run is the default and
returns the exact provider payload that WOULD be sent; the offer is re-quoted
live with the caller's key before money moves, so our (up to 30-min-old) table
can never overspend anyone; an idempotency key makes retries return the first
receipt instead of a second machine. Places on Vast and RunPod, one instance
per call.

The ONE sanctioned deviation from pass-through: auto_destroy_budget_usd opts
into the budget guard (core/guard.py), which holds the key IN PROCESS MEMORY
ONLY until the cap trips or the rental ends. Never disk, never DB. A restart
drops the guard (the rental keeps running) — every receipt that arms a guard
says so.
"""
import contextvars
import json
import logging
import time

import config
import db
from core import economics
from providers.common import request_json

log = logging.getLogger("broker")

BROKERABLE = ("vast", "runpod")

def _lookup_receipt(idempotency_key: str):
    with db.get_db() as conn:
        row = conn.execute("SELECT receipt FROM rentals WHERE idempotency_key=?",
                           (idempotency_key,)).fetchone()
    return json.loads(row["receipt"]) if row else None

def _delete_reservation(idempotency_key: str):
    with db.get_db() as conn:
        conn.execute("DELETE FROM rentals WHERE idempotency_key=? AND instance_id IS NULL",
                     (idempotency_key,))
        conn.commit()

VAST_ASKS_URL = "https://console.vast.ai/api/v0/asks/{id}/"
VAST_BUNDLES_URL = "https://console.vast.ai/api/v0/bundles"
VAST_INSTANCE_URL = "https://console.vast.ai/api/v1/instances/{id}/"
VAST_INSTANCES_URL = "https://console.vast.ai/api/v1/instances/"

RUNPOD_GQL_URL = "https://api.runpod.io/graphql"
# stable non-rc, non-cluster tag verified on Docker Hub 2026-08-24
RUNPOD_DEFAULT_IMAGE = "runpod/pytorch:1.1.0-cu1281-torch280-ubuntu2404"
# our runpod offers are synthesized per (gpu class x cloud x kind); the price
# field to re-quote follows from the (cloud, class) pair baked into the id
RUNPOD_PRICE_FIELDS = {
    ("secure", "on_demand"): "securePrice",
    ("community", "on_demand"): "communityPrice",
    ("secure", "interruptible"): "secureSpotPrice",
    ("community", "interruptible"): "communitySpotPrice",
}

# Secrets active in the current request; the logging filter scrubs them from
# every record on every logger. Set via `with active_secret(key):`.
_ACTIVE_SECRETS: contextvars.ContextVar[tuple] = contextvars.ContextVar(
    "broker_secrets", default=())

# A logger .addFilter() only sees records logged directly on that logger —
# propagated records skip it. The record FACTORY runs for every record from
# every logger, so redaction lives there.
_prior_factory = logging.getLogRecordFactory()

def _scrub_text(text: str, secrets) -> str:
    for s in secrets:
        if s and s in text:
            text = text.replace(s, "[REDACTED]")
    return text

def _scrub_factory(*args, **kwargs):
    record = _prior_factory(*args, **kwargs)
    secrets = _ACTIVE_SECRETS.get()
    if secrets:
        msg = record.getMessage()
        scrubbed = _scrub_text(msg, secrets)
        if scrubbed != msg:
            record.msg, record.args = scrubbed, ()
        # a traceback is rendered from exc_info, not msg — scrub it too, then
        # hand the formatter our cleaned text so the raw exc never reaches a log
        if record.exc_info:
            import traceback
            rendered = "".join(traceback.format_exception(*record.exc_info))
            record.exc_text = _scrub_text(rendered, secrets)
            record.exc_info = None
    return record

logging.setLogRecordFactory(_scrub_factory)

class active_secret:
    def __init__(self, *secrets):
        self._secrets = tuple(s for s in secrets if s)

    def __enter__(self):
        self._token = _ACTIVE_SECRETS.set(self._secrets)
        return self

    def __exit__(self, *exc):
        _ACTIVE_SECRETS.reset(self._token)

class AmbiguousOutcome(Exception):
    """Raised when the placement left this box and no definite answer came
    back (timeout, transport failure, provider 5xx). The machine MAY exist."""

VERIFY_NOTE = ("outcome UNKNOWN — the provider may have placed the machine."
               " Check your provider console before retrying; a retry with the"
               " same idempotency_key replays this receipt instead of renting"
               " a second machine.")

def _mark_unknown(idempotency_key: str, offer: dict, why: str):
    if not idempotency_key:
        return
    receipt = {"status": "unknown", "provider": offer["provider"],
               "provider_offer_id": offer["provider_offer_id"],
               "ts": int(time.time()), "why": why, "note": VERIFY_NOTE}
    with db.get_db() as conn:
        conn.execute("UPDATE rentals SET receipt=? WHERE idempotency_key=?"
                     " AND instance_id IS NULL",
                     (json.dumps(receipt), idempotency_key))
        conn.commit()

class BrokerError(Exception):
    """User-facing refusal; the message is safe to return verbatim."""

def _require_enabled():
    if not config.BROKER_ENABLED:
        raise BrokerError("the broker is not enabled on this station")

def _validate(offer_id: str, api_key: str, max_price: float, confirm: bool,
              dry_run: bool, budget_usd=None):
    if not api_key or len(api_key) < 8:
        raise BrokerError("api_key is required (your own provider key; it is never stored)")
    if not (isinstance(max_price, (int, float)) and 0 < max_price < 10000):
        raise BrokerError("max_price_per_gpu_hr must be a positive number — it is the overspend guard")
    if budget_usd is not None and not (
            isinstance(budget_usd, (int, float)) and 0 < budget_usd < 100000):
        raise BrokerError("auto_destroy_budget_usd must be a positive number (total USD cap)")
    if not dry_run and not confirm:
        raise BrokerError("a live placement requires confirm=true — say the dangerous thing out loud")
    with db.get_db() as conn:
        row = conn.execute("SELECT * FROM offers_current WHERE id=?",
                           (offer_id,)).fetchone()
    if not row:
        raise BrokerError("no such offer id (offers churn; re-run search_offers)")
    offer = dict(row)
    if offer["provider"] not in BROKERABLE:
        raise BrokerError(
            f"the broker places on {'/'.join(BROKERABLE)} only; this offer is {offer['provider']}")
    return offer

# ---------------------------------------------------------------- vast

def _requote_vast(ask_id: str, api_key: str, cls: str) -> dict:
    """Fetch the single live ask with the CALLER's key. The query type must
    match the offer's class: re-quoting a bid ask as on-demand would price the
    bid at the full on-demand rate and overbid the caller's money."""
    # the searchable field for an ask's id is ask_contract_id — filtering on
    # "id" returns nothing, which reads as "offer gone" for every offer
    body = {"ask_contract_id": {"eq": int(ask_id)},
            "type": "bid" if cls == "interruptible" else "on-demand",
            "limit": 1, "rentable": {"eq": True}}
    payload, status = request_json(
        "POST", VAST_BUNDLES_URL, json_body=body,
        headers={"Authorization": f"Bearer {api_key}"})
    if status != 200 or payload is None:
        raise BrokerError(f"vast re-quote failed (HTTP {status}) — nothing was placed")
    rows = payload.get("offers") or []
    if not rows:
        raise BrokerError("that offer is gone (rented or delisted) — nothing was placed")
    return rows[0]

def _status_vast(instance_id: str, api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}
    payload, status = request_json(
        "GET", VAST_INSTANCE_URL.format(id=instance_id), headers=headers)
    row = None
    if status == 200 and isinstance(payload, dict):
        row = payload.get("instances")
        if isinstance(row, list):           # some deploys answer with a list here
            row = next((r for r in row if str(r.get("id")) == str(instance_id)), None)
    if row is None:
        # single-instance route unavailable or shape drifted: walk the list
        payload, status = request_json("GET", VAST_INSTANCES_URL, headers=headers)
        if status != 200 or not isinstance(payload, dict):
            raise BrokerError(f"vast status fetch failed (HTTP {status})")
        rows = payload.get("instances") or []
        row = next((r for r in rows if str(r.get("id")) == str(instance_id)), None)
    if not row:
        return {"state": "gone", "note": "no such instance on this account "
                "(destroyed, expired, or a different key)"}
    actual = (row.get("actual_status") or "").lower()
    state = {"running": "running", "loading": "starting", "created": "starting",
             "exited": "stopped", "stopped": "stopped"}.get(actual, actual or "unknown")
    start = row.get("start_date")
    uptime = max(0, int(time.time() - start)) if start else None
    price_hr = row.get("dph_total")
    return {
        "state": state,
        "uptime_seconds": uptime,
        "price_hr": price_hr,
        "raw": {k: row.get(k) for k in (
            "actual_status", "intended_status", "status_msg", "gpu_name",
            "num_gpus", "ssh_host", "ssh_port", "public_ipaddr", "start_date")},
    }

# ---------------------------------------------------------------- runpod

def _parse_runpod_offer_id(provider_offer_id: str):
    """Our synthesized runpod ids are '{gpuTypeId}:{cloud}:{class}'; rsplit so a
    colon ever appearing inside a gpuTypeId still parses."""
    try:
        gpu_type_id, cloud, cls = provider_offer_id.rsplit(":", 2)
    except ValueError:
        raise BrokerError("malformed runpod offer id — re-run search_offers")
    if (cloud, cls) not in RUNPOD_PRICE_FIELDS:
        raise BrokerError("malformed runpod offer id — re-run search_offers")
    return gpu_type_id, cloud, cls

def _gql(api_key: str, query: str, variables: dict = None):
    return request_json(
        "POST", RUNPOD_GQL_URL,
        json_body={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {api_key}"})

def _requote_runpod(provider_offer_id: str, api_key: str) -> dict:
    """RunPod prices are per GPU class, not per host — re-quote by re-reading
    the class's current price with the caller's key."""
    from providers import runpod as runpod_adapter
    gpu_type_id, cloud, cls = _parse_runpod_offer_id(provider_offer_id)
    payload, status = _gql(api_key, runpod_adapter.QUERY)
    if status != 200 or payload is None or payload.get("errors"):
        err = ((payload or {}).get("errors") or [{}])[0].get("message", "")
        raise BrokerError(
            f"runpod re-quote failed (HTTP {status} {err})".strip() + " — nothing was placed")
    for g in ((payload.get("data") or {}).get("gpuTypes")) or []:
        if g.get("id") == gpu_type_id:
            price = g.get(RUNPOD_PRICE_FIELDS[(cloud, cls)])
            if not price:
                raise BrokerError(
                    "that runpod class is no longer priced for this cloud — nothing was placed")
            return {"gpu_type_id": gpu_type_id, "cloud": cloud, "cls": cls,
                    "price_per_gpu": float(price),
                    "stock": ((g.get("lowestPrice") or {}).get("stockStatus"))}
    raise BrokerError("that GPU type is gone from runpod's catalog — nothing was placed")

def _runpod_deploy_parts(rq: dict, *, image: str, disk_gb, label: str):
    """(mutation_field, input_type, input_obj) for the deploy call — shared by
    dry-run (shown verbatim) and live placement (sent verbatim)."""
    input_obj = {
        "gpuTypeId": rq["gpu_type_id"],
        "cloudType": "SECURE" if rq["cloud"] == "secure" else "COMMUNITY",
        "gpuCount": 1,
        "imageName": image or RUNPOD_DEFAULT_IMAGE,
        "containerDiskInGb": int(disk_gb or 10),
        "name": (label or "compute-wick-pics")[:60],
    }
    if rq["cls"] == "interruptible":
        input_obj["bidPerGpu"] = round(rq["price_per_gpu"], 4)
        return "podRentInterruptable", "PodRentInterruptableInput", input_obj
    return "podFindAndDeployOnDemand", "PodFindAndDeployOnDemandInput", input_obj

def _place_runpod(rq: dict, api_key: str, *, image: str, disk_gb, label: str):
    field, input_type, input_obj = _runpod_deploy_parts(
        rq, image=image, disk_gb=disk_gb, label=label)
    query = ("mutation Deploy($input: %s!) { %s(input: $input) "
             "{ id costPerHr desiredStatus } }" % (input_type, field))
    payload, status = _gql(api_key, query, {"input": input_obj})
    errs = (payload or {}).get("errors")
    pod = ((payload or {}).get("data") or {}).get(field)
    if status >= 500:                    # heard nothing definite: may have placed
        raise AmbiguousOutcome(f"HTTP {status}")
    if status != 200 or errs or not (pod or {}).get("id"):
        detail = (errs or [{}])[0].get("message") or f"HTTP {status}"
        raise BrokerError(f"runpod refused the placement: {detail}")
    return pod

def _status_runpod(pod_id: str, api_key: str) -> dict:
    query = ("query Pod($input: PodFilter!) { pod(input: $input) "
             "{ id desiredStatus lastStatusChange costPerHr uptimeSeconds "
             "runtime { uptimeInSeconds } } }")
    payload, status = _gql(api_key, query, {"input": {"podId": pod_id}})
    if status != 200 or payload is None:
        raise BrokerError(f"runpod status fetch failed (HTTP {status})")
    pod = ((payload.get("data") or {}).get("pod"))
    if not pod:
        return {"state": "gone", "note": "no such pod on this account "
                "(terminated or a different key)"}
    desired = (pod.get("desiredStatus") or "").lower()
    runtime = pod.get("runtime") or {}
    uptime = runtime.get("uptimeInSeconds") or pod.get("uptimeSeconds")
    state = {"running": "running" if runtime else "starting",
             "exited": "stopped", "terminated": "gone"}.get(desired, desired or "unknown")
    return {
        "state": state,
        "uptime_seconds": uptime,
        "price_hr": pod.get("costPerHr"),
        "raw": {k: pod.get(k) for k in
                ("desiredStatus", "lastStatusChange", "costPerHr")},
    }

# ---------------------------------------------------------------- entry points

def place_rental(*, offer_id: str, api_key: str, max_price_per_gpu_hr: float,
                 confirm: bool = False, dry_run: bool = True,
                 idempotency_key: str = "", image: str = "",
                 disk_gb: float = 10, label: str = "",
                 auto_destroy_budget_usd=None, account_token: str = "") -> dict:
    """The one entry point. Returns a receipt dict; raises BrokerError with a
    safe message on any refusal. The key never touches disk, DB, or logs."""
    _require_enabled()
    offer = _validate(offer_id, api_key, max_price_per_gpu_hr, confirm, dry_run,
                      auto_destroy_budget_usd)
    account_hash = None
    if account_token:
        from core import meter
        account_hash = meter.resolve(account_token)
        if not account_hash:
            raise BrokerError("unknown account_token — register at POST /api/account")

    if idempotency_key and not dry_run:
        prior = _lookup_receipt(idempotency_key)
        if prior is not None:
            prior["idempotent_replay"] = True
            return prior

    with active_secret(api_key, account_token):
        if offer["provider"] == "vast":
            live = _requote_vast(offer["provider_offer_id"], api_key, offer["class"])
            gpu_count = live.get("num_gpus") or 1
            live_price = float(live.get("dph_total") or 0)
            rq = None
        else:
            rq = _requote_runpod(offer["provider_offer_id"], api_key)
            gpu_count = 1
            live_price = rq["price_per_gpu"]
        live_per_gpu = live_price / gpu_count
        if not live_price:
            raise BrokerError("the live quote carries no price — nothing was placed")
        if live_per_gpu > max_price_per_gpu_hr:
            raise BrokerError(
                f"live price ${live_per_gpu:.4f}/GPU/hr exceeds your max "
                f"${max_price_per_gpu_hr:.4f} — nothing was placed")

        if offer["provider"] == "vast":
            payload = {
                "client_id": "me",
                "image": image or "pytorch/pytorch",
                "disk": float(disk_gb or 10),
                "runtype": "ssh",
            }
            if offer["class"] == "interruptible":
                payload["price"] = round(live_per_gpu * gpu_count, 6)
            would_place = {"method": "PUT",
                           "url": VAST_ASKS_URL.format(id=offer["provider_offer_id"]),
                           "body": payload}
        else:
            field, input_type, input_obj = _runpod_deploy_parts(
                rq, image=image, disk_gb=disk_gb, label=label)
            would_place = {"method": "POST", "url": RUNPOD_GQL_URL,
                           "mutation": field, "input": input_obj}

        if dry_run:
            out = {
                "dry_run": True,
                "would_place": would_place,
                "offer_id": offer_id,
                "provider": offer["provider"],
                "live_price_per_gpu_hr": round(live_per_gpu, 6),
                "live_price_final_per_gpu_hr": economics.apply_price(live_per_gpu),
                "fee_bps": config.FEE_BPS,
                "gpu_model": offer["gpu_model"],
                "gpu_count": gpu_count,
                "note": "no rental was placed; repeat with dry_run=false and confirm=true to execute",
            }
            if auto_destroy_budget_usd:
                out["budget_guard"] = {
                    "would_arm": True, "budget_usd": float(auto_destroy_budget_usd),
                    "note": _GUARD_NOTE}
            return out

        # Reserve the idempotency row BEFORE the money moves. A racing duplicate
        # request loses the UNIQUE insert and replays the first receipt instead
        # of placing a second machine.
        if idempotency_key:
            try:
                with db.get_db() as conn:
                    conn.execute(
                        "INSERT INTO rentals (idempotency_key, provider,"
                        " provider_offer_id, dry_run, created_at, receipt)"
                        " VALUES (?,?,?,?,?,?)",
                        (idempotency_key, offer["provider"], offer["provider_offer_id"],
                         0, int(time.time()), json.dumps({"status": "placing"})))
                    conn.commit()
            except db.IntegrityError:
                prior = _lookup_receipt(idempotency_key) or {"status": "placing"}
                prior["idempotent_replay"] = True
                return prior

        if offer["provider"] == "vast":
            try:
                result, status = request_json(
                    "PUT", VAST_ASKS_URL.format(id=offer["provider_offer_id"]),
                    json_body=payload,
                    headers={"Authorization": f"Bearer {api_key}"})
            except Exception as e:      # timeout / transport: it may have landed
                _mark_unknown(idempotency_key, offer,
                              f"transport: {type(e).__name__}")
                raise BrokerError(f"vast placement {VERIFY_NOTE}")
            if status != 200 or not (result or {}).get("success"):
                detail = (result or {}).get("msg") or (result or {}).get("error") or f"HTTP {status}"
                if status >= 500:       # provider-side failure: may have placed
                    _mark_unknown(idempotency_key, offer, f"HTTP {status}")
                    raise BrokerError(f"vast answered {detail} — {VERIFY_NOTE}")
                if idempotency_key:     # definite refusal; nothing placed
                    _delete_reservation(idempotency_key)
                raise BrokerError(f"vast refused the placement: {detail}")
            instance_id = result.get("new_contract")
        else:
            try:
                pod = _place_runpod(rq, api_key, image=image, disk_gb=disk_gb,
                                    label=label)
            except AmbiguousOutcome as e:
                _mark_unknown(idempotency_key, offer, str(e))
                raise BrokerError(f"runpod placement {VERIFY_NOTE}")
            except BrokerError:
                if idempotency_key:     # definite refusal; nothing placed
                    _delete_reservation(idempotency_key)
                raise
            except Exception as e:      # timeout / transport
                _mark_unknown(idempotency_key, offer,
                              f"transport: {type(e).__name__}")
                raise BrokerError(f"runpod placement {VERIFY_NOTE}")
            instance_id = pod.get("id")
            live_price = float(pod.get("costPerHr") or live_price)
            live_per_gpu = live_price / gpu_count

        receipt = {
            "dry_run": False,
            "provider": offer["provider"],
            "provider_instance_id": instance_id,
            "offer_id": offer_id,
            "gpu_model": offer["gpu_model"],
            "gpu_count": gpu_count,
            "price_hr": live_price,
            "price_per_gpu_hr": round(live_per_gpu, 6),
            "price_final_per_gpu_hr": economics.apply_price(live_per_gpu),
            "fee_bps": config.FEE_BPS,
            "placed_at": int(time.time()),
            "label": (label or "")[:80],
            "manage_url": ("https://cloud.vast.ai/instances/"
                           if offer["provider"] == "vast"
                           else "https://www.runpod.io/console/pods"),
        }
        if auto_destroy_budget_usd:
            from core import guard
            guard.register(offer["provider"], str(instance_id), api_key,
                           float(auto_destroy_budget_usd), live_price,
                           label=receipt["label"])
            receipt["budget_guard"] = {
                "armed": True, "budget_usd": float(auto_destroy_budget_usd),
                "note": _GUARD_NOTE}
        # The machine is now running and billing. Recording it must never turn
        # a real placement into a "failed" answer: if the write fails, return
        # the instance id with recorded=false rather than raising.
        try:
            with db.get_db() as conn:
                if idempotency_key:
                    conn.execute(
                        "UPDATE rentals SET instance_id=?, price_hr=?, label=?,"
                        " receipt=?, account_hash=? WHERE idempotency_key=?",
                        (str(instance_id), live_price, receipt["label"],
                         json.dumps(receipt), account_hash, idempotency_key))
                else:
                    conn.execute(
                        "INSERT INTO rentals (idempotency_key, provider,"
                        " provider_offer_id, instance_id, price_hr, dry_run,"
                        " created_at, label, receipt, account_hash)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (None, offer["provider"], offer["provider_offer_id"],
                         str(instance_id), live_price, 0, receipt["placed_at"],
                         receipt["label"], json.dumps(receipt), account_hash))
                conn.commit()
        except Exception as e:
            log.warning("broker: instance %s placed but receipt write failed: %s",
                        instance_id, type(e).__name__)
            receipt["recorded"] = False
            receipt["warning"] = ("instance is running but our record write failed; "
                                  "the instance id above is authoritative")
        from core import meter
        meter.record(account_hash, "rental_placed", provider=offer["provider"],
                     instance_id=str(instance_id), gpu_model=offer["gpu_model"],
                     gpu_count=gpu_count, price_hr=live_price)
        log.info("broker: placed %s instance %s (%s x%d)", offer["provider"],
                 instance_id, offer["gpu_model"], gpu_count)
        return receipt

_GUARD_NOTE = ("the guard holds your key in PROCESS MEMORY ONLY on this station "
               "until the cap trips or the rental ends — never disk, never DB. "
               "If this station restarts the guard is lost and your rental keeps "
               "running; poll rental_status yourself as the backstop.")

# refusal texts that mean "this offer, not your request, is the problem" — the
# only refusals place_best may walk past to the next offer
_GONE_MARKERS = ("gone", "delisted", "no longer priced", "catalog")

def place_best(*, gpu: str = "", api_key: str, max_price_per_gpu_hr: float,
               offer_class: str = "", provider: str = "", country: str = "",
               min_vram_gb=None, min_gpu_count=None, confirm: bool = False,
               dry_run: bool = True, idempotency_key: str = "", image: str = "",
               disk_gb: float = 10, label: str = "",
               auto_destroy_budget_usd=None, account_token: str = "") -> dict:
    """One-shot: search the live book, place on the best matching offer.
    Walks past at most 3 already-gone offers; a price refusal or provider
    refusal stops the walk — never spend-shop past the caller's own guard."""
    _require_enabled()
    if provider and provider not in BROKERABLE:
        raise BrokerError(f"the broker places on {'/'.join(BROKERABLE)} only")
    from core import rank
    candidates = [o for o in rank.query(
        gpu=gpu or None, country=country or None, cls=offer_class or None,
        provider=provider or None, max_price_per_gpu=max_price_per_gpu_hr,
        min_vram=min_vram_gb, min_gpu_count=min_gpu_count, limit=10)
        if o["provider"] in BROKERABLE]
    if not candidates:
        raise BrokerError("no live offer matches — loosen the filters or raise the price cap")
    attempts = []
    for offer in candidates[:3]:
        try:
            receipt = place_rental(
                offer_id=offer["id"], api_key=api_key,
                max_price_per_gpu_hr=max_price_per_gpu_hr, confirm=confirm,
                dry_run=dry_run, idempotency_key=idempotency_key, image=image,
                disk_gb=disk_gb, label=label,
                auto_destroy_budget_usd=auto_destroy_budget_usd,
                account_token=account_token)
            if attempts:
                receipt["skipped_offers"] = attempts
            return receipt
        except BrokerError as e:
            msg = str(e)
            if any(m in msg for m in _GONE_MARKERS):
                attempts.append({"offer_id": offer["id"], "refusal": msg})
                continue
            raise
    raise BrokerError(
        "the top matching offers were all gone at placement — the book moved; "
        f"re-run (tried {len(attempts)})")

def _status_raw(provider: str, instance_id: str, api_key: str) -> dict:
    """Ungated status core — also the guard's path, so armed guards keep
    working even if BROKER_ENABLED is later flipped off."""
    with active_secret(api_key):
        if provider == "vast":
            st = _status_vast(instance_id, api_key)
        else:
            st = _status_runpod(instance_id, api_key)
    st["provider"] = provider
    st["instance_id"] = str(instance_id)
    st["checked_at"] = int(time.time())
    if st.get("uptime_seconds") and st.get("price_hr"):
        st["spend_est_usd"] = round(st["price_hr"] * st["uptime_seconds"] / 3600, 4)
        st["spend_basis"] = ("price_hr x uptime — an estimate; "
                             "the provider's own bill is authoritative")
    return st

def rental_status(*, provider: str, provider_instance_id: str,
                  api_key: str) -> dict:
    """Live state + spend estimate for an instance on the caller's account.
    Pass-through key, read-only."""
    _require_enabled()
    if provider not in BROKERABLE:
        raise BrokerError(f"status is available for {'/'.join(BROKERABLE)} only")
    if not api_key or len(api_key) < 8:
        raise BrokerError("api_key is required")
    st = _status_raw(provider, provider_instance_id, api_key)
    from core import guard
    g = guard.status(provider, str(provider_instance_id))
    if g:
        st["budget_guard"] = g
    return st

def _destroy_raw(provider: str, instance_id: str, api_key: str):
    """Ungated destroy core — shared by destroy_rental and the budget guard."""
    with active_secret(api_key):
        if provider == "vast":
            result, status = request_json(
                "DELETE",
                f"https://console.vast.ai/api/v0/instances/{instance_id}/",
                headers={"Authorization": f"Bearer {api_key}"})
            if status != 200 or not (result or {}).get("success", True):
                detail = (result or {}).get("msg") or f"HTTP {status}"
                raise BrokerError(f"vast refused the destroy: {detail}")
        else:
            payload, status = _gql(
                api_key,
                "mutation Terminate($input: PodTerminateInput!) { podTerminate(input: $input) }",
                {"input": {"podId": str(instance_id)}})
            errs = (payload or {}).get("errors")
            if status != 200 or errs:
                detail = (errs or [{}])[0].get("message") or f"HTTP {status}"
                raise BrokerError(f"runpod refused the destroy: {detail}")

def destroy_rental(*, provider: str, provider_instance_id: str,
                   api_key: str) -> dict:
    """Tear down an instance on the caller's account. Pass-through, honest."""
    _require_enabled()
    if provider not in BROKERABLE:
        raise BrokerError(f"the broker manages {'/'.join(BROKERABLE)} instances only")
    if not api_key or len(api_key) < 8:
        raise BrokerError("api_key is required")
    _destroy_raw(provider, str(provider_instance_id), api_key)
    from core import guard, meter
    guard.release(provider, str(provider_instance_id))
    meter.record_destroy(provider, str(provider_instance_id), source="caller")
    return {"destroyed": str(provider_instance_id), "provider": provider}
