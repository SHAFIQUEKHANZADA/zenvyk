"""Pydantic request/response models for the Guardian API."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Verdict = Literal["PASS", "FLAGGED", "BLOCKED"]


# ---------- /v1/verify ----------
class VerifyRequest(BaseModel):
    prompt: str = Field(..., description="The user prompt to verify across models.")


class PerModel(BaseModel):
    model: str
    entails: bool
    latency_ms: int
    error: Optional[str] = None


class VerifyResponse(BaseModel):
    verdict: Verdict
    consensus_score: float
    agreement: str  # e.g. "4/5"
    response: str
    per_model: list[PerModel]
    elapsed_ms: int


# ---------- /v1/chat/completions (OpenAI-compatible) ----------
class ChatMessage(BaseModel):
    role: str
    content: Any  # string or content-parts; we extract text best-effort


class ChatCompletionRequest(BaseModel):
    model: str = "zenvyk-guardian"
    messages: list[ChatMessage]
    # Accept and ignore the rest of the OpenAI surface for drop-in compatibility.
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    n: Optional[int] = None
    stream: Optional[bool] = False
    max_tokens: Optional[int] = None

    model_config = {"extra": "allow"}
