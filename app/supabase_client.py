"""Thin async Supabase (PostgREST) client for auth + usage metering.

Uses the service-role key server-side only. Talks to three tables:
  api_keys(user_id, key, revoked)
  profiles(id, plan)
  usage(user_id, month, count)   + rpc increment_usage(p_user_id, p_month)

Nothing here logs API keys or the service-role secret.
"""
from __future__ import annotations

from typing import Optional

import httpx

from app import models_config


def is_configured() -> bool:
    """True only when both Supabase env vars are present -> enforcement is ON."""
    return bool(models_config.SUPABASE_URL and models_config.SUPABASE_SERVICE_ROLE_KEY)


def _headers() -> dict[str, str]:
    key = models_config.SUPABASE_SERVICE_ROLE_KEY
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _base() -> str:
    return models_config.SUPABASE_URL.rstrip("/") + "/rest/v1"


async def resolve_api_key(api_key: str) -> Optional[str]:
    """Return the user_id owning a non-revoked api key, or None if unknown/revoked."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/api_keys",
            headers=_headers(),
            params={
                "select": "user_id,revoked",
                "key": f"eq.{api_key}",
                "limit": "1",
            },
        )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    row = rows[0]
    if row.get("revoked"):
        return None
    return row.get("user_id")


async def get_user_plan(user_id: str) -> str:
    """Return the user's plan from profiles.plan, defaulting to 'free'."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/profiles",
            headers=_headers(),
            params={"select": "plan", "id": f"eq.{user_id}", "limit": "1"},
        )
    resp.raise_for_status()
    rows = resp.json()
    if rows and rows[0].get("plan"):
        return rows[0]["plan"]
    return "free"


async def get_usage(user_id: str, month: str) -> int:
    """Return the request count for a user in a given month (0 if none yet)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/usage",
            headers=_headers(),
            params={
                "select": "count",
                "user_id": f"eq.{user_id}",
                "month": f"eq.{month}",
                "limit": "1",
            },
        )
    resp.raise_for_status()
    rows = resp.json()
    return int(rows[0]["count"]) if rows else 0


async def increment_usage(user_id: str, month: str) -> int:
    """Atomically increment and return the user's monthly count via an RPC.

    Requires the SQL function public.increment_usage (see supabase_schema.sql).
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_base()}/rpc/increment_usage",
            headers=_headers(),
            json={"p_user_id": user_id, "p_month": month},
        )
    resp.raise_for_status()
    return int(resp.json())
