"""Plan definitions and per-plan capabilities.

The single source of truth for what each plan is allowed to do. Feature gating
in the request path reads from here.

Quota periods (all UTC):
  - Free  = 10 requests per DAY  (resets at 00:00 UTC), single-model, no NLI/drift.
  - Pro   = 100,000 requests per MONTH, full 5-model ensemble + NLI + drift.
  - Enterprise = unlimited.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, TypedDict


class PlanConfig(TypedDict):
    requests_per_period: Optional[int]  # None = unlimited
    period: str                         # "day" | "month"
    models: int
    nli: bool
    drift: bool
    webhooks: bool
    crawler: bool


PLANS: dict[str, PlanConfig] = {
    # Free = 10/DAY. TEMPORARY: Free currently gets the FULL paid capability
    # (5-model ensemble + NLI + drift) — only the daily limit differs. To make
    # Free single-model later, set models:1, nli:False, drift:False (this also
    # matches the pricing card's "Single-model verification" wording).
    "free": {
        "requests_per_period": 10,
        "period": "day",
        "models": 5,
        "nli": True,
        "drift": True,
        "webhooks": False,
        "crawler": False,
    },
    "pro": {
        "requests_per_period": 100_000,
        "period": "month",
        "models": 5,
        "nli": True,
        "drift": True,
        "webhooks": True,
        "crawler": False,
    },
    "enterprise": {
        "requests_per_period": None,  # unlimited
        "period": "month",
        "models": 5,
        "nli": True,
        "drift": True,
        "webhooks": True,
        "crawler": True,
    },
}

DEFAULT_PLAN = "free"


def get_plan(plan: Optional[str]) -> PlanConfig:
    """Return the PlanConfig for a plan name, falling back to free for unknowns."""
    return PLANS.get((plan or DEFAULT_PLAN).lower(), PLANS[DEFAULT_PLAN])


def current_month() -> str:
    """Calendar month key in UTC, e.g. '2026-07'. Used for monthly metering."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"


def current_day() -> str:
    """Calendar day key in UTC, e.g. '2026-07-04'. Used for daily (Free) metering."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}-{now.day:02d}"


def period_key(plan: Optional[str]) -> str:
    """The usage-table key for a plan's current period (day for Free, month else).

    Stored in the existing `usage.month` text column — day plans store 'YYYY-MM-DD',
    month plans store 'YYYY-MM'. No schema change needed.
    """
    return current_day() if get_plan(plan)["period"] == "day" else current_month()


def period_noun(plan: Optional[str]) -> str:
    """'day' or 'month' — for building quota messages."""
    return get_plan(plan)["period"]


def reset_hint(plan: Optional[str]) -> str:
    """Human phrase for when the quota resets."""
    return "tomorrow" if get_plan(plan)["period"] == "day" else "next month"
