# compute.pangle.online — GPU compute meta-aggregator

![live H100 spot](https://compute.pangle.online/badge.svg?gpu=H100) ![live RTX 4090 idle](https://compute.pangle.online/badge.svg?gpu=4090&offer_class=interruptible)

One process: provider pollers + SQLite store + ranked REST API + MCP server + the site.
Live at https://compute.pangle.online · API `/api/offers`, `/api/history`, `/api/idle-history`, `/api/watches` · MCP `/mcp/` (streamable HTTP).

## Run anywhere (the VPS lift)
```
docker compose up          # or:
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/uvicorn app:app --port 8956
```
No external services, no keys required (keys in `.env` deepen Vast/RunPod results; DataCrunch and Akash need none).

## The fee levers — there are two, and confusing them corrupts the data

`FEE_BPS` (`core/economics.py`) marks up **displayed** prices, and it is **0, permanently**.
The book, the tape, the index and the per-chip prices are published as THE MARKET's numbers —
check any of them against the provider's own page. A markup here would quietly make every
published price ours instead of theirs, so this lever stays off.

`EXEC_FEE_BPS` is the **platform fee**, charged on what we settle, never on what the provider
charges: **2.5%, one cent minimum, capped at $5 per lease per UTC day** (`core/meter.py`).
Renters place on their own provider key and pay the provider directly; this is the only thing
we take, and it is disclosed in the quote and the receipt before anything is placed.

`REF_VAST` / `REF_RUNPOD` referral slots ride the same seam.

## Layout
`providers/` one drop-in module per marketplace · `core/` schema, rank, economics,
stats · `db.py` SQLite (WAL) with `provider_health` honesty table · `app.py` FastAPI +
scheduler + MCP mount · `web/` the hand-written site (see DESIGN-DIRECTION.md).

Tests: `venv/bin/python -m tests.test_pipeline`

## Generated pages
`web/gpu/`, `web/vs/` and `web/sitemap.xml` are built artifacts — regenerate with
`venv/bin/python gen_gpu_pages.py` (the deployment runs it hourly).

## Fonts
Martian Mono ships in `web/fonts/` (SIL OFL). The display serif (Erode, by
Indian Type Foundry via Fontshare) is **not** redistributed here — its license
doesn't allow it; fetch it from https://www.fontshare.com/fonts/erode into
`web/fonts/` as `erode-400.woff2` / `erode-600.woff2`, or let the stack fall
back to Georgia.

## License
AGPL-3.0 — see `LICENSE`. Run it, fork it, learn from it; if you serve a
modified version, share your changes the same way.
