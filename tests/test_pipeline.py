"""Fixture round-trip: adapters parse -> schema validates -> db swap -> rank ->
economics. Run: python -m tests.test_pipeline (uses a throwaway DB)."""
import json
import os
import sys
import tempfile
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

import config
assert config.DB_PATH == os.environ["DB_PATH"]
import db
from core import economics, rank, stats
from providers import akash, datacrunch, runpod, vast

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

def main():
    now = int(time.time())
    db.init_db()

    v = vast.parse(json.load(open(f"{FIX}/vast_sample.json")), now)
    assert len(v) == 3, f"vast rows: {len(v)} (empty-name and zero-price must drop)"
    assert v[0]["gpu_model"] == "RTX 4090" and v[1]["gpu_model"] == "RTX 4090"
    assert v[1]["class"] == "interruptible" and v[1]["country"] == "US"
    assert v[1]["price_per_gpu_hr"] == round(1.52 / 4, 6)
    assert v[2]["vram_gb"] == round(81559 / 1024, 1)

    r = runpod.parse(json.load(open(f"{FIX}/runpod_sample.json")), now)
    # 4090: secure + community + community spot = 3; H100: secure + secure spot = 2
    assert len(r) == 5, f"runpod rows: {len(r)}"
    classes = sorted(o["class"] for o in r)
    assert classes.count("interruptible") == 2
    assert all(o["availability"] in ("High", "Low") for o in r)

    dc = datacrunch.parse(json.load(open(f"{FIX}/datacrunch_sample.json")), now)
    # 8xH100 od+spot, 1xA100 od+spot, L40S od only (zero spot drops); CPU row drops
    assert len(dc) == 5, f"datacrunch rows: {len(dc)}"
    assert dc[0]["gpu_count"] == 8 and dc[0]["vram_gb"] == 80.0, "VRAM must be per-GPU"
    assert dc[0]["price_per_gpu_hr"] == round(26.0 / 8, 6)
    assert sorted(o["class"] for o in dc).count("interruptible") == 2

    ak = akash.parse(json.load(open(f"{FIX}/akash_sample.json")), now)
    # a100 + rtx4090 + v100x2; None-price and zero-available rows drop
    assert len(ak) == 4, f"akash rows: {len(ak)}"
    names = sorted(o["gpu_model"] for o in ak)
    assert names == ["A100", "RTX 4090", "V100 16GB", "V100 32GB"], names
    assert all(o["class"] == "on_demand" for o in ak)
    assert ak[0]["availability"].endswith("GPUs")

    db.replace_provider_offers("vast", v)
    db.replace_provider_offers("runpod", r)
    db.replace_provider_offers("datacrunch", dc)
    db.replace_provider_offers("akash", ak)
    db.record_health("vast", now, "ok", len(v))
    db.record_health("runpod", now, "ok", len(r))
    db.record_health("datacrunch", now, "ok", len(dc))
    db.record_health("akash", now, "ok", len(ak))

    got = rank.query(gpu="4090")
    assert len(got) == 6, f"rank 4090: {len(got)}"
    assert got[0]["price_per_gpu_hr"] <= got[-1]["price_per_gpu_hr"]
    assert not got[0]["stale"]

    cheap = rank.query(max_price_per_gpu=0.3)
    assert all(o["price_per_gpu_hr"] <= 0.3 for o in cheap)

    priced = economics.apply_all(got)
    assert all(p["fee_bps"] == 0 for p in priced), "FEE_BPS must default 0"
    assert all(p["price_final_hr"] == p["price_hr"] for p in priced)
    assert all(p["link"] for p in priced)

    config.FEE_BPS = 250       # the lever: config only, nothing else changes
    config.REF_VAST = "9999"
    lever = economics.apply(v[0])
    assert lever["price_final_hr"] == round(v[0]["price_hr"] * 1.025, 6)
    assert "ref_id=9999" in lever["link"]   # Vast's param is ref_id, NOT ref
    assert economics.apply_price(1.0) == 1.025
    # the fee must move EVERY price surface, not just economics.apply
    levered_stats = stats.snapshot()
    base_cheap = min(c["min_price_per_gpu_hr"] for c in levered_stats["cheapest_by_model"])
    config.FEE_BPS = 0
    zero_cheap = min(c["min_price_per_gpu_hr"] for c in stats.snapshot()["cheapest_by_model"])
    assert base_cheap > zero_cheap, "stats cheapest_by_model must ride the fee seam"
    config.REF_VAST = ""

    s = stats.snapshot()
    assert s["total_offers"] == 17 and s["idle_offers"] == 5
    assert s["idle_share"] == round(5 / 17, 4)
    assert set(s["providers"]) == {"vast", "runpod", "datacrunch", "akash"}

    # a partial provider walk must be signalled, not swallowed (finding #1)
    from providers import vast as _vast
    pages = [({"offers": [{"id": 1, "gpu_name": "RTX 4090", "dph_total": 0.2,
                           "num_gpus": 1}] * 128}, 200), (None, 503)]
    seq = iter(pages)
    real_rq = _vast.request_json
    _vast.request_json = lambda *a, **k: next(seq, ({"offers": []}, 200))
    _vast_key = config.VAST_API_KEY
    config.VAST_API_KEY = "x" * 20         # force the keyed multi-page walk
    try:
        offers, status, note, complete = _vast.fetch_offers()
        assert complete is False, "a 503 mid-walk must report incomplete"
    finally:
        _vast.request_json = real_rq
        config.VAST_API_KEY = _vast_key

    hs = db.history_series("RTX 4090", "on_demand", now - 60)
    assert set(hs) <= {"vast", "runpod", "akash"} and hs, f"history providers: {set(hs)}"
    pt = hs["vast"][0]
    assert pt[0] == now and len(pt) == 4 and pt[1] <= pt[2], pt
    assert db.history_series("RTX 4090", "on_demand", now + 1) == {}

    from core import watches
    w = watches.create("4090", 0.5, None, None)          # fixture best is under 0.5
    assert watches.check_all() == 1
    w2 = watches.get(w["id"])
    assert w2["state"] == "tripped" and w2["events"][-1]["kind"] == "tripped"
    assert watches.check_all() == 0                       # no re-fire while tripped
    assert watches.validate_webhook("http://127.0.0.1:9/x") is not None
    try:
        watches.create("", 1, None, None)
        raise AssertionError("empty gpu must be rejected")
    except ValueError:
        pass
    assert watches.delete(w["id"]) and watches.get(w["id"]) is None

    # TTL: an expired watch is purged by the next check pass
    stale_w = watches.create("4090", 0.5, None, None)
    assert stale_w["expires_at"] == stale_w["created_at"] + watches.WATCH_TTL_DAYS * 86400
    with db.get_db() as conn:
        conn.execute("UPDATE watches SET created_at=? WHERE id=?",
                     (now - (watches.WATCH_TTL_DAYS * 86400 + 60), stale_w["id"]))
        conn.commit()
    watches.check_all()
    assert watches.get(stale_w["id"]) is None, "expired watch must be purged"

    # idle index clamps to the keyed-walk epoch — pre-key data undercounts idle
    real_epoch = config.IDLE_INDEX_EPOCH
    config.IDLE_INDEX_EPOCH = now + 3600
    assert db.idle_history(0) == [], "pre-epoch idle points must not be served"
    config.IDLE_INDEX_EPOCH = real_epoch
    assert db.idle_history(0), "post-epoch idle points must be served"

    # --- broker (stubbed provider; no network, no money) ---
    from core import broker
    try:
        broker.place_rental(offer_id=v[0]["id"], api_key="k" * 20,
                            max_price_per_gpu_hr=1)
        raise AssertionError("broker must refuse while BROKER_ENABLED=0")
    except broker.BrokerError:
        pass
    config.BROKER_ENABLED = True
    vast_offer = v[0]
    live_row = {"id": int(vast_offer["provider_offer_id"]),
                "num_gpus": vast_offer["gpu_count"],
                "dph_total": vast_offer["price_hr"]}
    calls = []
    def stub_request(method, url, *, headers=None, json_body=None, params=None):
        calls.append((method, url))
        if "bundles" in url:
            return {"offers": [live_row]}, 200
        return {"success": True, "new_contract": 424242}, 200
    real_request = broker.request_json
    broker.request_json = stub_request
    try:
        for bad in (
            dict(offer_id=vast_offer["id"], api_key="short", max_price_per_gpu_hr=1),
            dict(offer_id=vast_offer["id"], api_key="k" * 20, max_price_per_gpu_hr=0),
            dict(offer_id="nope", api_key="k" * 20, max_price_per_gpu_hr=1),
            dict(offer_id=vast_offer["id"], api_key="k" * 20,
                 max_price_per_gpu_hr=1, dry_run=False),   # live without confirm
        ):
            try:
                broker.place_rental(**bad)
                raise AssertionError(f"broker must refuse {bad}")
            except broker.BrokerError:
                pass
        dr = broker.place_rental(offer_id=vast_offer["id"], api_key="k" * 20,
                                 max_price_per_gpu_hr=99)
        assert dr["dry_run"] and dr["would_place"]["method"] == "PUT"
        try:   # live quote above the caller's max must refuse
            broker.place_rental(offer_id=vast_offer["id"], api_key="k" * 20,
                                max_price_per_gpu_hr=vast_offer["price_per_gpu_hr"] / 2)
            raise AssertionError("over-max quote must refuse")
        except broker.BrokerError:
            pass
        rc = broker.place_rental(offer_id=vast_offer["id"], api_key="k" * 20,
                                 max_price_per_gpu_hr=99, dry_run=False,
                                 confirm=True, idempotency_key="test-1")
        assert rc["provider_instance_id"] == 424242 and not rc["dry_run"]
        rc2 = broker.place_rental(offer_id=vast_offer["id"], api_key="k" * 20,
                                  max_price_per_gpu_hr=99, dry_run=False,
                                  confirm=True, idempotency_key="test-1")
        assert rc2.get("idempotent_replay"), "idempotency must replay, not re-place"
        with db.get_db() as conn:
            n = conn.execute("SELECT COUNT(*) c FROM rentals").fetchone()["c"]
            blob = conn.execute("SELECT receipt FROM rentals").fetchone()["receipt"]
        assert n == 1 and "k" * 20 not in blob, "one rental row, no key material"
        # redaction filter scrubs an active secret out of log output
        import io, logging as _lg
        buf = io.StringIO()
        h = _lg.StreamHandler(buf)
        _lg.getLogger("broker").addHandler(h)
        with broker.active_secret("sekrit-value"):
            _lg.getLogger("broker").warning("oops leaked sekrit-value in error")
        _lg.getLogger("broker").removeHandler(h)
        assert "sekrit-value" not in buf.getvalue() and "[REDACTED]" in buf.getvalue()
    finally:
        broker.request_json = real_request
        config.BROKER_ENABLED = False

    with db.get_db() as conn:
        wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert wal == "wal", wal

    print("ALL PIPELINE TESTS PASS —",
          f"{len(v)} vast + {len(r)} runpod + {len(dc)} datacrunch + {len(ak)} akash rows, idle_share {s['idle_share']}")

if __name__ == "__main__":
    main()
