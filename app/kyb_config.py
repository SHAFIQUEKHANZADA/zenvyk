"""Guardian KYB (Know Your Business) — config for the business-verification module.

Mirrors the AI-consensus idea but over authoritative BUSINESS data sources:
query 5 independent sources in parallel, require a majority (3-of-5) match on
key fields, produce a trust score + decision (AUTO_APPROVE / FLAG / REJECT).

Real sources activate when their API key env var is set (see kyb_sources.py);
until then the module runs in clearly-labeled SAMPLE mode.
"""
from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# The five sources, with a trust weight each (matches the reference mock).
KYB_SOURCES: list[dict] = [
    {"key": "middesk", "name": "Middesk", "weight": 1.0},
    {"key": "opencorporates", "name": "OpenCorporates", "weight": 0.8},
    {"key": "tin", "name": "TIN authority", "weight": 0.9},
    {"key": "google_places", "name": "Google Places", "weight": 0.6},
    {"key": "website", "name": "Website", "weight": 0.5},
]

# Core fields a source is scored on (name/address/EIN are the matchable trio).
CORE_FIELDS: tuple[str, ...] = ("name", "address", "ein")

# Consensus + decision thresholds.
CONSENSUS_MIN: int = int(_f("KYB_CONSENSUS_MIN", 3))          # sources that must AGREE
TRUST_APPROVE: float = _f("KYB_TRUST_APPROVE", 80.0)          # min trust to auto-approve
TRUST_FLAG: float = _f("KYB_TRUST_FLAG", 50.0)               # below this + no consensus -> reject

# --- Real-source credentials (server-side only; sample mode until set) --------
MIDDESK_API_KEY: str = os.getenv("MIDDESK_API_KEY", "").strip()
MIDDESK_BASE: str = os.getenv("MIDDESK_BASE", "https://api.middesk.com").rstrip("/")
OPENCORPORATES_API_TOKEN: str = os.getenv("OPENCORPORATES_API_TOKEN", "").strip()
GOOGLE_PLACES_API_KEY: str = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()

# Presentation mode: force clearly-labeled sample output for pitch screenshots.
PRESENTATION_MODE: bool = os.getenv("KYB_PRESENTATION_MODE", "false").lower() in (
    "1",
    "true",
    "yes",
)

# ---------------------------------------------------------------------------
# SAMPLE scenarios (used when a source has no key, or ?demo=1). Each maps a
# source key -> the fields it "matched" + a source verdict. Clearly labeled.
# ---------------------------------------------------------------------------
def _agree(name=True, address=True, ein=True, status="active", watchlist="clear") -> dict:
    return {"name": name, "address": address, "ein": ein, "status": status, "watchlist": watchlist}


SAMPLE_SCENARIOS: dict[str, dict] = {
    # Clean approve — all sources line up.
    "clean": {
        "middesk": {"verdict": "AGREES", "fields": _agree()},
        "opencorporates": {"verdict": "AGREES", "fields": _agree(ein=False)},
        "tin": {"verdict": "AGREES", "fields": {"name": True, "ein": True}},
        "google_places": {"verdict": "AGREES", "fields": {"name": True, "address": True}},
        "website": {"verdict": "PARTIAL", "fields": {"name": True, "address": False}},
    },
    # Consensus short — only 2 clear agreements, routes to a human.
    "short": {
        "middesk": {"verdict": "AGREES", "fields": _agree()},
        "opencorporates": {"verdict": "PARTIAL", "fields": _agree(address=False, ein=False)},
        "tin": {"verdict": "AGREES", "fields": {"name": True, "ein": True}},
        "google_places": {"verdict": "PARTIAL", "fields": {"name": True, "address": False}},
        "website": {"verdict": "NO_MATCH", "fields": {"name": False}},
    },
    # Watchlist hit — sanctions/watchlist match -> reject.
    "watchlist": {
        "middesk": {"verdict": "AGREES", "fields": _agree(watchlist="hit")},
        "opencorporates": {"verdict": "AGREES", "fields": _agree(ein=False)},
        "tin": {"verdict": "AGREES", "fields": {"name": True, "ein": True}},
        "google_places": {"verdict": "PARTIAL", "fields": {"name": True, "address": True}},
        "website": {"verdict": "PARTIAL", "fields": {"name": True}},
    },
}
DEFAULT_SCENARIO = "clean"
