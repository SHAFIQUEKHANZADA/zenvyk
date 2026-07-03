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

import litellm

from app import models_config, plans, store, supabase_client
from app.auth import PlanError, Tenant, resolve_tenant
from app.llm import get_responses
from app.schemas import (
    ChatCompletionRequest,
    ExtractRequest,
    RouteRequest,
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
        try:
            used = await supabase_client.get_usage(tenant.user_id, month)
        except Exception:  # noqa: BLE001 - usage table not ready -> don't block
            used = 0
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


import json
import re

import httpx

# Cap grounding text so we don't blow up token usage / latency.
_MAX_SOURCE_CHARS = 12000


async def _fetch_url(url: str) -> tuple[str | None, str]:
    """Fetch a URL; return (title, crude plain-text of the page)."""
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": "ZenvykGuardian/1.0"})
    resp.raise_for_status()
    html = resp.text
    tmatch = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.S | re.I)
    title = re.sub(r"\s+", " ", tmatch.group(1)).strip() if tmatch else None
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", html)
    return title, re.sub(r"\s+", " ", text).strip()


def _parse_json_object(text: str) -> dict:
    """Best-effort extract of a JSON object from a model reply (handles fences)."""
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except Exception:  # noqa: BLE001
        return {}


async def _clarify(user_prompt: str, answers: list[str]) -> dict | None:
    """When the ensemble disagreed, ask ONE clarifying question + 2-3 options
    so the user can steer toward what they actually meant."""
    if not models_config.GUARDIAN_MODELS:
        return None
    divergent = "\n\n".join(f"Answer {i + 1}: {a[:500]}" for i, a in enumerate(answers[:4]))
    instruction = (
        "The AI models gave differing or low-confidence answers to the user's "
        "question. Ask ONE short clarifying question to pin down what the user "
        "actually wants, and offer 2-3 concrete options.\n\n"
        f"User question: {user_prompt}\n\n{divergent}\n\n"
        'Reply ONLY with JSON: {"question": "...", "options": ["...", "...", "..."]}'
    )
    try:
        resp = await litellm.acompletion(
            model=models_config.GUARDIAN_MODELS[0],
            messages=[{"role": "user", "content": instruction}],
            temperature=0,
        )
        data = _parse_json_object(resp["choices"][0]["message"]["content"] or "")
    except Exception:  # noqa: BLE001 - clarification is best-effort
        return None
    question = str(data.get("question") or "").strip()
    options = [str(o).strip() for o in (data.get("options") or []) if str(o).strip()]
    return {"question": question, "options": options[:3]} if question else None


async def _effective_prompt(req: VerifyRequest) -> str:
    """Combine conversation history + a grounding source (doc/URL) + the prompt."""
    parts: list[str] = []

    if req.messages:
        history = "\n".join(f"{m.role}: {m.content}" for m in req.messages if m.content)
        if history:
            parts.append(f"Conversation so far:\n{history}")

    source = (req.document_text or "").strip()
    if not source and req.url:
        try:
            _, source = await _fetch_url(req.url)
        except Exception:  # noqa: BLE001 - bad/unreachable URL shouldn't 500
            source = ""
    if source:
        parts.append(
            "Answer using ONLY the following source. If the answer isn't in it, "
            "say you can't find it in the source.\n\nSOURCE:\n"
            + source[:_MAX_SOURCE_CHARS]
        )

    parts.append(f"User question: {req.prompt}")
    return "\n\n".join(parts)


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
    prompt = await _effective_prompt(req)
    result = await _guarded_verify(prompt, tenant)

    # Surface the grounding source (doc/URL) to the dashboard.
    if req.document_text:
        result["source_used"] = {"type": "document", "ref": "uploaded document"}
    elif req.url:
        result["source_used"] = {"type": "url", "ref": req.url}

    # Clarifying-question flow: when the ensemble is uncertain (FLAGGED), ask the
    # user what they meant instead of just flagging a dead end.
    status = result["verdict"]
    if result["verdict"] == "FLAGGED":
        answers = [
            pm["answer"] for pm in result.get("per_model", []) if pm.get("answer")
        ]
        clarification = await _clarify(req.prompt, answers)
        if clarification:
            status = "NEEDS_CLARIFICATION"
            result["clarification"] = clarification
    result["status"] = status
    return result


@app.get("/v1/models")
async def list_models() -> dict:
    """Available models for the router dropdown."""
    return {"models": models_config.GUARDIAN_MODELS}


@app.post("/v1/extract")
async def extract(req: ExtractRequest, request: Request) -> dict:
    """Extract readable text (e.g. a pasted conversation) from a link."""
    await resolve_tenant(request)
    try:
        title, text = await _fetch_url(req.url)
    except Exception as exc:  # noqa: BLE001 - report failure, don't 500
        return {"ok": False, "text": "", "title": None, "error": f"{type(exc).__name__}: {exc}"}
    return {"ok": bool(text), "text": text, "title": title, "error": None}


@app.post("/v1/route")
async def route(req: RouteRequest, request: Request) -> dict:
    """Send content to a single chosen model (the conversation router)."""
    await resolve_tenant(request)
    try:
        resp = await litellm.acompletion(
            model=req.model,
            messages=[{"role": "user", "content": req.content}],
        )
        answer = resp["choices"][0]["message"]["content"] or ""
    except Exception as exc:  # noqa: BLE001 - surface provider errors as text
        return {"model": req.model, "answer": f"[error] {type(exc).__name__}: {exc}"}
    return {"model": req.model, "answer": answer}


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
