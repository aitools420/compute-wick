"""D3=A: ambiguous placement outcomes keep the reservation as 'unknown'.
Run: python -m tests.test_unknown (throwaway DB, network stubbed)."""
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
from core import broker
from core.schema import make_offer


def seed():
    now = int(time.time())
    o = make_offer(provider="vast", gpu_model="RTX 4090", gpu_count=1,
                   vram_gb=24, price_hr=0.30, region="Texas, US", country="US",
                   cls="interruptible", availability="available",
                   reliability=0.99, provider_link="https://cloud.vast.ai/",
                   provider_offer_id="46000001", fetched_at=now)
    db.replace_provider_offers("vast", [o], now)
    return o


def reservation(key):
    with db.get_db() as conn:
        row = conn.execute("SELECT receipt FROM rentals WHERE idempotency_key=?",
                           (key,)).fetchone()
    return json.loads(row["receipt"]) if row else None


def attempt(key):
    try:
        broker.place_rental(offer_id="va-46000001", api_key="k" * 32,
                            max_price_per_gpu_hr=1.0, confirm=True,
                            dry_run=False, idempotency_key=key)
        raise AssertionError("placement unexpectedly succeeded")
    except broker.BrokerError as e:
        return str(e)


def main():
    db.init_db()
    seed()
    with db.get_db() as conn:
        oid = conn.execute("SELECT id FROM offers_current").fetchone()["id"]

    real = broker.request_json
    # requote succeeds; the PLACEMENT PUT times out
    def requote_ok_place_timeout(method, url, **kw):
        if method == "GET" or "/bundles" in url:
            return real_requote(url)
        raise TimeoutError("simulated timeout")
    def real_requote(url):
        return ({"success": True, "num_gpus": 1, "dph_total": 0.30}, 200)

    import core.broker as B
    B.request_json = lambda method, url, **kw: (
        ({"offers": [{"id": 46000001, "num_gpus": 1, "dph_total": 0.30,
                      "rentable": True}]}, 200) if method == "GET"
        else (_ for _ in ()).throw(TimeoutError("simulated timeout")))

    def run(key, stub, expect_state):
        B.request_json = stub
        msg = None
        try:
            broker.place_rental(offer_id=oid, api_key="k" * 32,
                                max_price_per_gpu_hr=1.0, confirm=True,
                                dry_run=False, idempotency_key=key)
            raise AssertionError("unexpectedly succeeded")
        except broker.BrokerError as e:
            msg = str(e)
        r = reservation(key)
        if expect_state == "unknown":
            assert r and r["status"] == "unknown", r
            assert "UNKNOWN" in msg
        else:
            assert r is None, r
        return msg

    REQUOTE_OK = ({"offers": [{"id": 46000001, "num_gpus": 1,
                               "dph_total": 0.30, "rentable": True}]}, 200)

    def is_requote(url):
        return "bundles" in url or "search" in url

    # 1 — timeout on the placement call → reservation kept as unknown
    def stub_timeout(method, url, **kw):
        if is_requote(url):
            return REQUOTE_OK
        raise TimeoutError("simulated timeout")
    run("k-timeout", stub_timeout, "unknown")
    print("1 ok — transport timeout keeps the reservation as unknown")

    # 2 — provider 5xx → unknown (a 5xx can still mean PLACED)
    def stub_5xx(method, url, **kw):
        if is_requote(url):
            return REQUOTE_OK
        return None, 502
    run("k-5xx", stub_5xx, "unknown")
    print("2 ok — 5xx keeps the reservation as unknown")

    # 3 — clean 4xx refusal → reservation released (retry is safe)
    def stub_refuse(method, url, **kw):
        if is_requote(url):
            return REQUOTE_OK
        return {"success": False, "msg": "no such ask"}, 404
    run("k-refused", stub_refuse, "released")
    print("3 ok — definite refusal releases the reservation")

    # 4 — a retry after unknown REPLAYS the unknown receipt, buys nothing
    def stub_explode(method, url, **kw):
        if is_requote(url):
            return REQUOTE_OK
        raise AssertionError("placement re-attempted despite unknown receipt!")
    B.request_json = stub_explode
    out = broker.place_rental(offer_id=oid, api_key="k" * 32,
                              max_price_per_gpu_hr=1.0, confirm=True,
                              dry_run=False, idempotency_key="k-timeout")
    assert out["status"] == "unknown" and out.get("idempotent_replay")
    print("4 ok — retry replays the unknown receipt; no second placement")

    B.request_json = real
    print("ALL UNKNOWN-OUTCOME TESTS PASS")


if __name__ == "__main__":
    main()
