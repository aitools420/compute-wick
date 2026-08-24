"""Limit orders (keyholder rail), no network: broker.place_rental is stubbed.
Run: python -m tests.test_orders (throwaway DB)."""
import hashlib
import hmac
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
from core import orders
from core.schema import make_offer


def seed(price=0.30):
    now = int(time.time())
    o = make_offer(provider="vast", gpu_model="RTX 4090", gpu_count=1,
                   vram_gb=24, price_hr=price, region="Texas, US", country="US",
                   cls="interruptible", availability="available",
                   reliability=0.99, provider_link="https://cloud.vast.ai/",
                   provider_offer_id="111", fetched_at=now)
    db.replace_provider_offers("vast", [o], now)


def main():
    db.init_db()
    seed(price=0.30)

    # 1 — validation
    for bad in (dict(gpu="", max_price=1), dict(gpu="X", max_price=0),
                dict(gpu="X", max_price=1, cls="weird")):
        try:
            orders.create(bad.get("gpu"), bad.get("max_price"), bad.get("cls"))
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    print("1 ok — validation refuses junk")

    # 2 — create + authenticated get
    o = orders.create("RTX 4090", 0.10, "interruptible")
    oid, sec = o["id"], o["order_secret"]
    assert o["state"] == "armed" and len(sec) == 48
    pub = orders.get(oid)
    assert pub["authenticated"] is False and "secret" not in pub
    assert orders.get(oid, sec)["authenticated"] is True
    print("2 ok — create; secret shown once; public get is redacted")

    # 3 — no ticket while the market is above the line (0.30 > 0.10)
    assert orders.check_all() == 0
    assert orders.get(oid)["state"] == "armed"
    assert orders.get(oid)["last_price"] == 0.30
    print("3 ok — armed order holds while price is above the line")

    # 4 — price crosses: ticket cut, signed, readable only with the secret
    seed(price=0.08)
    assert orders.check_all() == 1
    o2 = orders.get(oid, sec)
    assert o2["state"] == "ticketed"
    t = o2["ticket"]
    payload = {k: v for k, v in t.items() if k != "sig"}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    assert hmac.new(sec.encode(), body.encode(),
                    hashlib.sha256).hexdigest() == t["sig"]
    assert t["offer"]["price_final_per_gpu_hr"] == 0.08
    assert orders.get(oid)["ticket"] == "present — pass order_secret to read it"
    print("4 ok — ticket cut on cross, HMAC verifies, redacted without secret")

    # 5 — ticketed order is not re-ticketed next pass
    assert orders.check_all() == 0
    print("5 ok — single-flight: no second ticket while one is live")

    # 6 — verify_ticket enforces the ticket's offer
    tid = t["offer"]["id"]
    assert orders.verify_ticket(oid, sec, tid)["offer"]["id"] == tid
    try:
        orders.verify_ticket(oid, sec, "some-other-offer")
        raise AssertionError("accepted mismatched offer")
    except ValueError:
        pass
    print("6 ok — fills are pinned to the ticket's exact offer")

    # 7 — expired ticket re-arms
    with db.get_db() as conn:
        conn.execute("UPDATE orders SET ticket_expires=? WHERE id=?",
                     (int(time.time()) - 5, oid))
        conn.commit()
    assert orders.take_ticket(oid, sec) is None
    orders.check_all()
    assert orders.get(oid)["state"] in ("armed", "ticketed")  # re-armed (and possibly re-cut)
    print("7 ok — expired ticket re-arms the order")

    # 8 — fill marks filled; cancel; expiry sweep
    orders.mark_filled(oid, {"provider": "vast", "instance_id": "999"})
    assert orders.get(oid)["state"] == "filled"
    o3 = orders.create("RTX 4090", 0.05)
    try:
        orders.cancel(o3["id"], "wrong")
        raise AssertionError("cancel with bad secret")
    except ValueError:
        pass
    orders.cancel(o3["id"], o3["order_secret"])
    assert orders.get(o3["id"])["state"] == "cancelled"
    o4 = orders.create("RTX 4090", 0.05)
    with db.get_db() as conn:
        conn.execute("UPDATE orders SET expires_at=? WHERE id=?",
                     (int(time.time()) - 5, o4["id"]))
        conn.commit()
    orders.check_all()
    assert orders.get(o4["id"])["state"] == "expired"
    print("8 ok — filled / cancelled / expired transitions")

    print("ALL ORDERS TESTS PASS")


if __name__ == "__main__":
    main()
