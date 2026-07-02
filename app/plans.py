"""Plan definitions and per-plan capabilities.

The single source of truth for what each plan is allowed to do. Feature gating
in the request path reads from here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, TypedDict


class PlanConfig(TypedDict):
    requests_per_month: Optional[int]  # None = unlimited
    models: int
    nli: bool
    drift: bool
    webhooks: bool
    crawler: bool


PLANS: dict[str, PlanConfig] = {
    "free": {
        "requests_per_month": 1000,
        "models": 1,
        "nli": False,
        "drift": False,
        "webhooks": False,
        "crawler": False,
    },
    "pro": {
        "requests_per_month": 100_000,
        "models": 5,
        "nli": True,
        "drift": True,
        "webhooks": True,
        "crawler": False,
    },
    "enterprise": {
        "requests_per_month": None,  # unlimited
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
    """Calendar month key in UTC, e.g. '2026-07'. Used for usage metering."""
    now = datetime.now(timezone.utc)
    return f"{now.year:04d}-{now.month:02d}"
