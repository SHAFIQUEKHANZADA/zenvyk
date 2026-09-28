"""Generate one clean answer, then have N models independently verify it.

Replaces the old "consensus of N answers + cosine wording-drift" path, which
could BLOCK a correct answer just because the models phrased it differently
(e.g. "What's 20 + 20" → BLOCKED). Here:

  1. GENERATE — one model answers the user's question directly. The generator
     prompt never mentions verification/consensus, so the answer stays clean.
  2. VERIFY  — every model independently fact-checks that answer and returns
     strict JSON {"verdict":"PASS"|"FLAG","reason":"..."}. Robust parsing; a
     parse failure or timeout is ERROR (never a silent FLAG).

Score, agreement and verdict are all derived from ONE results array, so they
can never contradict each other. Reasons are returned per model for the
transparency breakdown. Every model call has a hard timeout so a run always
finishes (no hanging "verifying…").
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from typing import Optional

import litellm

# Tolerate provider-specific param quirks (e.g. models that reject temperature).
litellm.drop_params = True

# Audit log — every model's raw response per request (visible in Railway logs)
# so a verdict can always be traced back to what each model actually said.
# Attach our OWN stdout handler so the lines show up under uvicorn/Railway (a
# bare logger would otherwise be swallowed — uvicorn doesn't wire the root
# logger to stdout at INFO).
log = logging.getLogger("guardian.verify")
if not log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(asctime)s [guardian.verify] %(message)s"))
    log.addHandler(_handler)
log.setLevel(logging.INFO)
log.propagate = False

# --- Tunables (env-overridable) ---
# Per-model call timeout. A model that exceeds it is recorded as ERROR.
VERIFY_TIMEOUT_S: float = float(os.getenv("VERIFY_TIMEOUT_S", "30"))
# Optional dedicated generator model; defaults to the first model in the panel.
GENERATOR_MODEL: str = os.getenv("GENERATOR_MODEL", "").strip()
# Fraction of graded verifiers that must PASS for an overall PASS.
VERIFY_PASS_RATIO: float = float(os.getenv("VERIFY_PASS_RATIO", "0.6"))

# The generator answers the user. It must NOT know about verification, or it
# writes meta-commentary ("all models should agree…") that pollutes the answer.
_GEN_SYSTEM = (
    "You are a helpful, accurate assistant. Answer the user's latest question "
    "directly and concisely. Do not mention verification, consensus, scoring, "
    "reviewers, or that other models check your work — just answer the question."
)

# The verifier judges factual correctness only — never style/tone/length.
_VERIFY_INSTRUCTION = (
    "You are a strict fact-checker reviewing another AI's ANSWER to a QUESTION. "
    "Judge ONLY whether the answer is factually correct and actually answers the "
    "question. Ignore style, tone, length, and wording. "
    'Reply with ONLY a JSON object and nothing else: '
    '{"verdict":"PASS","reason":"<one short sentence>"} '
    'where verdict is "PASS" if the answer is correct or "FLAG" if it is wrong, '
    "misleading, or unsupported."
)


def _extract_json(text: str) -> dict:
    """Best-effort parse of a JSON object from a model reply.

    Handles ```json fences and surrounding prose by grabbing the first {...}.
    Returns {} on failure (caller treats that as ERROR, never FLAG).
    """
    if not text:
        return {}
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 - malformed JSON -> ERROR upstream
        return {}


async def generate_answer(prompt: str, model: str) -> tuple[str, Optional[str]]:
    """Produce one clean answer. Returns (answer, error). Never raises."""
    try:
        resp = await asyncio.wait_for(
            litellm.acompletion(
                model=model,
                messages=[
                    {"role": "system", "content": _GEN_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
            ),
            timeout=VERIFY_TIMEOUT_S,
        )
        answer = (resp["choices"][0]["message"]["content"] or "").strip()
        log.info("[generate] model=%s len=%d answer=%r", model, len(answer), answer[:500])
        return answer, None
    except asyncio.TimeoutError:
        log.info("[generate] model=%s ERROR=timeout", model)
        return "", "timeout"
    except Exception as exc:  # noqa: BLE001 - surface, don't crash the run
        log.info("[generate] model=%s ERROR=%s", model, exc)
        return "", f"{type(exc).__name__}: {exc}"


async def _verify_one(question: str, answer: str, model: str) -> dict:
    """One verifier's judgement of the answer. Returns PASS | FLAG | ERROR."""
    start = time.perf_counter()
    content = f"QUESTION:\n{question}\n\nANSWER:\n{answer}\n\n{_VERIFY_INSTRUCTION}"

    def _ms() -> int:
        return int((time.perf_counter() - start) * 1000)

    try:
        resp = await asyncio.wait_for(
            litellm.acompletion(
                model=model,
                messages=[{"role": "user", "content": content}],
                temperature=0,
            ),
            timeout=VERIFY_TIMEOUT_S,
        )
        raw = resp["choices"][0]["message"]["content"] or ""
        data = _extract_json(raw)
        verdict = str(data.get("verdict", "")).upper()
        reason = str(data.get("reason", "")).strip()
        log.info("[verify] model=%s parsed=%s raw=%r", model, verdict or "?", raw[:500])
        if verdict not in ("PASS", "FLAG"):
            # Parse failure = ERROR, NOT a silent FLAG.
            return {
                "model": model, "verdict": "ERROR",
                "reason": "Could not read this model's verdict.",
                "latency_ms": _ms(), "error": "parse_error", "raw": raw[:400],
            }
        return {
            "model": model, "verdict": verdict,
            "reason": reason or ("Answer is correct." if verdict == "PASS"
                                 else "Flagged a problem with the answer."),
            "latency_ms": _ms(), "error": None, "raw": raw[:400],
        }
    except asyncio.TimeoutError:
        return {
            "model": model, "verdict": "ERROR", "reason": "Verifier timed out.",
            "latency_ms": _ms(), "error": "timeout",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "model": model, "verdict": "ERROR",
            "reason": "Verifier failed to respond.",
            "latency_ms": _ms(), "error": f"{type(exc).__name__}: {exc}",
        }


async def verify_answer(question: str, answer: str, models: list[str]) -> list[dict]:
    """Fan out the verifier check to all models concurrently."""
    tasks = [_verify_one(question, answer, m) for m in models]
    return list(await asyncio.gather(*tasks))


def _build_result(answer: str, results: list[dict]) -> dict:
    """Derive verdict + score + agreement + per-model rows from ONE array.

    - Denominator = verifiers that returned a real verdict (ERROR excluded).
    - PASS if the pass-ratio clears the threshold; otherwise FLAGGED.
    - BLOCKED only when NOTHING could be verified (all errored) — never for a
      correct-but-differently-worded answer.
    """
    graded = [r for r in results if r["verdict"] in ("PASS", "FLAG")]
    denom = len(graded)
    pass_count = sum(1 for r in graded if r["verdict"] == "PASS")

    if denom == 0:
        verdict = "BLOCKED"
        ratio = 0.0
    else:
        ratio = pass_count / denom
        verdict = "PASS" if ratio >= VERIFY_PASS_RATIO else "FLAGGED"

    per_model = [
        {
            "model": r["model"],
            "verdict": r["verdict"],          # PASS | FLAG | ERROR
            "ok": r["verdict"] == "PASS",
            "reason": r.get("reason"),
            "answer": None,                    # verifiers don't produce answers
            "latency_ms": r.get("latency_ms", 0),
            "error": r.get("error"),
        }
        for r in results
    ]

    return {
        "verdict": verdict,
        "consensus_score": round(ratio, 4),
        "agreement": f"{pass_count}/{denom}",
        "response": answer,
        "per_model": per_model,
    }


async def run_verification(
    question: str,
    generator_prompt: str,
    models: list[str],
    generator_model: Optional[str] = None,
) -> dict:
    """Generate an answer, verify it across `models`, return the verdict dict.

    `question` is the user's raw ask (what verifiers judge against).
    `generator_prompt` is the fully-grounded prompt (history + doc/URL + ask)
    used only to WRITE the answer.
    """
    start = time.perf_counter()
    gen_model = generator_model or GENERATOR_MODEL or (models[0] if models else None)

    if not gen_model:
        return {
            "verdict": "BLOCKED", "consensus_score": 0.0, "agreement": "0/0",
            "response": "No model is configured to answer.", "per_model": [],
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
        }

    answer, gen_err = await generate_answer(generator_prompt, gen_model)
    if not answer:
        return {
            "verdict": "BLOCKED", "consensus_score": 0.0, "agreement": "0/0",
            "response": "The answer couldn't be generated — please try again.",
            "per_model": [], "generator_error": gen_err,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
        }

    results = await verify_answer(question, answer, models)
    out = _build_result(answer, results)
    out["elapsed_ms"] = int((time.perf_counter() - start) * 1000)
    log.info(
        "[result] verdict=%s agreement=%s score=%s question=%r",
        out["verdict"], out["agreement"], out["consensus_score"], question[:200],
    )
    return out
