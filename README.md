# compute.wick.pics — GPU compute meta-aggregator

One process: provider pollers + SQLite store + ranked REST API + MCP server + the site.
Live at https://compute.wick.pics · API `/api/offers`, `/api/history`, `/api/idle-history`, `/api/watches` · MCP `/mcp/` (streamable HTTP).

## Run anywhere (the VPS lift)
```
docker compose up          # or:
python3 -m venv venv && venv/bin/pip install -r requirements.txt
venv/bin/uvicorn app:app --port 8956
```
No external services, no keys required (keys in `.env` deepen Vast/RunPod results; DataCrunch and Akash need none).

## The fee lever
`core/economics.py` is the single choke point every outbound price passes through.
`FEE_BPS=0` at launch. Turning fees on = set the env var, restart. `REF_VAST` /
`REF_RUNPOD` referral slots ride the same seam.

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
