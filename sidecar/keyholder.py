#!/usr/bin/env python3
"""keyholder — the self-hosted execution half of a compute.pangle.online limit order.

Runs on YOUR machine. Holds YOUR provider API key (env var, never sent anywhere
except the fill call, which the station passes through to the provider without
storing). Long-polls the station for a fill ticket; when one is cut, verifies
its HMAC signature with the order secret and executes the fill.

Environment:
  ORDER_ID        (required) from create order
  ORDER_SECRET    (required) shown once at create
  PROVIDER_KEY    (required) your own Vast.ai or RunPod API key
  COMPUTE_URL     default https://compute.pangle.online
  CONFIRM         "1" places the rental FOR REAL; anything else dry-runs
  ACCOUNT_TOKEN   optional usage-ledger token
  IMAGE, DISK_GB  optional runtime knobs passed to the fill

One command:
  docker build -t keyholder . && docker run -e ORDER_ID=... -e ORDER_SECRET=... \
    -e PROVIDER_KEY=... -e CONFIRM=1 keyholder
Or just: python3 keyholder.py  (stdlib only, no installs)
"""
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("COMPUTE_URL", "https://compute.pangle.online").rstrip("/")
ORDER_ID = os.environ.get("ORDER_ID", "")
SECRET = os.environ.get("ORDER_SECRET", "")
KEY = os.environ.get("PROVIDER_KEY", "")
CONFIRM = os.environ.get("CONFIRM", "") == "1"

def die(msg):
    print("keyholder:", msg, file=sys.stderr)
    sys.exit(1)

def post(path, body, timeout=70):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "User-Agent": "keyholder-sidecar/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def verify(ticket):
    payload = {k: v for k, v in ticket.items() if k != "sig"}
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    want = hmac.new(SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, ticket.get("sig", ""))

def main():
    # our prints ARE the interface — never let a pipe buffer them
    sys.stdout.reconfigure(line_buffering=True)
    if not (ORDER_ID and SECRET and KEY):
        die("ORDER_ID, ORDER_SECRET and PROVIDER_KEY are required")
    print(f"keyholder: watching order {ORDER_ID} on {BASE}"
          f" ({'LIVE fills' if CONFIRM else 'dry-run only — set CONFIRM=1'})")
    backoff = 5
    while True:
        try:
            r = post(f"/api/orders/{ORDER_ID}/ticket",
                     {"order_secret": SECRET, "wait": 55})
            backoff = 5
        except urllib.error.HTTPError as e:
            die(f"station refused: {e.read().decode()[:200]}")
        except Exception as e:
            print(f"keyholder: station unreachable ({type(e).__name__}),"
                  f" retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)
            continue
        state = r.get("state")
        if state in ("filled", "cancelled", "expired", "gone"):
            print(f"keyholder: order is {state}; exiting")
            return
        t = r.get("ticket")
        if not t:
            continue
        if not verify(t):
            die("ticket signature FAILED verification — refusing to act")
        offer = t["offer"]
        print(f"keyholder: ticket — {offer['gpu_model']} on {offer['provider']}"
              f" at ${offer['price_final_per_gpu_hr']:.4f}/GPU/hr"
              f" (line ${t['line']:.4f}, expires {t['expires_at']})")
        fill = {"order_secret": SECRET, "api_key": KEY,
                "confirm": CONFIRM, "dry_run": not CONFIRM}
        if os.environ.get("ACCOUNT_TOKEN"):
            fill["account_token"] = os.environ["ACCOUNT_TOKEN"]
        if os.environ.get("IMAGE"):
            fill["image"] = os.environ["IMAGE"]
        if os.environ.get("DISK_GB"):
            fill["disk_gb"] = float(os.environ["DISK_GB"])
        try:
            out = post(f"/api/orders/{ORDER_ID}/fill", fill, timeout=120)
        except urllib.error.HTTPError as e:
            print(f"keyholder: fill refused: {e.read().decode()[:300]}")
            time.sleep(10)
            continue
        print("keyholder: receipt:", json.dumps(out.get("receipt", out), indent=2))
        if CONFIRM and out.get("order_state") == "filled":
            print("keyholder: order FILLED; exiting")
            return
        if not CONFIRM:
            print("keyholder: dry-run complete (order stays armed);"
                  " set CONFIRM=1 for real fills. Watching on.")
            time.sleep(30)

if __name__ == "__main__":
    main()
