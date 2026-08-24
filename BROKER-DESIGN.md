# Stage 2 — The Broker (BYO-key rental execution)

*Design doc, 2026-08-23; since built — kept as the original design record. operator GO required before code.*

Stage 1 points at the best offer; Stage 2 places it. One call: an agent (or human)
hands us their provider API key and an offer id, we execute the rental on their
account, they get a running machine. The renter's money moves on the PROVIDER's
books, never ours — but we are now software that spends other people's money,
and that changes what careless code costs.

## Key doctrine (the part that must be right before anything else)

1. **Pass-through, never persist.** The renter's key arrives in the request,
   is used for that one provider call, and dies with the request. Never written
   to the database, disk, or logs. No key vault in v1 — the feature that stores
   keys is a different, later decision with its own threat model.
2. **Redaction as code, not discipline.** A logging filter masks anything that
   looks like a key in every log line and every error path; a test asserts a
   placed rental leaves zero key material in logs, DB, and journal.
3. **Keys never ride GET.** POST body / MCP tool argument only, so they stay
   out of access logs and referrers by construction.

## Execution doctrine

4. **Nothing rents on search.** Renting is its own call with `offer_id`,
   the key, and an explicit `confirm: true`.
5. **Dry-run is the default.** `dry_run: true` unless flipped per call —
   returns exactly the provider payload that WOULD be sent, priced. An agent
   has to say the dangerous thing out loud to do the dangerous thing.
6. **Re-quote before placing.** Offers churn; the market moves in 5-minute
   ticks but the provider is live. We re-fetch the offer at placement, refuse
   if it is gone, and refuse if its price exceeds the caller's required
   `max_price_per_gpu_hr`. The stale table can never overspend the caller.
7. **Idempotency.** Caller-supplied `idempotency_key`; a duplicate request
   returns the first receipt instead of a second machine.
8. **Blast radius.** v1 places ONE instance per call, no auto-retry onto a
   different offer, no restarts, no scheduling. Boring on purpose.

## Provider order

- **v1: Vast only.** Our offers carry the real ask id
  (`provider_offer_id`); placement is one authenticated call against their
  ask endpoint. True per-host marketplace, cheapest inventory, and the
  1,037-offer book we already walk.
- **v1.1: RunPod** (GraphQL deploy mutations; class-based, maps from our
  synthesized offers).
- **Deferred: DataCrunch (OAuth account creds, not a simple key), Akash
  (wallet + deployment manifest — a different animal entirely).**

## Receipts and honesty

A placement returns `{provider, provider_instance_id, price_hr, placed_at}`
plus whatever connect info the provider hands back. We log the receipt WITHOUT
the key. `fee_bps` rides the receipt like every other price surface — still 0,
still visible. Open question flagged, not assumed: whether provider referral
attribution applies to API-placed rentals; investigate during build, never
claim it does until seen on a statement.

## Failure honesty

Provider errors surface verbatim-but-redacted. "The offer was taken" is an
answer, not a retry loop. A failed placement MUST be distinguishable from a
placed-but-slow instance; we return the provider's instance id or we say
plainly that nothing was created.

## Ship gate

- Code lands behind `BROKER_ENABLED=0` (default off) — deploy and iterate
  without exposure.
- Final acceptance is one REAL self-test: our own Vast key, the cheapest
  interruptible box on the book (cents/hour), placed and destroyed by the
  new path. Real money, thus its own operator sign-off at the time.

## Not in v1

Key storage, unified billing, fee > 0, multi-instance, fleets, resale,
DataCrunch/Akash execution, auto-anything.
