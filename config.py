"""Central config. Everything tunable lives in .env — fees, referral tags,
providers, polling — so a VPS lift or a fee turn-on is config, not surgery."""
import logging
import os

try:
    from dotenv import load_dotenv
    # override=True so .env is the authority the docstring promises; an ambient
    # FEE_BPS/POLL_SECONDS must not silently beat the file on a VPS lift.
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)
except ImportError:
    logging.getLogger("config").warning(
        "python-dotenv missing — .env NOT loaded; running on ambient env only")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

PORT = int(os.environ.get("PORT", "8956"))
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "1800"))
# A provider whose newest data is older than 3 poll intervals is STALE — flagged,
# never hidden. Capped so slowing the poll cadence (to spare provider APIs) can't
# stretch the "served as fresh" window: 30 min is the honesty ceiling.
STALE_AFTER_SECONDS = min(POLL_SECONDS * 2, 3600)

PROVIDERS = [p.strip() for p in os.environ.get("PROVIDERS", "vast,runpod,datacrunch,akash,cudo,ionet,hyperstack,novita,primeintellect,shadeform").split(",") if p.strip()]

# Tiered cadence: the keyed money feeds refresh every FAST_POLL_SECONDS so the
# book (and limit-order fills / tripwires) run near-spot; keyless catalogs and
# the history tape keep the POLL_SECONDS cadence.
FAST_POLL_SECONDS = int(os.environ.get("FAST_POLL_SECONDS", "300"))
# The house account whose usage ledger is PUBLISHED at /receipts (renter-1).
# A hash can name the account but never impersonate it.
RECEIPTS_ACCOUNT_HASH = os.environ.get("RECEIPTS_ACCOUNT_HASH", "")
FAST_PROVIDERS = [p.strip() for p in os.environ.get("FAST_PROVIDERS", "vast,runpod").split(",")
                  if p.strip() and p.strip() in PROVIDERS]

# The idle index is only honest from the moment the keyed Vast walk began —
# before it, an unkeyed single page (~128 offers) massively undercounted idle
# capacity. The index clamps to this epoch rather than charting the artifact.
IDLE_INDEX_EPOCH = int(os.environ.get("IDLE_INDEX_EPOCH", "1787495227"))
# offer_history retention; the aggregates are small (~thousands/day) so two
# years is comfort, not necessity.
HISTORY_RETENTION_DAYS = int(os.environ.get("HISTORY_RETENTION_DAYS", "730"))

VAST_API_KEY = os.environ.get("VAST_API_KEY", "")
RUNPOD_API_KEY = os.environ.get("RUNPOD_API_KEY", "")
HYPERSTACK_API_KEY = os.environ.get("HYPERSTACK_API_KEY", "")
NOVITA_API_KEY = os.environ.get("NOVITA_API_KEY", "")
PRIME_INTELLECT_API_KEY = os.environ.get("PRIME_INTELLECT_API_KEY", "")
SHADEFORM_API_KEY = os.environ.get("SHADEFORM_API_KEY", "")

# Stage 2 broker: OFF by default. Flipping to 1 exposes /api/rentals + MCP rent tools.
BROKER_ENABLED = os.environ.get("BROKER_ENABLED", "0") == "1"

# THE FEE LEVER. 0 at launch, by design. See core/economics.py — the single choke point.
FEE_BPS = int(os.environ.get("FEE_BPS", "0"))
REF_VAST = os.environ.get("REF_VAST", "")
REF_RUNPOD = os.environ.get("REF_RUNPOD", "")

DB_PATH = os.environ.get("DB_PATH") or os.path.join(BASE_DIR, "data", "compute.db")

USER_AGENT = "compute.pangle.online aggregator (contact: site)"
HTTP_TIMEOUT = 20.0
