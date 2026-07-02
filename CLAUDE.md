# Zenvyk Guardian — Claude Code Guide

## What this is
A FastAPI AI-verification proxy on Railway. Takes a prompt → fans out to multiple LLMs (LiteLLM) → verifies outputs for hallucination (DistilBERT NLI) and drift (all-MiniLM cosine) → returns PASS/FLAGGED/BLOCKED. Full spec: ZENVYK_GUARDIAN_SPEC.md (read first).

## Golden rules
- Build ONLY the Week-1 MVP (Section 2). No dashboard, no DB, no auth.
- Load ML models ONCE at startup (module level), never per request.
- LLM calls are async + concurrent; tolerate individual model failures.
- Secrets via env vars only.
- Python 3.11+, type hints, Pydantic for I/O.

## Stack
FastAPI, Uvicorn, LiteLLM, transformers (typeform/distilbert-base-uncased-mnli), sentence-transformers (all-MiniLM-L6-v2). Deploy: Railway.

## Commands
- `pip install -r requirements.txt`
- `uvicorn app.main:app --reload`  (local)
- Deploy: Railway start cmd → `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## Test
POST /v1/verify {"prompt":"What is the capital of France?"} → expect verdict PASS.

## Where things live
- `app/main.py` — FastAPI app + routes.
- `app/llm.py` — LiteLLM async fan-out (`get_responses`).
- `app/guardian.py` — `guardian_filter`: consensus + NLI + drift + verdict. Loads ML models at import. `nli`/`drift` flags gate features per plan.
- `app/models_config.py` — model list + thresholds + Supabase/admin env (env-overridable).
- `app/store.py` — in-memory log + `get_stats()`.
- `app/schemas.py` — Pydantic I/O models.
- `app/plans.py` — plan table (free/pro/enterprise) + capabilities.
- `app/auth.py` — per-request tenant/plan resolution (`resolve_tenant`, `PlanError`).
- `app/supabase_client.py` — async Supabase REST for auth + usage metering.
- `supabase_schema.sql` — tables (`profiles`, `api_keys`, `usage`) + `increment_usage` RPC.

## Plan enforcement (auth + quota + gating)
- **Off by default.** Enforcement activates ONLY when both `SUPABASE_URL` and
  `SUPABASE_SERVICE_ROLE_KEY` are set. Until then the API runs open (dev mode) so
  the dashboard keeps working without a key.
- **Auth (when on):** send `Authorization: Bearer <key>` or `x-api-key`. The key
  is looked up in `api_keys` → `user_id`; unknown/revoked → **401**. Plan comes
  from `profiles.plan` (default `free`). `ADMIN_API_KEY` = unlimited bypass.
- **Quota:** monthly per-user count in `usage`, incremented atomically on each
  successful `/v1/verify`. At/over the plan's `requests_per_month` → **402**
  `{"error":"quota_exceeded",...}`. Enterprise (`null`) is never count-blocked.
- **Feature gating in `/v1/verify` + `/v1/chat/completions`:**
  - `free` → single model, NO NLI/drift (basic refusal block only).
  - `pro`/`enterprise` → full 5-model ensemble + NLI + drift.
- **Response meta:** includes `plan` and `usage:{used,limit}` for the dashboard.
- **Secrets:** service-role key is server-side only; keys are never logged.
- ⚠️ Turning enforcement on requires the dashboard to send the user's API key on
  `/v1/verify`, or it will get 401.
