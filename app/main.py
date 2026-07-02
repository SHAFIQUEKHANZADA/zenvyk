"""Zenvyk Guardian — FastAPI app + routes.

POST /v1/verify             — direct verification (verdict + scores)
POST /v1/chat/completions   — OpenAI-compatible drop-in verifying proxy
GET  /health                — liveness
GET  /stats                 — in-memory running totals

Plan enforcement (auth + quota + feature gating) activates once Supabase env
vars are set; see app/auth.py and app/plans.py.
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import models_config, plans, store, supabase_client
from app.auth import PlanError, Tenant, resolve_tenant
from app.llm import get_responses
from app.schemas import (
    ChatCompletionRequest,
    VerifyRequest,
    VerifyResponse,
)

# Importing guardian loads the NLI + embedding models ONCE at startup.
from app.guardian import guardian_filter

app = FastAPI(title="Zenvyk Guardian", version="0.1.0")


@app.exception_handler(PlanError)
async def _plan_error_handler(request: Request, exc: PlanError) -> JSONResponse:
    """Render auth/quota failures with the exact JSON body they carry."""
    return JSONResponse(status_code=exc.status_code, content=exc.body)

# Allow the browser dashboard (different origin) to call this API.
# Origins are configurable via GUARDIAN_CORS_ORIGINS (comma-separated).
app.add_middleware(
    CORSMiddleware,
    allow_origins=models_config.CORS_ORIGINS,
    allow_origin_regex=models_config.CORS_ORIGIN_REGEX,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-guardian-verdict", "x-guardian-consensus-score", "x-guardian-agreement"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _guarded_verify(prompt: str, tenant: Tenant) -> dict:
    """Enforce the tenant's plan, run verification, meter usage, attach meta.

    - Quota: 402 if the tenant is at/over their monthly request limit.
    - Feature gating: free -> single model, no NLI/drift; pro/ent -> full ensemble.
    - Usage: incremented atomically only on a successful verification.
    """
    plan_cfg = plans.get_plan(tenant.plan)
    limit = plan_cfg["requests_per_month"]
    month = plans.current_month()

    # --- Quota check (enterprise/admin = unlimited) ---
    used = 0
    meter = supabase_client.is_configured() and not tenant.is_admin
    if meter and limit is not None:
        used = await supabase_client.get_usage(tenant.user_id, month)
        if used >= limit:
            raise PlanError(
                402,
                {
                    "error": "quota_exceeded",
                    "message": "Monthly limit reached. Upgrade to Pro at /pricing.",
                    "plan": tenant.plan,
                },
            )

    # --- Feature-gated fan-out + filter ---
    models = models_config.GUARDIAN_MODELS[: plan_cfg["models"]]
    responses = await get_responses(prompt, models)
    result = guardian_filter(responses, nli=plan_cfg["nli"], drift=plan_cfg["drift"])
    store.record(prompt, result)

    # --- Meter usage on success (atomic increment) ---
    used_now = used + 1
    if meter:
        try:
            used_now = await supabase_client.increment_usage(tenant.user_id, month)
        except Exception:  # noqa: BLE001 - never fail the request over metering
            used_now = used + 1

    result["plan"] = tenant.plan
    result["usage"] = {"used": used_now, "limit": limit}
    return result


def _extract_prompt(messages: list) -> str:
    """Pull the latest user message text from an OpenAI-style message list."""
    for msg in reversed(messages):
        if msg.role == "user":
            content = msg.content
            if isinstance(content, str):
                return content
            # content-parts form: list of {type, text}
            if isinstance(content, list):
                parts = [
                    p.get("text", "")
                    for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                ]
                return "\n".join(parts).strip()
    # Fallback: last message of any role.
    if messages:
        last = messages[-1].content
        return last if isinstance(last, str) else str(last)
    return ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/stats")
async def stats() -> dict:
    return store.get_stats()


@app.post("/v1/verify", response_model=VerifyResponse)
async def verify(req: VerifyRequest, request: Request) -> dict:
    tenant = await resolve_tenant(request)
    return await _guarded_verify(req.prompt, tenant)


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request) -> Response:
    """OpenAI-compatible drop-in. Verifies, then returns the OpenAI shape."""
    tenant = await resolve_tenant(request)
    prompt = _extract_prompt(req.messages)
    result = await _guarded_verify(prompt, tenant)

    verdict = result["verdict"]
    if verdict == "BLOCKED":
        content = (
            "[Zenvyk Guardian] This response was BLOCKED: the verification layer "
            "could not confirm it (low consensus or failed entailment). "
            "Please rephrase or try again."
        )
    else:
        content = result["response"]

    created = int(time.time())
    body = {
        "id": f"guardian-{created}",
        "object": "chat.completion",
        "created": created,
        "model": req.model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        # Custom fields surfacing the guardian verdict.
        "x_guardian_verdict": verdict,
        "x_guardian_consensus_score": result["consensus_score"],
        "x_guardian_agreement": result["agreement"],
        "x_guardian_plan": result.get("plan"),
        "x_guardian_usage": result.get("usage"),
    }
    return JSONResponse(
        content=body,
        headers={
            "x-guardian-verdict": verdict,
            "x-guardian-consensus-score": str(result["consensus_score"]),
            "x-guardian-agreement": result["agreement"],
        },
    )
