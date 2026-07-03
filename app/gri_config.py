"""Guardian Resource Intelligence (GRI) — tunable config.

All numbers here are estimation constants for the "flight plan before takeoff"
engine. They are deliberately conservative and env-overridable so they can be
tuned without code changes. NONE of this reads a user's personal consumer-app
quota (impossible); it models work run THROUGH Guardian's own provider keys and
the user's real Guardian plan quota (see app/plans.py + Supabase usage).
"""
from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# --- Presentation mode: clearly-labeled illustrative numbers for pitch decks ---
# Real users always see real data. Toggle globally via env, or per-request ?demo=1.
PRESENTATION_MODE: bool = os.getenv("PRESENTATION_MODE", "false").lower() in (
    "1",
    "true",
    "yes",
)

# --- Token estimation ---------------------------------------------------------
# Rough tokens a finished deliverable of each type consumes end-to-end (drafting,
# revising, verifying across the ensemble). Tunable per deployment.
DELIVERABLE_PROFILES: dict[str, int] = {
    "slide_deck": int(_f("GRI_TOKENS_SLIDE_DECK", 900_000)),
    "business_plan": int(_f("GRI_TOKENS_BUSINESS_PLAN", 750_000)),
    "grant_package": int(_f("GRI_TOKENS_GRANT_PACKAGE", 600_000)),
    "investor_workbook": int(_f("GRI_TOKENS_INVESTOR_WORKBOOK", 500_000)),
    "generic_doc": int(_f("GRI_TOKENS_GENERIC_DOC", 150_000)),
}

# Keyword -> deliverable type, used to auto-detect deliverables from the prompt.
DELIVERABLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "slide_deck": ("slide", "deck", "presentation", "pitch deck", "powerpoint", "keynote"),
    "business_plan": ("business plan", "biz plan", "go-to-market", "gtm plan", "strategy doc"),
    "grant_package": ("grant", "rfp", "proposal package", "funding application"),
    "investor_workbook": ("investor", "workbook", "financial model", "cap table", "projections"),
    "generic_doc": ("document", "report", "essay", "article", "summary", "memo", "brief", "whitepaper"),
}

# Human-friendly labels for the UI.
DELIVERABLE_LABELS: dict[str, str] = {
    "slide_deck": "Slide deck",
    "business_plan": "Business plan",
    "grant_package": "Grant package",
    "investor_workbook": "Investor workbook",
    "generic_doc": "Document",
}

# Average tokens consumed per AI call -> converts a token estimate into #calls,
# which is what the Guardian plan quota (requests/month) actually meters.
TOKENS_PER_CALL: int = int(_f("GRI_TOKENS_PER_CALL", 8_000))

# Effective end-to-end throughput (tokens/second) across the ensemble, used to
# turn a token estimate into a wall-clock runtime estimate.
THROUGHPUT_TOKENS_PER_SEC: float = _f("GRI_THROUGHPUT_TPS", 3_500)

# --- Pricing: blended USD per 1K tokens per provider (in+out average) ----------
PRICING: dict[str, float] = {
    "openai": _f("GRI_PRICE_OPENAI", 0.005),
    "anthropic": _f("GRI_PRICE_ANTHROPIC", 0.006),
    "google": _f("GRI_PRICE_GOOGLE", 0.002),
    "xai": _f("GRI_PRICE_XAI", 0.004),
    "groq": _f("GRI_PRICE_GROQ", 0.001),
}
# Blended price used for the headline cost figure (median-ish of the above).
BLENDED_PRICE_PER_1K: float = _f("GRI_PRICE_BLENDED", 0.004)

# --- Guardian's OWN monthly budget per provider (USD) --------------------------
# This is Guardian's spend cap on each provider key — NOT a user's subscription.
PROVIDER_BUDGETS: dict[str, float] = {
    "openai": _f("GRI_BUDGET_OPENAI", 500.0),
    "anthropic": _f("GRI_BUDGET_ANTHROPIC", 500.0),
    "google": _f("GRI_BUDGET_GOOGLE", 300.0),
    "xai": _f("GRI_BUDGET_XAI", 200.0),
    "groq": _f("GRI_BUDGET_GROQ", 150.0),
}

# Provider display metadata + a concrete model to ping for health.
# `name` is the consumer-facing brand shown in the comparison cards.
PROVIDERS: list[dict[str, str]] = [
    {"key": "openai", "name": "ChatGPT", "model": "gpt-4o-mini"},
    {"key": "anthropic", "name": "Claude", "model": "anthropic/claude-haiku-4-5-20251001"},
    {"key": "google", "name": "Gemini", "model": "gemini/gemini-2.5-flash"},
    {"key": "xai", "name": "Grok", "model": "xai/grok-3"},
    {"key": "groq", "name": "Llama", "model": "groq/llama-3.3-70b-versatile"},
]

# Composite provider-score weights (must sum to 1.0).
SCORE_WEIGHTS: dict[str, float] = {
    "cost": _f("GRI_W_COST", 0.25),
    "speed": _f("GRI_W_SPEED", 0.20),
    "capacity": _f("GRI_W_CAPACITY", 0.35),
    "reliability": _f("GRI_W_RELIABILITY", 0.20),
}

# Default provider when nothing else is selected.
DEFAULT_PROVIDER: str = os.getenv("GRI_DEFAULT_PROVIDER", "openai")

# Bound the real work a single /execute phase call generates (keeps cost sane).
PHASE_MAX_OUTPUT_TOKENS: int = int(_f("GRI_PHASE_MAX_OUTPUT_TOKENS", 1_200))

# Health ping cache TTL (seconds) so /provider/status doesn't hammer providers.
HEALTH_CACHE_TTL_SEC: float = _f("GRI_HEALTH_TTL", 60.0)
