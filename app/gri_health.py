"""Lightweight provider health/latency pings for Guardian's OWN model keys.

A ping is a minimal (1-token) completion against each provider's cheap model,
run concurrently, timed, and cached for HEALTH_CACHE_TTL_SEC so status/analyze
calls don't hammer the providers. This reflects Guardian's key health — NOT any
end-user's personal subscription.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import litellm

from app import gri_config

# provider key -> {"health","latency_ms","checked_at"}
_cache: dict[str, dict] = {}
_lock = asyncio.Lock()


def _fresh(entry: dict, now: float) -> bool:
    return entry and (now - entry.get("checked_at", 0)) < gri_config.HEALTH_CACHE_TTL_SEC


async def _ping_one(provider: dict) -> dict:
    """Time a 1-token completion. Classify latency into health."""
    start = time.perf_counter()
    try:
        await litellm.acompletion(
            model=provider["model"],
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            timeout=8,
        )
        latency_ms = int((time.perf_counter() - start) * 1000)
        health = "healthy" if latency_ms < 2500 else "degraded"
        return {"health": health, "latency_ms": latency_ms, "checked_at": time.time()}
    except Exception:  # noqa: BLE001 - any failure => provider unavailable
        latency_ms = int((time.perf_counter() - start) * 1000)
        return {"health": "down", "latency_ms": latency_ms, "checked_at": time.time()}


async def get_health(force: bool = False) -> dict[str, dict]:
    """Return {provider_key: {health, latency_ms, checked_at}}, using the cache
    unless `force` or entries are stale."""
    now = time.time()
    async with _lock:
        stale = force or any(
            not _fresh(_cache.get(p["key"], {}), now) for p in gri_config.PROVIDERS
        )
        if stale:
            results = await asyncio.gather(
                *(_ping_one(p) for p in gri_config.PROVIDERS)
            )
            for provider, res in zip(gri_config.PROVIDERS, results):
                _cache[provider["key"]] = res
        return {k: dict(v) for k, v in _cache.items()}


def cached_health() -> dict[str, dict]:
    """Non-blocking read of whatever is cached (may be empty on cold start)."""
    return {k: dict(v) for k, v in _cache.items()}
