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
- `app/guardian.py` — `guardian_filter`: consensus + NLI + drift + verdict. Loads ML models at import.
- `app/models_config.py` — model list + thresholds (env-overridable).
- `app/store.py` — in-memory log + `get_stats()`.
- `app/schemas.py` — Pydantic I/O models.
