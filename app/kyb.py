"""Guardian KYB consensus engine (pure).

Given the per-source results (each: verdict + matched fields + weight), compute:
  - how many sources AGREE (3-of-5 style consensus)
  - a weighted trust score (0-100) + risk score (0-100)
  - a decision: AUTO_APPROVE / FLAG / REJECT
  - per-field consensus (name/address/ein/status/watchlist)

No network I/O here — kyb_sources.py fetches, this decides. Fully testable.
"""
from __future__ import annotations

from typing import Any

from app import kyb_config

Decision = str  # "AUTO_APPROVE" | "FLAG" | "REJECT"


def _core_ratio(fields: dict) -> float:
    """Fraction of the core fields (name/address/ein) this source confirmed,
    over the core fields it actually reports on."""
    provided = [f for f in kyb_config.CORE_FIELDS if isinstance(fields.get(f), bool)]
    if not provided:
        return 0.0
    matched = sum(1 for f in provided if fields.get(f) is True)
    return matched / len(provided)


def decide(sources: list[dict]) -> dict:
    """Run consensus + scoring over fetched source results."""
    total = len(sources)
    usable = [s for s in sources if s.get("verdict") != "ERROR"]

    agrees = sum(1 for s in usable if s.get("verdict") == "AGREES")
    watchlist_hit = any(
        (s.get("fields") or {}).get("watchlist") == "hit" for s in usable
    )

    # Weighted trust from core-field matches.
    weight_sum = 0.0
    weighted = 0.0
    for s in usable:
        w = float(s.get("weight") or 0.0)
        ratio = _core_ratio(s.get("fields") or {})
        s["match_ratio"] = round(ratio, 3)
        weight_sum += w
        weighted += w * ratio
    trust = round(100.0 * (weighted / weight_sum), 1) if weight_sum else 0.0

    if watchlist_hit:
        trust = min(trust, 18.0)

    risk = int(round(100 - trust))
    if watchlist_hit:
        risk = max(risk, 85)

    # Decision engine.
    if watchlist_hit:
        decision, reason = "REJECT", "Watchlist / sanctions match — cannot approve."
    elif agrees == 0:
        decision, reason = "REJECT", "No source could confirm this business."
    elif agrees >= kyb_config.CONSENSUS_MIN and trust >= kyb_config.TRUST_APPROVE:
        decision = "AUTO_APPROVE"
        reason = (
            f"{agrees} of {total} sources agree with high confidence — cleared for "
            f"automated profile creation."
        )
    else:
        decision = "FLAG"
        reason = (
            f"Only {agrees} of {total} sources fully agree — routing to a human for review."
        )

    # Per-field consensus tallies.
    field_consensus: dict[str, dict[str, int]] = {}
    for f in ("name", "address", "ein", "status", "watchlist"):
        agree_n = 0
        seen = 0
        for s in usable:
            v = (s.get("fields") or {}).get(f)
            if v is None:
                continue
            seen += 1
            if f == "status":
                if str(v).lower() in ("active", "good_standing", "good standing"):
                    agree_n += 1
            elif f == "watchlist":
                if str(v).lower() == "clear":
                    agree_n += 1
            elif v is True:
                agree_n += 1
        if seen:
            field_consensus[f] = {"agree": agree_n, "total": seen}

    sample = any(s.get("mode") != "live" for s in sources)

    return {
        "decision": decision,
        "trust_score": trust,
        "risk_score": risk,
        "confidence": int(round(trust)),
        "consensus": {"agree": agrees, "total": total},
        "watchlist_hit": watchlist_hit,
        "field_consensus": field_consensus,
        "sources": sources,
        "sample": sample,
        "message": reason,
    }


def merge(business: dict[str, Any], result: dict) -> dict:
    """Attach the normalized input + a downstream hint to the decision result."""
    result["business"] = business
    if result["decision"] == "AUTO_APPROVE":
        result["next"] = (
            "Verified data package queued for listings submission — Google Business "
            "Profile, Apple Business Connect, aggregator feed. GHL workflow notified."
        )
    elif result["decision"] == "FLAG":
        result["next"] = "Sent to a human reviewer for the fields that didn't reach consensus."
    else:
        result["next"] = "Blocked from profile creation. No listings will be submitted."
    return result
