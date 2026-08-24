# keyholder — execute compute.wick.pics limit orders without giving anyone your key

A limit order says "rent me a 4090 when idle price ≤ $0.02/hr". The station
watches the market and cuts a signed fill ticket when your line is crossed —
but it never holds your provider key. This sidecar is the missing half: it
runs on YOUR machine, holds YOUR key, and executes tickets automatically.

    # 1. place the order (no key involved)
    curl -X POST https://compute.wick.pics/api/orders \
      -H 'content-type: application/json' \
      -d '{"gpu":"RTX 4090","max_price_per_gpu_hr":0.02,"offer_class":"interruptible"}'
    # -> note order.id and order.order_secret (shown once)

    # 2. run the keyholder with your own Vast/RunPod key
    docker build -t keyholder . && docker run --rm \
      -e ORDER_ID=... -e ORDER_SECRET=... -e PROVIDER_KEY=... -e CONFIRM=1 keyholder

No docker? `python3 keyholder.py` — stdlib only, nothing to install.

Safety properties, all verifiable in ~150 lines above:
- your key lives in your environment; it leaves only inside the fill call,
  which the station passes to the provider and never stores (RAM-only there).
- tickets are HMAC-signed with your order secret; a forged ticket is refused.
- the station re-quotes the live price before money moves and refuses fills
  above your line; fills are idempotent (a retry cannot rent twice).
- CONFIRM unset = dry-run: you see exactly what would happen, nothing rents.
- tip: use a scoped provider key where supported (Vast keys can be de-fanged
  to instance-ops only).
