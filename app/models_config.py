"""Central configuration: which models to query and the verdict thresholds.

All values are overridable via environment variables (see .env.example).
"""
from __future__ import annotations

import math
import os

from dotenv import load_dotenv

load_dotenv()


def _get_models() -> list[str]:
    raw = os.getenv(
        "GUARDIAN_MODELS",
        "gpt-4o-mini,claude-3-5-sonnet-latest,xai/grok-2-latest,"
        "gemini/gemini-2.0-flash,groq/llama-3.3-70b-versatile",
    )
    return [m.strip() for m in raw.split(",") if m.strip()]


# Models to fan out to (target 3-5). Configurable via GUARDIAN_MODELS.
GUARDIAN_MODELS: list[str] = _get_models()

# --- Consensus (semantic drift) thresholds, cosine similarity in [0, 1] ---
CONSENSUS_PASS_THRESHOLD: float = float(os.getenv("CONSENSUS_PASS_THRESHOLD", "0.70"))
CONSENSUS_BLOCK_THRESHOLD: float = float(os.getenv("CONSENSUS_BLOCK_THRESHOLD", "0.50"))

# --- NLI entailment vote fraction for PASS (ceil of this * total) ---
ENTAIL_PASS_FRACTION: float = float(os.getenv("ENTAIL_PASS_FRACTION", "0.60"))

# --- HF model ids (loaded once at startup in guardian.py) ---
NLI_MODEL: str = os.getenv("NLI_MODEL", "typeform/distilbert-base-uncased-mnli")
EMBED_MODEL: str = os.getenv("EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

# --- Server ---
PORT: int = int(os.getenv("PORT", "8000"))


def _get_cors_origins() -> list[str]:
    # Comma-separated allowed origins for the browser dashboard. Use "*" to
    # allow all (note: "*" disables credentialed requests per the CORS spec).
    raw = os.getenv("GUARDIAN_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
    return [o.strip() for o in raw.split(",") if o.strip()]


CORS_ORIGINS: list[str] = _get_cors_origins()

# Regex allowing any zenvyk.com subdomain over https (e.g. guardian.zenvyk.com).
# Applied in addition to CORS_ORIGINS. Override via GUARDIAN_CORS_ORIGIN_REGEX.
CORS_ORIGIN_REGEX: str = os.getenv(
    "GUARDIAN_CORS_ORIGIN_REGEX", r"https://([a-z0-9-]+\.)?zenvyk\.com"
)


def entail_pass_threshold(total: int) -> int:
    """Minimum entail votes required for PASS given `total` responses."""
    return math.ceil(ENTAIL_PASS_FRACTION * total)
