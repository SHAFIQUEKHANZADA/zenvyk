"""Tenant authentication + plan resolution for each request.

Order of resolution:
  1. If Supabase isn't configured -> enforcement OFF (dev/open mode): every
     request is treated as an unlimited admin so existing clients keep working.
  2. Admin bypass: key == ADMIN_API_KEY -> unlimited enterprise tenant.
  3. Otherwise the key is looked up in Supabase api_keys; unknown/revoked -> 401.
     The user's plan comes from profiles.plan (default 'free').

Raises PlanError (rendered as a precise JSON body by main.py) on failure.
Never logs the key or the service-role secret.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import httpx
from fastapi import Request

from app import models_config, supabase_client


class PlanError(Exception):
    """Auth/quota failure carrying an exact JSON body + HTTP status."""

    def __init__(self, status_code: int, body: dict[str, Any]):
        self.status_code = status_code
        self.body = body
        super().__init__(body.get("message", "plan error"))


@dataclass
class Tenant:
    user_id: str
    plan: str
    is_admin: bool = False


def _extract_key(request: Request) -> Optional[str]:
    """Pull the API key from `Authorization: Bearer <k>` or `x-api-key`."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    x_key = request.headers.get("x-api-key")
    if x_key:
        return x_key.strip()
    return None


async def resolve_tenant(request: Request) -> Tenant:
    """Authenticate the request and return its Tenant, or raise PlanError(401)."""
    # 1. Enforcement disabled until Supabase is configured (keeps dev/dashboard working).
    if not supabase_client.is_configured():
        return Tenant(user_id="dev", plan="enterprise", is_admin=True)

    api_key = _extract_key(request)
    if not api_key:
        raise PlanError(
            401,
            {
                "error": "missing_api_key",
                "message": "Provide an API key via 'Authorization: Bearer <key>' or 'x-api-key'.",
            },
        )

    # 2. Admin bypass.
    if models_config.ADMIN_API_KEY and api_key == models_config.ADMIN_API_KEY:
        return Tenant(user_id="admin", plan="enterprise", is_admin=True)

    # 3. Resolve the token: a Supabase login JWT (dashboard) OR an API key.
    #    Supabase access tokens are JWTs and start with 'eyJ'.
    try:
        user_id: Optional[str] = None
        if api_key.startswith("eyJ"):
            user_id = await supabase_client.resolve_access_token(api_key)
        if user_id is None:
            user_id = await supabase_client.resolve_api_key(api_key)
    except httpx.HTTPError:
        raise PlanError(
            503,
            {
                "error": "auth_unavailable",
                "message": "Authentication backend is temporarily unavailable. Try again shortly.",
            },
        )

    if not user_id:
        raise PlanError(
            401,
            {
                "error": "invalid_api_key",
                "message": "Unknown or revoked API key.",
            },
        )

    try:
        plan = await supabase_client.get_user_plan(user_id)
    except httpx.HTTPError:
        plan = "free"  # fail safe to the most restrictive plan

    return Tenant(user_id=user_id, plan=plan, is_admin=False)
