"""Guardian Resource Intelligence (GRI) engine — the "flight plan before takeoff".

Pure estimation/decision logic (no network I/O) so it is fully unit-testable.
Routes in main.py fetch real quota (Supabase), provider spend (execution_logs)
and health (gri_health) and pass them in here.

Honesty contract: this models work run THROUGH Guardian's own provider keys and
the user's real Guardian plan quota. It never claims to read a user's personal
consumer-app quota (ChatGPT Plus / Claude app) — that data isn't exposed to
third parties and is never fabricated as live.
"""
from __future__ import annotations

import math
from typing import Optional

from app import gri_config

# --- token counting (tiktoken with a safe fallback) ---------------------------
try:  # pragma: no cover - depends on optional dep at runtime
    import tiktoken

    _enc = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_enc.encode(text or ""))

except Exception:  # noqa: BLE001 - fall back to a char heuristic if tiktoken absent

    def count_tokens(text: str) -> int:
        return max(1, len(text or "") // 4)


def _clamp(lo: float, hi: float, x: float) -> float:
    return max(lo, min(hi, x))


def _match_type(text: str) -> str:
    """Map a free-text deliverable name to a known profile type."""
    low = text.lower()
    for dtype, kws in gri_config.DELIVERABLE_KEYWORDS.items():
        if any(kw in low for kw in kws):
            return dtype
    return "generic_doc"


def detect_deliverables(
    prompt: str, explicit: Optional[list[str]]
) -> list[tuple[str, str]]:
    """Return [(display_name, type)]. Explicit chips win; else scan the prompt."""
    if explicit:
        return [(name, _match_type(name)) for name in explicit if name.strip()]

    low = prompt.lower()
    found: list[tuple[str, str]] = []
    for dtype, kws in gri_config.DELIVERABLE_KEYWORDS.items():
        if dtype == "generic_doc":
            continue
        if any(kw in low for kw in kws):
            found.append((gri_config.DELIVERABLE_LABELS[dtype], dtype))
    if not found:
        found = [(gri_config.DELIVERABLE_LABELS["generic_doc"], "generic_doc")]
    return found


def _runtime_sec(tokens: int) -> int:
    return int(math.ceil(tokens / max(1.0, gri_config.THROUGHPUT_TOKENS_PER_SEC)))


def _calls(tokens: int) -> int:
    return int(math.ceil(tokens / max(1, gri_config.TOKENS_PER_CALL)))


# --- complexity ---------------------------------------------------------------
_COMPLEXITY_SIGNALS = (
    "research", "market", "competitor", "internet", "web", "scrape",
    "image", "chart", "diagram", "graphic", "data", "dataset",
    "financial", "forecast", "legal", "compliance", "multi", "end-to-end",
)


def complexity_score(n_deliverables: int, total_tokens: int, prompt: str) -> int:
    low = prompt.lower()
    hits = sum(1 for s in _COMPLEXITY_SIGNALS if s in low)
    score = 0.0
    score += min(40.0, n_deliverables * 12.0)
    score += min(35.0, total_tokens / 30_000.0)
    score += min(25.0, hits * 5.0)
    return int(_clamp(1, 100, round(score)))


# --- provider scoring ---------------------------------------------------------
def _price_scores() -> dict[str, float]:
    """Cheapest provider -> 1.0, most expensive -> 0.0 (linear)."""
    prices = gri_config.PRICING
    lo, hi = min(prices.values()), max(prices.values())
    span = (hi - lo) or 1.0
    return {k: 1.0 - (v - lo) / span for k, v in prices.items()}


def _speed_score(health_entry: dict) -> float:
    lat = health_entry.get("latency_ms")
    if health_entry.get("health") == "down":
        return 0.0
    if lat is None:
        return 0.7  # unknown -> neutral
    # 400ms -> ~1.0, 3000ms -> ~0.2
    return float(_clamp(0.1, 1.0, 1.15 - lat / 3000.0))


def _reliability_score(health: str) -> float:
    return {"healthy": 1.0, "degraded": 0.6, "down": 0.0, "unknown": 0.7}.get(health, 0.7)


def score_providers(
    total_tokens: int,
    provider_spend: dict[str, float],
    health: dict[str, dict],
) -> list[dict]:
    """Score each Guardian provider key on cost/speed/capacity/reliability."""
    price_scores = _price_scores()
    weights = gri_config.SCORE_WEIGHTS
    out: list[dict] = []

    for p in gri_config.PROVIDERS:
        key = p["key"]
        budget = gri_config.PROVIDER_BUDGETS.get(key, 0.0) or 0.0
        spent = float(provider_spend.get(key, 0.0))
        remaining_budget = max(0.0, budget - spent)
        remaining_pct = round(100.0 * remaining_budget / budget, 1) if budget else 0.0

        est_cost = (total_tokens / 1000.0) * gri_config.PRICING.get(key, gri_config.BLENDED_PRICE_PER_1K)
        est_needed_pct = round(100.0 * est_cost / budget, 1) if budget else 999.0

        h = health.get(key, {})
        health_state = h.get("health", "unknown")
        latency_ms = h.get("latency_ms")

        # sub-scores in [0,1]
        cost_s = price_scores.get(key, 0.5)
        speed_s = _speed_score(h)
        capacity_s = _clamp(0.0, 1.0, (remaining_pct - est_needed_pct) / 100.0 + 0.01)
        reliab_s = _reliability_score(health_state)

        composite = (
            weights["cost"] * cost_s
            + weights["speed"] * speed_s
            + weights["capacity"] * capacity_s
            + weights["reliability"] * reliab_s
        )
        score = round(100.0 * composite, 1)

        can_complete = est_needed_pct <= remaining_pct and health_state != "down"
        # per-provider completion probability leans on capacity + reliability
        prob = round(_clamp(0.02, 0.99, 0.6 * capacity_s + 0.4 * reliab_s), 2)
        if health_state == "down":
            prob = 0.02
        risk = "LOW" if (can_complete and prob >= 0.7) else "MEDIUM" if prob >= 0.4 else "HIGH"

        out.append(
            {
                "key": key,
                "name": p["name"],
                "remaining_budget_pct": remaining_pct,
                "est_needed_pct": min(est_needed_pct, 999.0),
                "completion_probability": prob,
                "risk": risk,
                "score": score,
                "health": health_state,
                "latency_ms": latency_ms,
                "_can_complete": can_complete,
            }
        )
    return out


def pick_best_provider(scored: list[dict]) -> dict:
    """Highest scorer that can complete; else the highest scorer overall."""
    completers = [p for p in scored if p.get("_can_complete")]
    pool = completers or scored
    return max(pool, key=lambda p: p["score"])


# --- phase planner ------------------------------------------------------------
def _make_phase(idx: int, items: list[dict]) -> dict:
    tokens = sum(i["est_tokens"] for i in items)
    return {
        "name": f"Phase {idx}",
        "deliverables": [i["name"] for i in items],
        "est_tokens": tokens,
        "est_runtime_sec": _runtime_sec(tokens),
        "est_ai_calls": _calls(tokens),
    }


def build_phases(
    deliverables: list[dict], cap_first: int, cap_rest: int
) -> list[dict]:
    """Greedily group deliverables into phases whose #calls fit the capacity."""
    phases: list[dict] = []
    cur: list[dict] = []
    cur_calls = 0
    cap = max(1, cap_first)
    for d in deliverables:
        d_calls = _calls(d["est_tokens"])
        if cur and cur_calls + d_calls > cap:
            phases.append(_make_phase(len(phases) + 1, cur))
            cur, cur_calls = [], 0
            cap = max(1, cap_rest)
        cur.append(d)
        cur_calls += d_calls
    if cur:
        phases.append(_make_phase(len(phases) + 1, cur))
    return phases


# --- top-level analyze --------------------------------------------------------
def analyze(
    *,
    prompt: str,
    deliverables: Optional[list[str]],
    attachments: Optional[list[dict]],
    history_tokens: Optional[int],
    quota: dict,
    provider_spend: dict[str, float],
    health: dict[str, dict],
) -> dict:
    """Produce the full flight-plan. `quota` = {plan, limit, used, remaining}."""
    # 1. Input tokens.
    base_input = count_tokens(prompt) + int(history_tokens or 0)
    for a in attachments or []:
        base_input += int(a.get("tokens") or count_tokens(a.get("name", "")))

    # 2. Deliverables + per-item estimates.
    detected = detect_deliverables(prompt, deliverables)
    deliverable_estimates: list[dict] = []
    for name, dtype in detected:
        est = gri_config.DELIVERABLE_PROFILES.get(dtype, gri_config.DELIVERABLE_PROFILES["generic_doc"])
        deliverable_estimates.append(
            {
                "name": name,
                "type": dtype,
                "est_tokens": est,
                "est_runtime_sec": _runtime_sec(est),
            }
        )

    total_tokens = base_input + sum(d["est_tokens"] for d in deliverable_estimates)
    est_calls = _calls(total_tokens)
    est_runtime = _runtime_sec(total_tokens)
    est_cost = round((total_tokens / 1000.0) * gri_config.BLENDED_PRICE_PER_1K, 2)
    complexity = complexity_score(len(deliverable_estimates), total_tokens, prompt)

    # 3. Providers.
    scored = score_providers(total_tokens, provider_spend, health)
    best = pick_best_provider(scored)

    # 4. Quota fit + completion probability.
    limit = quota.get("limit")
    used = int(quota.get("used") or 0)
    remaining = quota.get("remaining")
    unlimited = limit is None

    quota_score = 1.0 if unlimited else _clamp(0.0, 1.0, (remaining or 0) / max(1, est_calls))
    best_headroom = (best["remaining_budget_pct"] - best["est_needed_pct"]) / 100.0
    budget_score = _clamp(0.0, 1.0, best_headroom + 0.01)
    probability = round(_clamp(0.02, 0.99, 0.15 + 0.85 * (0.7 * quota_score + 0.3 * budget_score)), 2)
    risk = "LOW" if probability >= 0.75 else "MEDIUM" if probability >= 0.45 else "HIGH"

    # 5. Decision engine.
    recommendation = _decide(
        unlimited=unlimited,
        remaining=remaining,
        limit=limit,
        est_calls=est_calls,
        risk=risk,
        deliverables=deliverable_estimates,
        best=best,
        probability=probability,
    )

    return {
        "complexity_score": complexity,
        "estimated_tokens": total_tokens,
        "estimated_ai_calls": est_calls,
        "estimated_runtime_sec": est_runtime,
        "estimated_cost_usd": est_cost,
        "deliverables": deliverable_estimates,
        "quota": {"plan": quota.get("plan", "free"), "limit": limit, "used": used, "remaining": remaining},
        "completion_probability": probability,
        "risk": risk,
        "recommendation": recommendation,
        "providers": [{k: v for k, v in p.items() if not k.startswith("_")} for p in scored],
        "presentation": False,
    }


def _decide(
    *,
    unlimited: bool,
    remaining: Optional[int],
    limit: Optional[int],
    est_calls: int,
    risk: str,
    deliverables: list[dict],
    best: dict,
    probability: float,
) -> dict:
    best_name = best["name"]
    n = len(deliverables)

    # Enterprise / comfortably fits -> PROCEED.
    if unlimited or (remaining is not None and est_calls <= remaining):
        return {
            "verdict": "PROCEED",
            "phases": None,
            "best_provider": best_name,
            "message": (
                f"You're clear for takeoff. This needs ~{est_calls} AI calls and you have "
                f"{'unlimited capacity' if unlimited else str(remaining) + ' left this cycle'}. "
                f"Recommended engine: {best_name}."
            ),
            "alternatives": [],
        }

    # Nothing left this cycle -> QUEUE / upgrade.
    if remaining is not None and remaining <= 0:
        return {
            "verdict": "QUEUE",
            "phases": None,
            "best_provider": best_name,
            "message": (
                "You've used your full quota this cycle. Queue this project to auto-start "
                "when your quota resets, or upgrade your plan to run it now."
            ),
            "alternatives": ["REDUCE", "SWITCH_PROVIDER"],
        }

    # Over capacity but some remaining -> PHASE it.
    cap_first = max(1, remaining or 0)
    cap_rest = max(1, limit or est_calls)
    phases = build_phases(deliverables, cap_first, cap_rest)

    # A single deliverable too big for even a full cycle -> can't phase it down.
    single_too_big = n == 1 and _calls(deliverables[0]["est_tokens"]) > cap_rest
    if single_too_big:
        return {
            "verdict": "REDUCE",
            "phases": phases,
            "best_provider": best_name,
            "message": (
                f"This single deliverable is larger than a full cycle's capacity. Reduce its "
                f"scope, split it into smaller pieces, or upgrade your plan. {best_name} is the "
                f"most capable engine for it."
            ),
            "alternatives": ["QUEUE", "SWITCH_PROVIDER"],
        }

    return {
        "verdict": "PHASE",
        "phases": phases,
        "best_provider": best_name,
        "message": (
            f"This won't finish in one run (~{est_calls} AI calls needed, "
            f"{remaining} left this cycle). Guardian split it into {len(phases)} checkpointed "
            f"phases so no work is lost — Phase 1 fits your remaining capacity now, the rest "
            f"continue next cycle. Recommended engine: {best_name}."
        ),
        "alternatives": ["REDUCE", "QUEUE", "SWITCH_PROVIDER"],
    }
