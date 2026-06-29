"""Zenvyk Guardian — FastAPI app + routes.

POST /v1/verify             — direct verification (verdict + scores)
POST /v1/chat/completions   — OpenAI-compatible drop-in verifying proxy
GET  /health                — liveness
GET  /stats                 — in-memory running totals
"""
from __future__ import annotations

import time

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import models_config, store
from app.llm import get_responses
from app.schemas import (
    ChatCompletionRequest,
    VerifyRequest,
    VerifyResponse,
)

# Importing guardian loads the NLI + embedding models ONCE at startup.
from app.guardian import guardian_filter

app = FastAPI(title="Zenvyk Guardian", version="0.1.0")

# Allow the browser dashboard (different origin) to call this API.
# Origins are configurable via GUARDIAN_CORS_ORIGINS (comma-separated).
app.add_middleware(
    CORSMiddleware,
    allow_origins=models_config.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-guardian-verdict", "x-guardian-consensus-score", "x-guardian-agreement"],
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _verify_prompt(prompt: str) -> dict:
    """Fan out a prompt to all configured models and run the guardian filter."""
    responses = await get_responses(prompt, models_config.GUARDIAN_MODELS)
    result = guardian_filter(responses)
    store.record(prompt, result)
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
async def verify(req: VerifyRequest) -> dict:
    return await _verify_prompt(req.prompt)


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest) -> Response:
    """OpenAI-compatible drop-in. Verifies, then returns the OpenAI shape."""
    prompt = _extract_prompt(req.messages)
    result = await _verify_prompt(prompt)

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
    }
    return JSONResponse(
        content=body,
        headers={
            "x-guardian-verdict": verdict,
            "x-guardian-consensus-score": str(result["consensus_score"]),
            "x-guardian-agreement": result["agreement"],
        },
    )
