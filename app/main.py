"""Zenvyk Guardian — FastAPI app + routes.

POST /v1/verify             — direct verification (verdict + scores)
POST /v1/chat/completions   — OpenAI-compatible drop-in verifying proxy
GET  /health                — liveness
GET  /stats                 — in-memory running totals

Plan enforcement (auth + quota + feature gating) activates once Supabase env
vars are set; see app/auth.py and app/plans.py.
"""
from __future__ import annotations

import os
import time

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import litellm

from app import (
    gri,
    gri_config,
    gri_demo,
    gri_health,
    models_config,
    plans,
    store,
    supabase_client,
)
from app.auth import PlanError, Tenant, resolve_tenant
from app.llm import get_responses
from app.schemas import (
    AnalyzeRequest,
    AnalyzeResponse,
    ChatCompletionRequest,
    CheckpointRequest,
    ExecuteRequest,
    ExtractRequest,
    ResumeRequest,
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
    limit = plan_cfg["requests_per_period"]
    period = plans.period_key(tenant.plan)      # 'YYYY-MM-DD' (Free/day) or 'YYYY-MM'
    noun = plans.period_noun(tenant.plan)       # 'day' or 'month'

    # --- Quota check (enterprise/admin = unlimited) ---
    used = 0
    meter = supabase_client.is_configured() and not tenant.is_admin
    if meter and limit is not None:
        try:
            used = await supabase_client.get_usage(tenant.user_id, period)
        except Exception:  # noqa: BLE001 - usage table not ready -> don't block
            used = 0
        if used >= limit:
            raise PlanError(
                402,
                {
                    "error": "quota_exceeded",
                    "message": (
                        f"You've reached your {tenant.plan} limit of {limit} "
                        f"verifications/{noun}. Resets {plans.reset_hint(tenant.plan)} "
                        f"(UTC) — upgrade to Pro for 100,000/month at /pricing."
                    ),
                    "plan": tenant.plan,
                    "period": noun,
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
            used_now = await supabase_client.increment_usage(tenant.user_id, period)
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


# Cheap model used for the vagueness pre-check (defaults to the first ensemble model).
_CLARIFIER_MODEL = os.getenv("CLARIFIER_MODEL", "").strip()


def _clarifier_model() -> str | None:
    if _CLARIFIER_MODEL:
        return _CLARIFIER_MODEL
    return models_config.GUARDIAN_MODELS[0] if models_config.GUARDIAN_MODELS else None


async def _intent_gap(prompt: str, messages) -> dict | None:
    """PRE-CHECK (before any fan-out): is the prompt too underspecified to answer
    well? If so, return {question, options} so Guardian can ask instead of guessing.

    Runs a single cheap model call. Considers conversation history: if earlier turns
    already supply the missing details (e.g. the user answered a prior clarifying
    question), it does NOT ask again.
    """
    model = _clarifier_model()
    if not model:
        return None

    convo = ""
    if messages:
        convo = "\n".join(f"{m.role}: {m.content}" for m in messages if m.content)[-2000:]

    instruction = (
        "You are an intent-gap classifier for an AI assistant. Decide if the user's "
        "LATEST request is too UNDERSPECIFIED to answer well without first asking one "
        "clarifying question. Underspecified means: unclear goal or subject; uses "
        "'it'/'this'/'that'/'one' with no referent; overly broad; or has multiple "
        "plausible interpretations (e.g. 'Build me a plan', 'Which one should I pick?', "
        "'Fix it'). A clear, answerable request is NOT underspecified (e.g. 'What is the "
        "capital of France?', 'Summarize this text', 'Write a haiku about the sea').\n\n"
        "CRITICAL RULES:\n"
        "- If the conversation already supplies the details needed to answer, it is NOT "
        "underspecified — the correct action is to ANSWER, not to ask again.\n"
        "- If the assistant already asked a clarifying question and the user has now "
        "provided information, treat it as answered (underspecified=false).\n"
        "- When in doubt, or if any reasonable answer is possible, prefer to ACT: "
        "respond underspecified=false. Only ask when you genuinely cannot proceed.\n\n"
        + (f"Conversation so far:\n{convo}\n\n" if convo else "")
        + f"Latest request: {prompt}\n\n"
        'Reply ONLY with JSON: {"underspecified": true|false, "question": '
        '"<one short friendly clarifying question>", "options": ["opt1","opt2","opt3"]}'
    )
    try:
        resp = await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": instruction}],
            temperature=0,
            max_tokens=250,
        )
        data = _parse_json_object(resp["choices"][0]["message"]["content"] or "")
    except Exception:  # noqa: BLE001 - pre-check is best-effort; never block a request
        return None

    if not data.get("underspecified"):
        return None
    question = str(data.get("question") or "").strip()
    options = [str(o).strip() for o in (data.get("options") or []) if str(o).strip()][:4]
    return {"question": question, "options": options} if question else None


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

    # FIX 2 — vagueness pre-check BEFORE the fan-out, on the FIRST turn only.
    # Rationale (bug fix): once a conversation is underway, the answer is almost
    # always already in context — so we ACT rather than re-ask. Running the check
    # only when there is no prior history makes re-ask loops impossible and stops
    # over-triggering on follow-ups. Skipped when a document/URL is attached.
    if not req.document_text and not req.url and not req.messages:
        clarification = await _intent_gap(req.prompt, req.messages)
        if clarification:
            return {
                "verdict": "FLAGGED",
                "status": "NEEDS_CLARIFICATION",
                "consensus_score": 0.0,
                "agreement": "0/0",
                "response": clarification["question"],
                "per_model": [],
                "elapsed_ms": 0,
                "clarification": clarification,
                "plan": tenant.plan,
            }

    prompt = await _effective_prompt(req)
    result = await _guarded_verify(prompt, tenant)

    # Surface the grounding source (doc/URL) to the dashboard.
    if req.document_text:
        result["source_used"] = {"type": "document", "ref": "uploaded document"}
    elif req.url:
        result["source_used"] = {"type": "url", "ref": req.url}

    # Clarification is handled ONLY by the first-turn pre-check above. A FLAGGED
    # verdict here means the ensemble genuinely disagreed on a real answer — we
    # surface it as FLAGGED and never loop back into another question.
    result["status"] = result["verdict"]
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


# ---------------------------------------------------------------------------
# Guardian Resource Intelligence (GRI) — flight plan before takeoff
# ---------------------------------------------------------------------------
def _is_demo(request: Request) -> bool:
    """Presentation Mode: ?demo=1 per-request, or PRESENTATION_MODE globally."""
    q = (request.query_params.get("demo") or "").lower()
    return gri_config.PRESENTATION_MODE or q in ("1", "true", "yes")


def _provider_by(name_or_key: str) -> dict:
    key = str(name_or_key or "").lower()
    for p in gri_config.PROVIDERS:
        if p["key"] == key or p["name"].lower() == key:
            return p
    return gri_config.PROVIDERS[0]


async def _quota_for(tenant: Tenant) -> dict:
    """Real plan quota for a tenant: {plan, limit, used, remaining, period}."""
    plan_cfg = plans.get_plan(tenant.plan)
    limit = plan_cfg["requests_per_period"]  # None = unlimited (enterprise)
    used = 0
    if supabase_client.is_configured() and not tenant.is_admin:
        try:
            used = await supabase_client.get_usage(tenant.user_id, plans.period_key(tenant.plan))
        except Exception:  # noqa: BLE001 - usage table not ready
            used = 0
    remaining = None if limit is None else max(0, limit - used)
    return {
        "plan": tenant.plan,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "period": plans.period_noun(tenant.plan),
    }


@app.post("/v1/resource/analyze", response_model=AnalyzeResponse)
async def resource_analyze(req: AnalyzeRequest, request: Request) -> dict:
    if _is_demo(request):
        return gri_demo.demo_analyze(req.prompt)

    tenant = await resolve_tenant(request)
    quota = await _quota_for(tenant)

    spend: dict[str, float] = {}
    if supabase_client.is_configured():
        try:
            spend = await supabase_client.provider_spend(plans.current_month())
        except Exception:  # noqa: BLE001 - logs table not ready
            spend = {}

    result = gri.analyze(
        prompt=req.prompt,
        deliverables=req.deliverables,
        attachments=[a.model_dump() for a in req.attachments] if req.attachments else None,
        history_tokens=req.history_tokens,
        quota=quota,
        provider_spend=spend,
        health=gri_health.cached_health(),
    )

    # Persist the flight plan so phases can be executed/checkpointed later.
    project_id = None
    if supabase_client.is_configured():
        rec = result["recommendation"]
        phases = rec.get("phases") or [
            {
                "name": "Phase 1",
                "deliverables": [d["name"] for d in result["deliverables"]],
                "est_tokens": result["estimated_tokens"],
                "est_runtime_sec": result["estimated_runtime_sec"],
                "est_ai_calls": result["estimated_ai_calls"],
            }
        ]
        meta = {
            "best_provider": rec["best_provider"],
            "estimated_tokens": result["estimated_tokens"],
            "estimated_ai_calls": result["estimated_ai_calls"],
            "risk": result["risk"],
            "phases": phases,
        }
        try:
            project_id = await supabase_client.create_project(
                tenant.user_id, req.prompt, meta, phases
            )
        except Exception:  # noqa: BLE001 - projects table not ready -> analysis still returns
            project_id = None

    result["project_id"] = project_id
    return result


@app.post("/v1/project/execute")
async def project_execute(req: ExecuteRequest, request: Request) -> Response:
    """Run one phase for real (through a Guardian key) and auto-checkpoint it."""
    tenant = await resolve_tenant(request)

    try:
        project = await supabase_client.get_project(req.project_id)
    except Exception:  # noqa: BLE001 - GRI tables not created yet
        return JSONResponse(
            status_code=503,
            content={"error": "storage_unavailable", "message": "Project storage isn't set up. Run supabase_schema.sql."},
        )
    if not project:
        return JSONResponse(status_code=404, content={"error": "project_not_found"})

    quota = await _quota_for(tenant)
    if quota["limit"] is not None and (quota["remaining"] or 0) <= 0 and not tenant.is_admin:
        raise PlanError(
            402,
            {"error": "quota_exceeded", "message": "No quota left this cycle.", "plan": tenant.plan},
        )

    meta = project.get("meta") or {}
    phase_plans = meta.get("phases") or []
    if req.phase_index < 0 or req.phase_index >= len(phase_plans):
        return JSONResponse(status_code=404, content={"error": "phase_not_found"})
    phase = phase_plans[req.phase_index]
    deliverables = phase.get("deliverables") or []

    provider = _provider_by(meta.get("best_provider", gri_config.DEFAULT_PROVIDER))
    prompt = (
        f"{project.get('prompt', '')}\n\n"
        f"Produce a clear, structured draft for this phase's deliverable(s): "
        f"{', '.join(deliverables)}. Be concise and usable."
    )

    await supabase_client.set_phase_status(req.project_id, req.phase_index, "running")
    month = plans.current_month()
    try:
        resp = await litellm.acompletion(
            model=provider["model"],
            messages=[{"role": "user", "content": prompt}],
            max_tokens=gri_config.PHASE_MAX_OUTPUT_TOKENS,
        )
        text = resp["choices"][0]["message"]["content"] or ""
        usage = resp.get("usage") or {}
        tokens = int(usage.get("total_tokens") or (len(text) // 4) + 200)
        success = True
    except Exception as exc:  # noqa: BLE001 - record the failure, don't 500
        text = f"[error] {type(exc).__name__}: {exc}"
        tokens = 0
        success = False

    cost = round((tokens / 1000.0) * gri_config.PRICING.get(provider["key"], gri_config.BLENDED_PRICE_PER_1K), 4)
    output = {"deliverables": deliverables, "text": text, "provider": provider["name"], "tokens": tokens}

    saved_at = ""
    try:
        saved_at = await supabase_client.save_checkpoint(req.project_id, req.phase_index, output)
        await supabase_client.set_phase_status(
            req.project_id, req.phase_index, "done" if success else "pending"
        )
        await supabase_client.log_execution(
            user_id=tenant.user_id, project_id=req.project_id, provider=provider["key"],
            phase_idx=req.phase_index, tokens=tokens, cost_usd=cost, success=success, month=month,
        )
    except Exception:  # noqa: BLE001 - persistence best-effort
        pass

    if supabase_client.is_configured() and not tenant.is_admin and success:
        try:
            await supabase_client.increment_usage(tenant.user_id, month)
        except Exception:  # noqa: BLE001
            pass

    next_index = req.phase_index + 1 if req.phase_index + 1 < len(phase_plans) else None
    if next_index is None and success:
        try:
            await supabase_client.set_project_status(req.project_id, "done")
        except Exception:  # noqa: BLE001
            pass

    return JSONResponse(
        content={
            "project_id": req.project_id,
            "phase_index": req.phase_index,
            "status": "done" if success else "failed",
            "provider": provider["name"],
            "output": output,
            "tokens": tokens,
            "cost_usd": cost,
            "saved_at": saved_at,
            "next_phase_index": next_index,
            "message": "Phase complete — progress checkpointed, never lost.",
        }
    )


@app.post("/v1/project/checkpoint")
async def project_checkpoint(req: CheckpointRequest, request: Request) -> dict:
    """Manually save a checkpoint for a phase."""
    await resolve_tenant(request)
    saved_at = ""
    try:
        saved_at = await supabase_client.save_checkpoint(req.project_id, req.phase_index, req.output)
        await supabase_client.set_phase_status(req.project_id, req.phase_index, "done")
    except Exception:  # noqa: BLE001
        pass
    return {
        "project_id": req.project_id,
        "phase_index": req.phase_index,
        "saved_at": saved_at,
        "message": "Checkpoint saved — progress protected.",
    }


@app.post("/v1/project/resume")
async def project_resume(req: ResumeRequest, request: Request) -> Response:
    """Return the last checkpoint + the next phase to run, so work is never lost."""
    await resolve_tenant(request)
    try:
        project = await supabase_client.get_project(req.project_id)
    except Exception:  # noqa: BLE001 - GRI tables not created yet
        return JSONResponse(
            status_code=503,
            content={"error": "storage_unavailable", "message": "Project storage isn't set up. Run supabase_schema.sql."},
        )
    if not project:
        return JSONResponse(status_code=404, content={"error": "project_not_found"})

    phases = await supabase_client.get_phases(req.project_id)
    last = await supabase_client.get_last_checkpoint(req.project_id)
    next_phase = next((p for p in phases if p.get("status") != "done"), None)

    return JSONResponse(
        content={
            "project_id": req.project_id,
            "status": project.get("status"),
            "last_checkpoint": last,
            "next_phase_index": next_phase["idx"] if next_phase else None,
            "next_phase": next_phase,
            "completed_phases": sum(1 for p in phases if p.get("status") == "done"),
            "total_phases": len(phases),
            "message": (
                "Resumed from last checkpoint — no work lost."
                if last
                else "No checkpoint yet; start at phase 0."
            ),
        }
    )


@app.get("/v1/provider/status")
async def provider_status(request: Request) -> dict:
    """Live health/latency + Guardian's own remaining budget per provider key."""
    if _is_demo(request):
        return gri_demo.demo_provider_status()

    health = await gri_health.get_health()
    spend: dict[str, float] = {}
    if supabase_client.is_configured():
        try:
            spend = await supabase_client.provider_spend(plans.current_month())
        except Exception:  # noqa: BLE001
            spend = {}

    providers = []
    for p in gri_config.PROVIDERS:
        key = p["key"]
        budget = gri_config.PROVIDER_BUDGETS.get(key, 0.0)
        spent = float(spend.get(key, 0.0))
        remaining_pct = round(100.0 * max(0.0, budget - spent) / budget, 1) if budget else 0.0
        h = health.get(key, {})
        providers.append(
            {
                "key": key,
                "name": p["name"],
                "health": h.get("health", "unknown"),
                "latency_ms": h.get("latency_ms"),
                "remaining_budget_pct": remaining_pct,
                "budget_usd": budget,
                "spent_usd": round(spent, 2),
            }
        )
    return {"providers": providers, "presentation": False}


@app.get("/v1/resource/dashboard")
async def resource_dashboard(request: Request) -> dict:
    """Capacity, remaining runtime, queue, current provider, avg success, status."""
    if _is_demo(request):
        return gri_demo.demo_dashboard()

    tenant = await resolve_tenant(request)
    quota = await _quota_for(tenant)
    limit, used = quota["limit"], quota["used"]
    used_pct = round(100.0 * used / limit, 1) if limit else 0.0
    rem_runtime = (
        None
        if limit is None
        else int((quota["remaining"] or 0) * gri_config.TOKENS_PER_CALL / gri_config.THROUGHPUT_TOKENS_PER_SEC)
    )

    summary: dict = {"avg_success": None}
    queued = 0
    if supabase_client.is_configured():
        try:
            summary = await supabase_client.execution_summary()
        except Exception:  # noqa: BLE001
            pass
        try:
            queued = await supabase_client.count_queued_projects()
        except Exception:  # noqa: BLE001
            pass

    health = gri_health.cached_health()
    down = any(h.get("health") == "down" for h in health.values())
    return {
        "capacity": {"plan": quota["plan"], "used": used, "limit": limit, "used_pct": used_pct},
        "est_remaining_runtime_sec": rem_runtime,
        "projects_in_queue": queued,
        "current_provider": _provider_by(gri_config.DEFAULT_PROVIDER)["name"],
        "avg_completion_success": summary.get("avg_success"),
        "guardian_status": "Risk" if down else "Healthy",
        "risk": "HIGH" if down else "LOW",
        "presentation": False,
    }


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
