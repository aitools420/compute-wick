# x402 keyless rentals — Phase 3 design (draft, 2026-08-24)

Goal: an agent with nothing but a wallet rents a GPU in one call — no provider
account, no API key, no human. The station places the rental on HOUSE capacity,
meters usage, and bills per prepaid block over x402 (HTTP 402 micropayments,
USDC). This is where "earn the gap" lives: house buys at market, resells
metered, fee_bps finally has a customer.

## The ToS read (done first, on purpose — 2026-08-24)

The naive model — rent on our Vast/RunPod keys, resell metered — is NOT clean:

- **RunPod ToS (2026-03-24), §8 Prohibited Activities:** "Resell any credits
  purchased through the Service without the prior written consent of Runpod."
  → resale is consent-gated, and the clause invites asking. Also §7: no use
  "as part of any effort to compete with us."
- **Vast ToS (2025-11-10), Prohibited Activities #10:** no "effort to compete
  with Company or to provide services as a service bureau." Account holder is
  responsible for all activity under the account.
  → reselling house-key capacity to third parties reads as service-bureau use.

Conclusions:
1. **Vast: never resold.** BYO-key and referral only.
2. **RunPod: parked pending written consent** — a partner/consent ask is
   business outreach, i.e. an operator decision under the standing outreach freeze.
3. **Akash: the clean venue.** Permissionless by design — tenants, providers
   and intermediaries all interact with the chain; brokering is protocol-
   native, no ToS wall. (io.net similar in spirit; API surface less mature.)

## MVP architecture (Akash first)

    agent ──x402 402/USDC──▶ station ──AKT lease──▶ Akash provider
                              │  meter.py (house account, /receipts publishes)
                              │  guard.py (budget auto-destroy at prepaid cap)

- **Payment:** x402 middleware on `POST /api/x/rent` — 402 challenge carries
  price-per-block; agent pays USDC (Base); station verifies settlement, then
  places. Prepaid blocks only (e.g. 1 GPU-hour at a time); no postpaid debt.
- **Placement:** house Akash wallet holds a small AKT float (~$25). SDL
  manifest templated per GPU class; one class at MVP (e.g. RTX 4090).
- **Metering:** every placement/renewal/destroy lands in usage_events under a
  dedicated x402 house account; fee_bps applies at event time — fees ON for
  this rail is a one-line .env change and is disclosed in the 402 challenge.
- **Termination:** prepaid block exhausts → guard destroys the lease (same
  in-RAM guard discipline as auto_destroy_budget_usd; disclosed in receipt).
- **Transparency:** the x402 house account publishes at /receipts like
  renter-1 — the resale book has a public tape from fill #1.

## Risks, named

- **Float volatility:** AKT price moves; keep float ≤ $25 at MVP, top up
  manually, operator-approved.
- **Holding user prepayments:** blocks are small and consumed immediately —
  we sell a service block, not custody a balance. Refund path: none at MVP
  (stated in the 402 challenge); a failed placement = no charge (charge only
  after lease is live).
- **Akash reliability:** provider quality varies; MVP pins to audited
  providers and surfaces the provider attestation in the receipt.
- **Regulatory:** selling metered compute for crypto is a sale of services,
  not money transmission; still worth a lawyer's eye before fees flip on.

## Not in MVP

Multi-class inventory, postpaid, refunds, RunPod (awaiting consent), Vast
(never), fiat rails, autoscaling.

## Operator gates

1. GO on the Akash-first shape (this doc).
2. ~$25 AKT float + a house wallet (existing bot-wallet doctrine: dedicated
   wallet, never reused).
3. Optional, separate: the RunPod written-consent ask (outreach — his call).
