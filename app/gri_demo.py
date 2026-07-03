"""Presentation Mode — clearly-labeled illustrative GRI numbers for pitch decks.

Returned ONLY when ?demo=1 or PRESENTATION_MODE is set. Every payload carries
`presentation: true` so the UI can badge it. Real users never see this.
"""
from __future__ import annotations


def demo_analyze(prompt: str) -> dict:
    return {
        "complexity_score": 82,
        "estimated_tokens": 2_400_000,
        "estimated_ai_calls": 300,
        "estimated_runtime_sec": 686,
        "estimated_cost_usd": 9.6,
        "deliverables": [
            {"name": "Investor pitch deck", "type": "slide_deck", "est_tokens": 900_000, "est_runtime_sec": 257},
            {"name": "Business plan", "type": "business_plan", "est_tokens": 750_000, "est_runtime_sec": 214},
            {"name": "Grant package", "type": "grant_package", "est_tokens": 600_000, "est_runtime_sec": 171},
        ],
        "quota": {"plan": "pro", "limit": 100_000, "used": 41_200, "remaining": 58_800},
        "completion_probability": 0.86,
        "risk": "LOW",
        "recommendation": {
            "verdict": "PHASE",
            "best_provider": "Claude",
            "message": (
                "Guardian split this into 3 checkpointed phases so nothing is lost. "
                "Phase 1 runs now within your remaining capacity; Claude is the recommended engine."
            ),
            "phases": [
                {"name": "Phase 1", "deliverables": ["Investor pitch deck"], "est_tokens": 900_000, "est_runtime_sec": 257, "est_ai_calls": 113},
                {"name": "Phase 2", "deliverables": ["Business plan"], "est_tokens": 750_000, "est_runtime_sec": 214, "est_ai_calls": 94},
                {"name": "Phase 3", "deliverables": ["Grant package"], "est_tokens": 600_000, "est_runtime_sec": 171, "est_ai_calls": 75},
            ],
            "alternatives": ["REDUCE", "QUEUE", "SWITCH_PROVIDER"],
        },
        "providers": [
            {"key": "anthropic", "name": "Claude", "remaining_budget_pct": 88.0, "est_needed_pct": 2.9, "completion_probability": 0.94, "risk": "LOW", "score": 91.0, "health": "healthy", "latency_ms": 620},
            {"key": "openai", "name": "ChatGPT", "remaining_budget_pct": 74.0, "est_needed_pct": 2.4, "completion_probability": 0.9, "risk": "LOW", "score": 88.0, "health": "healthy", "latency_ms": 540},
            {"key": "google", "name": "Gemini", "remaining_budget_pct": 92.0, "est_needed_pct": 1.0, "completion_probability": 0.88, "risk": "LOW", "score": 85.0, "health": "healthy", "latency_ms": 700},
            {"key": "xai", "name": "Grok", "remaining_budget_pct": 61.0, "est_needed_pct": 2.0, "completion_probability": 0.72, "risk": "MEDIUM", "score": 76.0, "health": "degraded", "latency_ms": 2600},
        ],
        "project_id": None,
        "presentation": True,
    }


def demo_dashboard() -> dict:
    return {
        "capacity": {"plan": "pro", "used": 41_200, "limit": 100_000, "used_pct": 41.2},
        "est_remaining_runtime_sec": 12_400,
        "projects_in_queue": 2,
        "current_provider": "Claude",
        "avg_completion_success": 0.97,
        "guardian_status": "Healthy",
        "risk": "LOW",
        "presentation": True,
    }


def demo_provider_status() -> dict:
    return {
        "providers": [
            {"key": "openai", "name": "ChatGPT", "health": "healthy", "latency_ms": 540, "remaining_budget_pct": 74.0, "budget_usd": 500.0, "spent_usd": 130.0},
            {"key": "anthropic", "name": "Claude", "health": "healthy", "latency_ms": 620, "remaining_budget_pct": 88.0, "budget_usd": 500.0, "spent_usd": 60.0},
            {"key": "google", "name": "Gemini", "health": "healthy", "latency_ms": 700, "remaining_budget_pct": 92.0, "budget_usd": 300.0, "spent_usd": 24.0},
            {"key": "xai", "name": "Grok", "health": "degraded", "latency_ms": 2600, "remaining_budget_pct": 61.0, "budget_usd": 200.0, "spent_usd": 78.0},
            {"key": "groq", "name": "Llama", "health": "healthy", "latency_ms": 300, "remaining_budget_pct": 96.0, "budget_usd": 150.0, "spent_usd": 6.0},
        ],
        "presentation": True,
    }
