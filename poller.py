"""The ingestion loop. One pass = every enabled provider fetched, normalized,
swapped into offers_current, health recorded — a provider failure is a recorded
DOWN, never a silent catch (sim-predict lesson). A PARTIAL fetch (some pages
failed mid-walk) is refused entirely: a fragment must never overwrite a good
book while reporting ok. 429s back off exponentially."""
import logging
import time

import config
import db

log = logging.getLogger("poller")

LOW_WATER = {"vast": 50, "runpod": 10, "datacrunch": 30, "akash": 10,
             "cudo": 3, "ionet": 5, "hyperstack": 5, "novita": 5,
             "primeintellect": 5, "shadeform": 10}
_backoff: dict[str, int] = {}   # provider -> skip until epoch

def _adapter(name):
    if name == "vast":
        from providers import vast
        return vast
    if name == "runpod":
        from providers import runpod
        return runpod
    if name == "datacrunch":
        from providers import datacrunch
        return datacrunch
    if name == "akash":
        from providers import akash
        return akash
    if name == "cudo":
        from providers import cudo
        return cudo
    if name == "ionet":
        from providers import ionet
        return ionet
    if name == "hyperstack":
        from providers import hyperstack
        return hyperstack
    if name == "novita":
        from providers import novita
        return novita
    if name == "primeintellect":
        from providers import primeintellect
        return primeintellect
    if name == "shadeform":
        from providers import shadeform
        return shadeform
    raise ValueError(f"unknown provider {name}")

def poll_provider(name: str, snapshot_ts: int = None, record_history: bool = True) -> dict:
    now = int(time.time())
    if _backoff.get(name, 0) > now:
        # a recorded skip, not a silent gap in the health series
        db.record_health(name, now, "skipped", 0, None,
                         f"backoff until {_backoff[name]}")
        return {"provider": name, "skipped": f"backoff until {_backoff[name]}"}
    mod = _adapter(name)
    complete = True
    try:
        result = mod.fetch_offers()
        offers, status, note = result[0], result[1], result[2]
        if len(result) > 3:
            complete = result[3]
    except Exception as e:
        offers, status, note = [], None, f"{type(e).__name__}: {e}"
    ts = int(time.time())
    if status == 429:
        prev = _backoff.get(f"_{name}_level", 0) + 1
        _backoff[f"_{name}_level"] = prev
        _backoff[name] = ts + min(config.POLL_SECONDS * (2 ** prev), 3600)
        note = f"429 rate limited, backing off; {note}"
    elif status == 200:
        _backoff.pop(f"_{name}_level", None)   # reset on any clean response

    if not complete:
        # a partial fetch: keep the previous good book, publish nothing.
        health = "partial"
        note = (note + "; partial fetch — swap refused").strip("; ")
    elif not offers:
        health = "DOWN"
    else:
        health = "low" if len(offers) < LOW_WATER.get(name, 10) else "ok"
        db.replace_provider_offers(name, offers, snapshot_ts=snapshot_ts,
                                   record_history=record_history)
    db.record_health(name, ts, health, len(offers), status, note)
    log.info("%s: %s %d offers (http=%s) %s", name, health, len(offers), status, note)
    return {"provider": name, "status": health, "offers": len(offers),
            "http": status, "note": note}

def poll_all(providers: list = None, record_history: bool = True) -> list[dict]:
    # one snapshot stamp for the whole pass, so every provider's history rows
    # land in the same time bucket (adapters otherwise stamp seconds apart and
    # split a bucket, distorting the idle index). A fast pass (subset, no
    # history) refreshes the live book only — the tape keeps its cadence.
    snapshot_ts = int(time.time())
    results = [poll_provider(name, snapshot_ts, record_history)
               for name in (providers if providers is not None else config.PROVIDERS)]
    if record_history:
        with db.get_db() as conn:   # retention: the history index makes this cheap
            conn.execute("DELETE FROM offer_history WHERE snapshot_ts < ?",
                         (snapshot_ts - config.HISTORY_RETENTION_DAYS * 86400,))
            conn.commit()
    return results

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    db.init_db()
    for r in poll_all():
        print(r)
