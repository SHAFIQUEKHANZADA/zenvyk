"""LiteLLM multi-model fan-out.

Calls each configured model concurrently via litellm.acompletion. Models that
error are skipped (their error is recorded) so the rest can continue.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional, TypedDict

import litellm

# Don't let LiteLLM raise on provider quirks we don't care about for the MVP.
litellm.drop_params = True


class ModelResponse(TypedDict):
    model: str
    content: Optional[str]
    latency_ms: int
    error: Optional[str]


async def _call_one(prompt: str, model: str) -> ModelResponse:
    start = time.perf_counter()
    try:
        resp = await litellm.acompletion(
            model=model,
            messages=[{"role": "user", "content": prompt}],
        )
        content = resp["choices"][0]["message"]["content"]
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "model": model,
            "content": content,
            "latency_ms": latency_ms,
            "error": None,
        }
    except Exception as exc:  # noqa: BLE001 - tolerate any provider failure
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {
            "model": model,
            "content": None,
            "latency_ms": latency_ms,
            "error": f"{type(exc).__name__}: {exc}",
        }


async def get_responses(prompt: str, models: list[str]) -> list[ModelResponse]:
    """Fan out `prompt` to all `models` concurrently; return one entry each."""
    tasks = [_call_one(prompt, m) for m in models]
    return list(await asyncio.gather(*tasks))
