"""Central config loader. Reads secrets from .env (never committed)."""
import os
from pathlib import Path

# Minimal .env loader so we don't hard-depend on python-dotenv being installed.
_ENV = Path(__file__).parent / ".env"
if _ENV.exists():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        _v = _v.strip()
        # strip trailing inline comment ("VALUE   # note") — but keep a literal
        # leading '#' value untouched (there are none in practice)
        _hash = _v.find("#")
        if _hash > 0 and _v[_hash - 1] in " \t":
            _v = _v[:_hash].strip()
        os.environ.setdefault(_k.strip(), _v)

APP_ID = os.environ.get("META_APP_ID", "")
APP_SECRET = os.environ.get("META_APP_SECRET", "")
ACCESS_TOKEN = os.environ.get("META_ACCESS_TOKEN", "")

# Optional: restrict to a known set of account ids (comma-separated, e.g. "act_123,act_456").
# Leave blank to auto-discover every account the token can access.
ACCOUNT_ALLOWLIST = [
    a.strip() for a in os.environ.get("META_ACCOUNT_ALLOWLIST", "").split(",") if a.strip()
]

# Supabase / Postgres (used from Phase 1 onward; not needed for the connection test).
DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Phase 2: scheduling + alert delivery
SYNC_HOUR = int(os.environ.get("SYNC_HOUR", "6"))         # daily auto-sync hour (local)
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")  # optional Slack incoming webhook

# Phase C: sales bookings sheet
SALES_SHEET_ID = os.environ.get(
    "SALES_SHEET_ID", "")
BUDGET_SHEET_ID = os.environ.get(
    "BUDGET_SHEET_ID", "")
GOOGLE_SA_JSON = os.environ.get("GOOGLE_SA_JSON", "")     # optional service-account key path

# Hosting: bind + optional login (leave AUTH_USER blank to disable auth locally)
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))
AUTH_USER = os.environ.get("AUTH_USER", "")
AUTH_PASS = os.environ.get("AUTH_PASS", "")
