"""guardian_filter: multi-model consensus + NLI entailment + semantic drift.

The NLI pipeline and the SentenceTransformer embedder are loaded ONCE at import
time (module level), never per request.
"""
from __future__ import annotations

import time
from itertools import combinations
from typing import Optional

from sentence_transformers import SentenceTransformer, util
from transformers import pipeline

from app import models_config
from app.llm import ModelResponse

# ---------------------------------------------------------------------------
# Load heavy models ONCE at startup (module level).
# ---------------------------------------------------------------------------
print(f"[guardian] loading NLI model: {models_config.NLI_MODEL} ...")
_nli = pipeline("text-classification", model=models_config.NLI_MODEL)

print(f"[guardian] loading embedding model: {models_config.EMBED_MODEL} ...")
_embedder = SentenceTransformer(models_config.EMBED_MODEL)
print("[guardian] models ready.")


def _consensus_score(texts: list[str]) -> float:
    """Average pairwise cosine similarity of response embeddings, in [0, 1].

    1 response -> 1.0 (trivially consistent). 0 responses -> 0.0.
    """
    if len(texts) <= 1:
        return 1.0 if texts else 0.0
    embeddings = _embedder.encode(texts, convert_to_tensor=True, normalize_embeddings=True)
    sims = [
        float(util.cos_sim(embeddings[i], embeddings[j]))
        for i, j in combinations(range(len(texts)), 2)
    ]
    score = sum(sims) / len(sims)
    # cosine of normalized embeddings is in [-1, 1]; clamp to [0, 1] for a score.
    return max(0.0, min(1.0, score))


def _entails(reference: str, candidate: str) -> bool:
    """True if `candidate` ENTAILS `reference` per the DistilBERT MNLI model."""
    # MNLI labels: ENTAILMENT / NEUTRAL / CONTRADICTION.
    result = _nli(
        {"text": reference, "text_pair": candidate},
        truncation=True,
    )
    # transformers may return a dict or a list[dict] depending on version.
    label = (result[0] if isinstance(result, list) else result)["label"]
    return label.upper().startswith("ENTAIL")


def _decide(entail_votes: int, total: int, consensus: float) -> str:
    pass_votes = models_config.entail_pass_threshold(total)
    if entail_votes >= pass_votes and consensus >= models_config.CONSENSUS_PASS_THRESHOLD:
        return "PASS"
    if entail_votes <= 1 or consensus < models_config.CONSENSUS_BLOCK_THRESHOLD:
        return "BLOCKED"
    return "FLAGGED"


def guardian_filter(responses: list[ModelResponse]) -> dict:
    """Run consensus + NLI over fanned-out responses and return a verdict dict."""
    start = time.perf_counter()

    ok = [r for r in responses if r.get("content")]
    total = len(ok)

    if total == 0:
        return {
            "verdict": "BLOCKED",
            "consensus_score": 0.0,
            "agreement": "0/0",
            "response": "No model returned a usable response.",
            "per_model": [
                {
                    "model": r["model"],
                    "entails": False,
                    "latency_ms": r["latency_ms"],
                    "error": r.get("error"),
                }
                for r in responses
            ],
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
        }

    texts = [r["content"] for r in ok]  # type: ignore[misc]
    consensus = _consensus_score(texts)

    # Reference = first (most-central) response; it counts as entailing itself.
    reference = texts[0]
    per_model: list[dict] = []
    entail_votes = 0
    for r in ok:
        content = r["content"]
        entails = True if content == reference else _entails(reference, content)  # type: ignore[arg-type]
        if entails:
            entail_votes += 1
        per_model.append(
            {
                "model": r["model"],
                "entails": entails,
                "latency_ms": r["latency_ms"],
                "error": None,
            }
        )

    # Include errored models in per_model for transparency (not counted in total).
    for r in responses:
        if not r.get("content"):
            per_model.append(
                {
                    "model": r["model"],
                    "entails": False,
                    "latency_ms": r["latency_ms"],
                    "error": r.get("error"),
                }
            )

    verdict = _decide(entail_votes, total, consensus)

    return {
        "verdict": verdict,
        "consensus_score": round(consensus, 4),
        "agreement": f"{entail_votes}/{total}",
        "response": reference,
        "per_model": per_model,
        "elapsed_ms": int((time.perf_counter() - start) * 1000),
    }
