"""Broker depth + intel + metering, no network: provider calls are stubbed at
request_json. Run: python -m tests.test_broker2 (throwaway DB)."""
import json
import os
import sys
import tempfile
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["BROKER_ENABLED"] = "1"

import config
config.BROKER_ENABLED = True
config.FEE_BPS = 0
import db
from core import broker, guard, intel, meter, perf, rank
from core.schema import make_offer

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

def seed_offers(now):
    vast_offer = make_offer(
        provider="vast", gpu_model="RTX 4090", gpu_count=1, vram_gb=24,
        price_hr=0.30, region="Texas, US", country="US", cls="interruptible",
        availability="available", reliability=0.99,
        provider_link="https://cloud.vast.ai/", provider_offer_id="111",
        fetched_at=now)
    vast_8x = make_offer(
        provider="vast", gpu_model="RTX 4090", gpu_count=8, vram_gb=24,
        price_hr=2.40, region="Berlin, DE", country="DE", cls="on_demand",
        availability="available", reliability=0.98,
        provider_link="https://cloud.vast.ai/", provider_offer_id="222",
        fetched_at=now)
    rp_offer = make_offer(
        provider="runpod", gpu_model="RTX 4090", gpu_count=1, vram_gb=24,
        price_hr=0.44, region="secure cloud", country=None, cls="on_demand",
        availability="High", reliability=None,
        provider_link="https://www.runpod.io/console/deploy",
        provider_offer_id="NVIDIA GeForce RTX 4090:secure:on_demand",
        fetched_at=now)
    db.replace_provider_offers("vast", [vast_offer, vast_8x])
    db.replace_provider_offers("runpod", [rp_offer])
    return vast_offer, vast_8x, rp_offer

def main():
    now = int(time.time())
    db.init_db()
    vast_offer, vast_8x, rp_offer = seed_offers(now)

    # -------- filters: min_gpu_count, EU macro, region
    assert [o["id"] for o in rank.query(min_gpu_count=8)] == [vast_8x["id"]]
    assert [o["id"] for o in rank.query(country="EU")] == [vast_8x["id"]]
    assert [o["id"] for o in rank.query(country="US,DE")
            ] == [vast_offer["id"], vast_8x["id"]]
    assert [o["id"] for o in rank.query(region="secure")] == [rp_offer["id"]]

    # -------- runpod offer-id parsing
    gid, cloud, cls = broker._parse_runpod_offer_id(rp_offer["provider_offer_id"])
    assert (gid, cloud, cls) == ("NVIDIA GeForce RTX 4090", "secure", "on_demand")
    try:
        broker._parse_runpod_offer_id("garbage")
        raise AssertionError("bad id must refuse")
    except broker.BrokerError:
        pass

    # -------- runpod dry-run: requote via stubbed graphql, exact payload back
    def fake_rj(method, url, *, headers=None, json_body=None, params=None):
        if url == broker.RUNPOD_GQL_URL:
            q = (json_body or {}).get("query", "")
            if "gpuTypes" in q:
                return {"data": {"gpuTypes": [{
                    "id": "NVIDIA GeForce RTX 4090", "securePrice": 0.46,
                    "communityPrice": 0.39, "secureSpotPrice": 0.24,
                    "communitySpotPrice": 0.21, "secureCloud": True,
                    "communityCloud": True,
                    "lowestPrice": {"stockStatus": "High"}}]}}, 200
            if "podFindAndDeployOnDemand" in q:
                return {"data": {"podFindAndDeployOnDemand": {
                    "id": "pod-abc", "costPerHr": 0.46,
                    "desiredStatus": "RUNNING"}}}, 200
            if "podTerminate" in q:
                return {"data": {"podTerminate": None}}, 200
            if "pod(input" in q or '"pod"' in q or "query Pod" in q:
                return {"data": {"pod": {
                    "id": "pod-abc", "desiredStatus": "RUNNING",
                    "lastStatusChange": "x", "costPerHr": 0.46,
                    "uptimeSeconds": 7200,
                    "runtime": {"uptimeInSeconds": 7200}}}}, 200
        raise AssertionError(f"unexpected URL {url}")
    real_rj = broker.request_json
    broker.request_json = fake_rj
    try:
        dry = broker.place_rental(
            offer_id=rp_offer["id"], api_key="k" * 20,
            max_price_per_gpu_hr=0.50)
        assert dry["dry_run"] and dry["provider"] == "runpod"
        assert dry["would_place"]["mutation"] == "podFindAndDeployOnDemand"
        assert dry["would_place"]["input"]["gpuTypeId"] == "NVIDIA GeForce RTX 4090"
        assert dry["would_place"]["input"]["cloudType"] == "SECURE"
        assert dry["live_price_per_gpu_hr"] == 0.46

        # price guard: live 0.46 over a 0.40 cap must refuse
        try:
            broker.place_rental(offer_id=rp_offer["id"], api_key="k" * 20,
                                max_price_per_gpu_hr=0.40)
            raise AssertionError("cap breach must refuse")
        except broker.BrokerError as e:
            assert "exceeds your max" in str(e)

        # live placement + metering + guard arming
        acct = meter.create_account("test")
        rec = broker.place_rental(
            offer_id=rp_offer["id"], api_key="k" * 20,
            max_price_per_gpu_hr=0.50, confirm=True, dry_run=False,
            idempotency_key="idem-1", auto_destroy_budget_usd=5.0,
            account_token=acct["account_token"])
        assert rec["provider_instance_id"] == "pod-abc"
        assert rec["budget_guard"]["armed"] and guard.active_count() == 1
        replay = broker.place_rental(
            offer_id=rp_offer["id"], api_key="k" * 20,
            max_price_per_gpu_hr=0.50, confirm=True, dry_run=False,
            idempotency_key="idem-1")
        assert replay.get("idempotent_replay") is True

        # lifecycle status: stubbed pod query -> spend estimate + guard visible
        st = broker.rental_status(provider="runpod",
                                  provider_instance_id="pod-abc",
                                  api_key="k" * 20)
        assert st["state"] == "running" and st["uptime_seconds"] == 7200
        assert st["spend_est_usd"] == round(0.46 * 2, 4)
        assert st["budget_guard"]["armed"]

        # guard: below cap -> keeps; force cap -> destroys and meters
        assert guard.check_all() == 0 and guard.active_count() == 1
        with guard._lock:
            guard._guards[("runpod", "pod-abc")]["budget_usd"] = 0.5
        assert guard.check_all() == 1 and guard.active_count() == 0

        # caller destroy on a fresh placement
        rec2 = broker.place_rental(
            offer_id=rp_offer["id"], api_key="k" * 20,
            max_price_per_gpu_hr=0.50, confirm=True, dry_run=False)
        out = broker.destroy_rental(provider="runpod",
                                    provider_instance_id=rec2["provider_instance_id"],
                                    api_key="k" * 20)
        assert out["destroyed"] == "pod-abc"

        # ledger: placed x2 attributed? (second placement had no token)
        u = meter.usage(acct["account_token"], days=1)
        kinds = [e["kind"] for e in u["events"]]
        assert "rental_placed" in kinds and "guard_destroyed" in kinds
        assert u["totals"]["fee_usd"] == 0.0
        pt = meter.platform_totals(1)
        assert pt["rentals_placed"] == 2 and pt["rentals_destroyed"] == 2
        assert meter.usage("cwk_bogus", days=1) is None

        # place_best: cheapest brokerable is the vast interruptible; make the
        # vast requote say GONE and the walk must land on the runpod offer
        def fake_rj2(method, url, *, headers=None, json_body=None, params=None):
            if url == broker.VAST_BUNDLES_URL:
                return {"offers": []}, 200          # gone
            return fake_rj(method, url, headers=headers, json_body=json_body,
                           params=params)
        broker.request_json = fake_rj2
        best = broker.place_best(gpu="4090", api_key="k" * 20,
                                 max_price_per_gpu_hr=0.60)
        assert best["dry_run"] and best["provider"] == "runpod"
        assert len(best["skipped_offers"]) >= 1
    finally:
        broker.request_json = real_rj

    # -------- adapters: cudo + ionet fixtures
    from providers import cudo, ionet
    cf = json.load(open(f"{FIX}/cudo_sample.json"))
    co = cudo.parse(cf["vm"], cf["bm"], cf["gpus"], now)
    assert len(co) == 2, f"cudo rows: {len(co)}"   # zero-free and CPU rows drop
    vm = next(o for o in co if o["provider_offer_id"].startswith("vm:"))
    bm = next(o for o in co if o["provider_offer_id"].startswith("bm:"))
    assert vm["gpu_model"] == "A100 80GB PCIE" and vm["vram_gb"] == 80.0
    assert vm["country"] == "NO" and vm["price_per_gpu_hr"] == 1.5
    assert bm["gpu_count"] == 8 and bm["price_per_gpu_hr"] == round(14.32 / 8, 6)

    io = ionet.parse(json.load(open(f"{FIX}/ionet_sample.json")), now)
    # H100 idle=0 drops, CPU drops; 4090 has 2 priced products
    assert len(io) == 2, f"ionet rows: {len(io)}"
    assert all(o["gpu_model"] == "RTX 4090" for o in io)
    assert sorted(o["region"] for o in io) == ["kubernetes", "ray cluster"]

    from providers import hyperstack
    hf = json.load(open(f"{FIX}/hyperstack_sample.json"))
    hs = hyperstack.parse(hf["flavors"], hf["pricebook"], hf["stocks"], now)
    # out-of-stock, CPU, and unpriced-gpu rows drop
    assert len(hs) == 2, f"hyperstack rows: {len(hs)}"
    a100 = next(o for o in hs if o["gpu_model"] == "A100 PCIE")
    assert a100["gpu_count"] == 4 and a100["price_hr"] == 5.4
    assert a100["price_per_gpu_hr"] == 1.35 and a100["vram_gb"] == 80.0
    assert a100["country"] == "NO" and a100["class"] == "on_demand"
    assert a100["availability"] == "10 available in NORWAY-1"
    l40 = next(o for o in hs if o["gpu_model"] == "L40")
    assert l40["class"] == "interruptible" and l40["price_hr"] == 0.8
    assert l40["country"] == "CA" and l40["availability"] == "25+ available in CANADA-1"

    from providers import novita
    nv = novita.parse(json.load(open(f"{FIX}/novita_sample.json")), now)
    # availableDeploy=False drops; one offer per (product, region)
    assert len(nv) == 8, f"novita rows: {len(nv)}"
    assert not any(o["provider_offer_id"].startswith("26:") for o in nv)
    h100 = [o for o in nv if o["gpu_model"] == "H100 SXM"]
    assert len(h100) == 2 and h100[0]["price_hr"] == 3.39 and h100[0]["vram_gb"] == 80.0
    assert sorted(o["country"] for o in h100) == ["IS", "US"]
    r4090 = [o for o in nv if o["gpu_model"] == "RTX 4090"]
    assert len(r4090) == 3 and all(o["price_hr"] == 0.33 for o in r4090)
    assert {o["country"] for o in nv if o["gpu_model"] == "L40S"} == {"ZA", "AE", "IN"}

    from providers import primeintellect
    pi = primeintellect.parse(
        json.load(open(f"{FIX}/primeintellect_sample.json")), now)
    # CPU_NODE drops; whole-node price splits per GPU; upstream cloud in availability
    assert len(pi) == 7, f"primeintellect rows: {len(pi)}"
    assert not any(o["gpu_model"] == "CPU NODE" for o in pi)
    h8 = next(o for o in pi if o["gpu_model"] == "H100 SXM" and o["gpu_count"] == 8)
    assert h8["price_hr"] == 31.92 and h8["price_per_gpu_hr"] == 3.99
    assert h8["vram_gb"] == 80.0 and h8["availability"] == "available via lambdalabs"
    assert sum(1 for o in pi if o["class"] == "interruptible") == 1  # SPOT H200

    from providers import shadeform
    sf = shadeform.parse(json.load(open(f"{FIX}/shadeform_sample.json")), now)
    # CPU type and available=false regions drop; hourly_price is cents
    assert len(sf) == 2, f"shadeform rows: {len(sf)}"
    sh = next(o for o in sf if o["gpu_model"] == "H100 PCIE")
    assert sh["price_hr"] == 5.99 and sh["country"] == "US"
    assert sh["availability"] == "available via paperspace (vm)"
    assert next(o for o in sf if o["gpu_model"] == "RTX A6000")["price_hr"] == 1.93

    # -------- perf value index: rated + unrated split, fee seam
    v = perf.value_index()
    rated = {r["gpu_model"] for r in v["value"]}
    assert "RTX 4090" in rated
    r4090 = next(r for r in v["value"]
                 if r["gpu_model"] == "RTX 4090" and r["offer_class"] == "interruptible")
    assert r4090["tflops_per_usd_hr"] == round(330 / 0.30, 1)
    assert perf.tflops_for("RTX 6000ADA") == perf.tflops_for("RTX 6000 ADA")

    # -------- intel: reliability weights + spread now-table
    db.record_health("vast", now, "ok", 100)
    db.record_health("vast", now - 60, "DOWN", 0)
    rel = intel.reliability(days=1)
    vrow = next(r for r in rel["providers"] if r["provider"] == "vast")
    assert vrow["polls"] == 2 and vrow["feed_score"] == 50.0
    assert vrow["machine_reliability_avg"] is not None

    sp = intel.spread()
    m = next(r for r in sp["models"] if r["gpu_model"] == "RTX 4090")
    assert m["on_demand_min"] == 0.3 and m["interruptible_min"] == 0.3
    # (vast_8x is 2.40/8 = 0.30/gpu on-demand; interruptible single also 0.30)
    assert m["discount_pct"] == 0.0

    # price_position needs >=10 snapshots — seed a tape
    with db.get_db() as conn:
        for i in range(12):
            conn.execute("INSERT INTO offer_history VALUES (?,?,?,?,?,?,?)",
                         (now - i * 3600, "vast", "RTX 4090", "interruptible",
                          5, 0.30 + i * 0.01, 0.35))
        conn.commit()
    pp = intel.price_position("RTX 4090", "interruptible")
    assert pp["snapshots"] == 12 and pp["percentile_vs_30d"] is not None
    assert "verdict" in pp and pp["current_best_per_gpu_hr"] == 0.3

    print("test_broker2: ALL PASS")

if __name__ == "__main__":
    main()
