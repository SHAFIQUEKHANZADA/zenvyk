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
- `app/supabase_client.py` — async Supabase REST for auth + usage metering + GRI persistence.
- `supabase_schema.sql` — tables (`profiles`, `api_keys`, `usage`, + GRI: `projects`, `project_phases`, `checkpoints`, `execution_logs`) + `increment_usage` RPC.
- `app/gri.py` — GRI engine (pure): token/cost/runtime estimate, deliverable detect, complexity, quota+completion-probability, decision engine, provider scoring.
- `app/gri_config.py` — GRI tunables: DELIVERABLE_PROFILES, PRICING, PROVIDER_BUDGETS, throughput, PROVIDERS, PRESENTATION_MODE (all env-overridable).
- `app/gri_health.py` — cached provider health/latency pings (Guardian's own keys).
- `app/gri_demo.py` — Presentation Mode illustrative payloads (labeled `presentation:true`).

## Guardian Resource Intelligence (GRI) — "flight plan before takeoff"
- Analyzes a project BEFORE the AI runs: predicts completion, splits into phases,
  checkpoints so no work is lost, forecasts cost, routes across providers.
- **Honesty contract:** models work run THROUGH Guardian's OWN provider keys +
  the user's REAL Guardian plan quota (`profiles.plan` + `usage`). NEVER reads a
  user's personal consumer-app quota (ChatGPT Plus / Claude app) — impossible;
  never faked. Provider cards = Guardian's own key budgets/health, labeled so.
- **Presentation Mode:** `?demo=1` (or `PRESENTATION_MODE=1`) returns clearly
  labeled illustrative numbers for pitch screenshots. Real users always see real.
- Endpoints: `POST /v1/resource/analyze`, `POST /v1/project/execute` (runs one
  phase + auto-checkpoint), `POST /v1/project/checkpoint`, `POST /v1/project/resume`,
  `GET /v1/provider/status`, `GET /v1/resource/dashboard`.
- Quota uses the REAL `plans.py` limits (Free 10/DAY, Pro 100k/month). GRI reads
  them; it does not change them. `tiktoken` (cl100k) for token counts, char fallback.

## Plan enforcement (auth + quota + gating)
- **Off by default.** Enforcement activates ONLY when both `SUPABASE_URL` and
  `SUPABASE_SERVICE_ROLE_KEY` are set. Until then the API runs open (dev mode) so
  the dashboard keeps working without a key.
- **Auth (when on):** send `Authorization: Bearer <key>` or `x-api-key`. The key
  is looked up in `api_keys` → `user_id`; unknown/revoked → **401**. Plan comes
  from `profiles.plan` (default `free`). `ADMIN_API_KEY` = unlimited bypass.
- **Quota (per-period):** per-user count in `usage`, incremented atomically on
  each successful `/v1/verify`. **Free = 10 per DAY** (key `YYYY-MM-DD`, resets
  00:00 UTC); **Pro = 100,000 per MONTH** (key `YYYY-MM`); Enterprise unlimited.
  At/over the plan's `requests_per_period` → **402** `{"error":"quota_exceeded",
  "period":"day|month",...}` with a "resets tomorrow/next month" message. The
  usage key comes from `plans.period_key(plan)`.
- **Vagueness pre-check (`/v1/verify`):** before any fan-out, `_intent_gap` runs
  one cheap model call; if the prompt is underspecified it returns
  `status:"NEEDS_CLARIFICATION"` + `{question, options}` (no fan-out, no quota
  spent). Skipped when a document/URL is attached. Model via `CLARIFIER_MODEL`
  env (defaults to `GUARDIAN_MODELS[0]`).
- **Feature gating in `/v1/verify` + `/v1/chat/completions`:**
  - `free` → **TEMPORARILY full capability** (5-model + NLI + drift), only the
    10/day limit differs. Flip to single-model later via `plans.py` (models:1).
  - `pro`/`enterprise` → full 5-model ensemble + NLI + drift.
- **Response meta:** includes `plan` and `usage:{used,limit}` for the dashboard.
- **Secrets:** service-role key is server-side only; keys are never logged.
- ⚠️ Turning enforcement on requires the dashboard to send the user's API key on
  `/v1/verify`, or it will get 401.
