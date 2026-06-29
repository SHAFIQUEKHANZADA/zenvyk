# ZENVYK GUARDIAN — Build Specification (Claude Code)

> An AI Runtime Verification Proxy. It sits in front of LLMs, sends each prompt to multiple models, and verifies every AI output for **hallucination** and **drift** before returning it — producing a **PASS / FLAGGED / BLOCKED** verdict via multi-model consensus.
>
> This document is the source of truth for building the backend MVP. It is written for **Claude Code**. Deploy target: **Railway (free tier)**.

---

## 1. PRODUCT OVERVIEW

**Name:** Zenvyk Guardian
**What it is:** A drop-in, OpenAI-API-compatible proxy that verifies AI outputs in real time.
**Who uses it:** Any team running LLMs that needs to catch hallucinations/drift before outputs reach users.

**Core idea:**
1. A request comes in (a prompt).
2. Guardian fans it out to **multiple LLMs** (target: 3–5 models) via **LiteLLM**.
3. The **guardian_filter** verifies the responses:
   - **Multi-model consensus** — do the models agree?
   - **NLI claim checking** — break the answer into claims and check entailment (catches hallucinations).
   - **Semantic drift** — cosine-similarity embeddings flag anomalous/divergent answers.
4. Returns a **verdict (PASS / FLAGGED / BLOCKED)** + the verified response + scores.

**Drop-in design:** apps using OpenAI's API can point to Guardian by changing only the `base_url` — no other code changes.

---

## 2. SCOPE

### ✅ In scope (Week-1 MVP — build this)
- FastAPI server
- LiteLLM multi-model proxy (fan-out to N models)
- `guardian_filter`:
  - Multi-model **consensus** vote (3-of-5 style, configurable)
  - **NLI** entailment/claim check using a DistilBERT MNLI model (Hugging Face)
  - **Semantic drift** via `all-MiniLM-L6-v2` cosine similarity
- Verdict logic: PASS / FLAGGED / BLOCKED
- Two endpoints:
  - `POST /v1/chat/completions` — **OpenAI-compatible** drop-in proxy (verifies, then returns)
  - `POST /v1/verify` — direct verification (returns verdict + scores)
- `GET /health`
- In-memory request log + a `GET /stats` summary (powers the dashboard later)
- Config via environment variables
- Dockerfile / Railway deploy config

### ❌ Out of scope (later phases — do NOT build now)
- The dashboard UI (separate project)
- Database persistence (in-memory is fine for MVP)
- Auth / API keys management UI
- Blockchain audit logs, EthicalRevoke, data-center crawler (Phase 2+)
- Sub-200ms latency optimization (MVP correctness first)

---

## 3. TECH STACK
- **Language:** Python 3.11+
- **Framework:** FastAPI + Uvicorn
- **LLM routing:** LiteLLM (`litellm`)
- **NLI model:** `typeform/distilbert-base-uncased-mnli` (Hugging Face `transformers`) — DistilBERT MNLI for entailment/contradiction
- **Embeddings (drift):** `sentence-transformers/all-MiniLM-L6-v2`
- **Validation:** Pydantic
- **Config:** `python-dotenv`
- **Deploy:** Railway (free tier); Dockerfile provided

---

## 4. ARCHITECTURE

```
Client (any OpenAI-compatible app)
        │  base_url → Guardian
        ▼
┌─────────────────────────────────────────────┐
│  FastAPI (main.py)                           │
│   POST /v1/chat/completions  (drop-in)       │
│   POST /v1/verify                            │
│   GET  /health  ·  GET /stats                │
└───────────────┬─────────────────────────────┘
                ▼
        ┌──────────────────┐   fan-out (async)
        │  LiteLLM proxy   │ ─────────────► [GPT-4o-mini, Claude, Gemini, ...]
        └────────┬─────────┘   N model responses
                 ▼
        ┌──────────────────────────────┐
        │  guardian_filter (guardian.py)│
        │   • consensus vote            │
        │   • NLI entailment (DistilBERT)│
        │   • semantic drift (MiniLM)   │
        └────────┬─────────────────────┘
                 ▼
        PASS / FLAGGED / BLOCKED  + verified response + scores
```

---

## 5. PROJECT STRUCTURE

```
zenvyk-guardian/
├── app/
│   ├── main.py            # FastAPI app + routes
│   ├── guardian.py        # guardian_filter: consensus + NLI + drift
│   ├── llm.py             # LiteLLM fan-out (async multi-model calls)
│   ├── models_config.py   # which models to query, thresholds
│   ├── store.py           # in-memory request log + stats
│   └── schemas.py         # Pydantic request/response models
├── requirements.txt
├── Dockerfile
├── railway.json           # or Procfile
├── .env.example
├── README.md
└── CLAUDE.md
```

---

## 6. CORE LOGIC (build exactly this)

### 6.1 LiteLLM fan-out (`llm.py`)
- Async function `get_responses(prompt, models)` that calls each model concurrently via `litellm.acompletion`.
- Return a list of `{model, content, latency_ms, error?}`.
- Skip models that error; continue with the rest.

### 6.2 guardian_filter (`guardian.py`)
Load models **once at startup** (module level): the NLI pipeline + the SentenceTransformer.

**a) Consensus / drift score:**
- Embed all responses with `all-MiniLM-L6-v2`.
- Compute average pairwise **cosine similarity** = `consensus_score` (0–1). High = models agree.

**b) NLI entailment vote:**
- Pick a reference (e.g., the first / most-central response).
- For each response, run NLI (`reference` vs `candidate`) → ENTAILMENT / NEUTRAL / CONTRADICTION.
- Count `entail_votes` = responses that ENTAIL the reference.
- (Stretch: decompose the reference into atomic claims and check each — keep simple for MVP.)

**c) Verdict (configurable thresholds in `models_config.py`):**
```
total = number of responses
PASS    if entail_votes >= ceil(0.6*total) AND consensus_score >= 0.70
BLOCKED if entail_votes <= 1 OR consensus_score < 0.50
FLAGGED otherwise
```
Return:
```json
{
  "verdict": "PASS|FLAGGED|BLOCKED",
  "consensus_score": 0.93,
  "agreement": "4/5",
  "response": "<verified answer>",
  "per_model": [ { "model": "...", "entails": true, "latency_ms": 183 } ],
  "elapsed_ms": 420
}
```

### 6.3 In-memory store (`store.py`)
- Append each verification result (timestamp, model breakdown, verdict, latency).
- `get_stats()` returns totals: total_requests, pass/flagged/blocked counts + %, avg_latency, top models. (Powers the future dashboard.)

---

## 7. API ENDPOINTS

### `POST /v1/verify`
Body: `{ "prompt": "..." }` → returns the verdict object (6.2).

### `POST /v1/chat/completions` (OpenAI-compatible drop-in)
- Accept the OpenAI chat-completions request shape (`model`, `messages`, ...).
- Extract the user prompt, run the Guardian pipeline.
- If **PASS/FLAGGED** → return an OpenAI-shaped response with the verified content, plus a custom header/field `x-guardian-verdict`.
- If **BLOCKED** → return the OpenAI shape but with a safe refusal message + `x-guardian-verdict: BLOCKED`.
- Goal: an app can set `base_url` to Guardian and it "just works."

### `GET /health` → `{ "status": "ok" }`
### `GET /stats` → the in-memory stats summary.

---

## 8. ENVIRONMENT VARIABLES (`.env.example`)
```
# LLM provider keys (use whichever you have)
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GEMINI_API_KEY=

# Guardian config (optional overrides)
GUARDIAN_MODELS=gpt-4o-mini,claude-3-5-sonnet-20240620,gemini/gemini-1.5-flash
CONSENSUS_PASS_THRESHOLD=0.70
CONSENSUS_BLOCK_THRESHOLD=0.50
PORT=8000
```

---

## 9. BUILD PHASES (Claude Code: do in order, verify each)
1. **Scaffold** — project structure, `requirements.txt`, FastAPI app with `/health`. Run locally.
2. **LiteLLM fan-out** (`llm.py`) — async multi-model calls; test with 2 models.
3. **guardian.py** — load NLI + MiniLM at startup; implement consensus + NLI + verdict.
4. **/v1/verify** — wire prompt → fan-out → guardian_filter → verdict. Test with curl.
5. **/v1/chat/completions** — OpenAI-compatible drop-in wrapper.
6. **store.py + /stats** — in-memory logging + stats summary.
7. **Dockerfile + railway.json** — containerize; document Railway deploy.
8. **README** — run + deploy + curl examples.

---

## 10. CLAUDE CODE — KICKOFF PROMPT
> Paste as the first message to Claude Code in an empty repo:

```
You are building "Zenvyk Guardian", a Python FastAPI service deployed on Railway. Read ZENVYK_GUARDIAN_SPEC.md fully before writing any code — it is the source of truth.

What it does: an AI verification proxy. It takes a prompt, fans it out to multiple LLMs via LiteLLM, and verifies the outputs for hallucination (NLI via DistilBERT MNLI) and drift (cosine similarity via all-MiniLM-L6-v2), returning a PASS/FLAGGED/BLOCKED verdict.

Hard rules:
- Build ONLY the Week-1 MVP scope in Section 2. Do NOT build the dashboard UI, database, auth, or any Phase-2 features.
- Load the NLI and embedding models ONCE at startup, not per request.
- LLM calls must be async/concurrent (litellm.acompletion). Skip models that error; continue with the rest.
- Keep secrets in environment variables; never hardcode keys.
- Follow the project structure (Section 5), endpoints (Section 7), and verdict logic (Section 6.2) exactly.

Build in the phase order in Section 9. Start with Phase 1 (scaffold + /health), get it running locally, then continue.

Use placeholder API keys in .env.example and document what's needed. After Phases 1–4 (a working /v1/verify), stop and show me a working curl example before continuing.
```

---

## 11. CLAUDE.md (put at repo root)
```markdown
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
```

---

## 12. DEPLOYMENT (Railway)
1. Push repo to GitHub.
2. Railway → New Project → Deploy from GitHub.
3. Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
4. Add env vars: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` (+ optional config).
5. Deploy → get the live URL → test `/health` and `/v1/verify`.

---

## 13. ⚠️ NOTES / CAVEATS
- **Latency:** querying multiple models + running NLI takes seconds, not the marketing "<200ms." That's fine for the MVP — correctness first; optimize later (caching, smaller models, fewer models).
- **Railway free tier memory:** `torch` + models are heavy. If the build OOMs, use CPU-only torch (`torch --index-url https://download.pytorch.org/whl/cpu`) and the small MiniLM/DistilBERT models (already specified).
- **Model keys:** start with whatever providers you have (even 2 models works); the "3-of-5" is the target, configurable via `GUARDIAN_MODELS`.
- **First run** downloads the HF models (one-time, a few hundred MB).

---

## 14. DELIVERABLE
A deployed (Railway) FastAPI service where:
- `POST /v1/verify` returns a PASS/FLAGGED/BLOCKED verdict with consensus + NLI scores for any prompt,
- `POST /v1/chat/completions` works as a drop-in OpenAI-compatible verifying proxy,
- `GET /stats` returns the running totals that will feed the Guardian dashboard.

This is the Week-1 MVP from Reid's spec: *FastAPI + LiteLLM proxy with guardian_filter, DistilBERT NLI + all-MiniLM for hallucination/drift detection, deployed on Railway.*
