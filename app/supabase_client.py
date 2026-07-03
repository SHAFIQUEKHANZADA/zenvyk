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


async def resolve_access_token(token: str) -> Optional[str]:
    """Return the user_id for a Supabase login (JWT) access token, or None.

    Lets the dashboard authenticate with the logged-in user's session token
    instead of a separate API key.
    """
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{models_config.SUPABASE_URL.rstrip('/')}/auth/v1/user",
            headers={
                "apikey": models_config.SUPABASE_SERVICE_ROLE_KEY,
                "Authorization": f"Bearer {token}",
            },
        )
    if resp.status_code != 200:
        return None
    return resp.json().get("id")


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


# ---------------------------------------------------------------------------
# Guardian Resource Intelligence (GRI) — projects / phases / checkpoints / logs
# ---------------------------------------------------------------------------
async def create_project(
    user_id: Optional[str], prompt: str, meta: dict, phases: list[dict]
) -> Optional[str]:
    """Insert a project + its phases; return the new project id."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_base()}/projects",
            headers={**_headers(), "Prefer": "return=representation"},
            json={
                "user_id": user_id if user_id and user_id not in ("dev", "admin") else None,
                "prompt": prompt[:4000],
                "status": "analyzed",
                "meta": meta,
            },
        )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return None
    project_id = rows[0]["id"]

    if phases:
        rows_to_insert = [
            {
                "project_id": project_id,
                "idx": i,
                "name": ph.get("name", f"Phase {i + 1}"),
                "status": "pending",
            }
            for i, ph in enumerate(phases)
        ]
        async with httpx.AsyncClient(timeout=10) as client:
            r2 = await client.post(
                f"{_base()}/project_phases", headers=_headers(), json=rows_to_insert
            )
        r2.raise_for_status()
    return project_id


async def get_project(project_id: str) -> Optional[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/projects",
            headers=_headers(),
            params={"select": "*", "id": f"eq.{project_id}", "limit": "1"},
        )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


async def get_phases(project_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/project_phases",
            headers=_headers(),
            params={"select": "*", "project_id": f"eq.{project_id}", "order": "idx.asc"},
        )
    resp.raise_for_status()
    return resp.json()


async def set_phase_status(project_id: str, idx: int, status: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.patch(
            f"{_base()}/project_phases",
            headers=_headers(),
            params={"project_id": f"eq.{project_id}", "idx": f"eq.{idx}"},
            json={"status": status},
        )
    resp.raise_for_status()


async def set_project_status(project_id: str, status: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.patch(
            f"{_base()}/projects",
            headers=_headers(),
            params={"id": f"eq.{project_id}"},
            json={"status": status},
        )
    resp.raise_for_status()


async def save_checkpoint(project_id: str, phase_idx: int, output: Any) -> str:
    """Upsert a checkpoint for (project, phase). Returns saved_at."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_base()}/checkpoints",
            headers={
                **_headers(),
                "Prefer": "resolution=merge-duplicates,return=representation",
            },
            json={"project_id": project_id, "phase_idx": phase_idx, "output": output},
        )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0]["saved_at"] if rows else ""


async def get_last_checkpoint(project_id: str) -> Optional[dict]:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/checkpoints",
            headers=_headers(),
            params={
                "select": "*",
                "project_id": f"eq.{project_id}",
                "order": "phase_idx.desc",
                "limit": "1",
            },
        )
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if rows else None


async def log_execution(
    *,
    user_id: Optional[str],
    project_id: Optional[str],
    provider: str,
    phase_idx: int,
    tokens: int,
    cost_usd: float,
    success: bool,
    month: str,
) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{_base()}/execution_logs",
            headers=_headers(),
            json={
                "user_id": user_id if user_id and user_id not in ("dev", "admin") else None,
                "project_id": project_id,
                "provider": provider,
                "phase_idx": phase_idx,
                "tokens": tokens,
                "cost_usd": cost_usd,
                "success": success,
                "month": month,
            },
        )
    resp.raise_for_status()


async def provider_spend(month: str) -> dict[str, float]:
    """Sum cost_usd per provider for a given month (for remaining-budget calc)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/execution_logs",
            headers=_headers(),
            params={"select": "provider,cost_usd", "month": f"eq.{month}"},
        )
    resp.raise_for_status()
    totals: dict[str, float] = {}
    for row in resp.json():
        prov = row.get("provider")
        if prov:
            totals[prov] = totals.get(prov, 0.0) + float(row.get("cost_usd") or 0.0)
    return totals


async def execution_summary() -> dict:
    """Return {avg_success, queued} for the dashboard (best-effort)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/execution_logs",
            headers=_headers(),
            params={"select": "success", "limit": "1000"},
        )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return {"avg_success": None}
    ok = sum(1 for r in rows if r.get("success"))
    return {"avg_success": round(ok / len(rows), 3)}


async def count_queued_projects() -> int:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{_base()}/projects",
            headers={**_headers(), "Prefer": "count=exact"},
            params={"select": "id", "status": "in.(analyzed,running,queued)", "limit": "1"},
        )
    resp.raise_for_status()
    content_range = resp.headers.get("content-range", "")
    if "/" in content_range:
        try:
            return int(content_range.split("/")[-1])
        except ValueError:
            return 0
    return len(resp.json())
