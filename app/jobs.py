"""Background verification jobs for the Playground.

Lets `/v1/verify` run WITHOUT the browser holding the connection open: the
client submits, the server keeps working, and the result is written to a store
the client can read back when it returns (even from another device / after a
reload). This mirrors the GRI execute→checkpoint→resume pattern.

Store:
  - Durable (Supabase `verify_jobs`) when enforcement is configured — survives
    process restarts and is readable cross-device.
  - In-memory fallback for local/dev (no Supabase) so the Playground still works.

Every store call is best-effort: a storage hiccup degrades rather than 500s.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from app import supabase_client

# Dev / fallback store. Also kept as a warm cache alongside the durable store.
_MEM: dict[str, dict] = {}


async def create(user_id: Optional[str], prompt: str) -> str:
    """Create a pending job and return its id."""
    job_id = str(uuid.uuid4())
    _MEM[job_id] = {
        "id": job_id,
        "user_id": user_id,
        "status": "pending",
        "result": None,
        "error": None,
    }
    if supabase_client.is_configured():
        try:
            await supabase_client.create_verify_job(job_id, user_id, prompt)
        except Exception:  # noqa: BLE001 - durable write best-effort; memory still holds it
            pass
    return job_id


async def finish(
    job_id: str, *, result: Any = None, error: Optional[str] = None
) -> None:
    """Mark a job done (with result) or error (with message)."""
    status = "error" if error else "done"
    if job_id in _MEM:
        _MEM[job_id].update(status=status, result=result, error=error)
    if supabase_client.is_configured():
        try:
            await supabase_client.finish_verify_job(job_id, status, result, error)
        except Exception:  # noqa: BLE001
            pass


async def get(job_id: str) -> Optional[dict]:
    """Fetch a job. Prefers the durable store (cross-process/device), then memory."""
    if supabase_client.is_configured():
        try:
            row = await supabase_client.get_verify_job(job_id)
            if row is not None:
                return row
        except Exception:  # noqa: BLE001 - fall back to the warm cache
            pass
    return _MEM.get(job_id)
