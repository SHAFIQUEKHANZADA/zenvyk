"""In-memory request log + stats summary (powers the future dashboard).

Not persistent: cleared on restart. Good enough for the MVP.
"""
from __future__ import annotations

import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
_log: list[dict[str, Any]] = []


def record(prompt: str, result: dict[str, Any]) -> None:
    """Append one verification result to the in-memory log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prompt": prompt[:200],
        "verdict": result["verdict"],
        "consensus_score": result["consensus_score"],
        "agreement": result["agreement"],
        "elapsed_ms": result["elapsed_ms"],
        "models": [pm["model"] for pm in result["per_model"]],
    }
    with _lock:
        _log.append(entry)


def get_stats() -> dict[str, Any]:
    """Return running totals: verdict counts/%, avg latency, top models."""
    with _lock:
        entries = list(_log)

    total = len(entries)
    verdicts = Counter(e["verdict"] for e in entries)
    models = Counter(m for e in entries for m in e["models"])

    def pct(n: int) -> float:
        return round(100.0 * n / total, 1) if total else 0.0

    avg_latency = (
        round(sum(e["elapsed_ms"] for e in entries) / total, 1) if total else 0.0
    )

    return {
        "total_requests": total,
        "verdicts": {
            "PASS": verdicts.get("PASS", 0),
            "FLAGGED": verdicts.get("FLAGGED", 0),
            "BLOCKED": verdicts.get("BLOCKED", 0),
        },
        "verdict_pct": {
            "PASS": pct(verdicts.get("PASS", 0)),
            "FLAGGED": pct(verdicts.get("FLAGGED", 0)),
            "BLOCKED": pct(verdicts.get("BLOCKED", 0)),
        },
        "avg_latency_ms": avg_latency,
        "top_models": [
            {"model": m, "count": c} for m, c in models.most_common(5)
        ],
    }
