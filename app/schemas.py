"""Pydantic request/response models for the Guardian API."""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Verdict = Literal["PASS", "FLAGGED", "BLOCKED"]


# ---------- /v1/verify ----------
class SourceMessage(BaseModel):
    role: str = "user"
    content: str = ""


class VerifyRequest(BaseModel):
    prompt: str = Field(..., description="The user prompt to verify across models.")
    # Optional grounding / context (used by the dashboard Playground).
    messages: Optional[list[SourceMessage]] = None      # prior conversation turns
    url: Optional[str] = None                            # a link to ground against
    document_text: Optional[str] = None                 # extracted file text to ground against


class PerModel(BaseModel):
    model: str
    entails: bool
    latency_ms: int
    error: Optional[str] = None
    answer: Optional[str] = None   # the model's (truncated) response text
    ok: Optional[bool] = None      # alias of `entails` for the dashboard breakdown


class Usage(BaseModel):
    used: int
    limit: Optional[int] = None  # None = unlimited (enterprise)


class Clarification(BaseModel):
    question: str
    options: list[str] = []


class SourceUsed(BaseModel):
    type: str  # "url" | "document"
    ref: str


# Verify can also ask the user to clarify instead of just flagging.
VerifyStatus = Literal["PASS", "FLAGGED", "BLOCKED", "NEEDS_CLARIFICATION"]


class VerifyResponse(BaseModel):
    verdict: Verdict
    status: Optional[VerifyStatus] = None  # verdict, or NEEDS_CLARIFICATION
    consensus_score: float
    agreement: str  # e.g. "4/5"
    response: str
    per_model: list[PerModel]
    elapsed_ms: int
    clarification: Optional[Clarification] = None  # set when status == NEEDS_CLARIFICATION
    source_used: Optional[SourceUsed] = None       # set when grounded on a doc/URL
    # Plan + usage meta (populated when enforcement is on) so the dashboard can show it.
    plan: Optional[str] = None
    usage: Optional[Usage] = None


# ---------- /v1/extract + /v1/route (conversation router/extractor) ----------
class ExtractRequest(BaseModel):
    url: str = Field(..., description="A link whose readable text/conversation to extract.")


class RouteRequest(BaseModel):
    content: str = Field(..., description="Content to send to the chosen model.")
    model: str = Field(..., description="Which model to route the content to.")


# ---------- Guardian Resource Intelligence (GRI) ----------
Risk = Literal["LOW", "MEDIUM", "HIGH"]
Verdict5 = Literal["PROCEED", "PHASE", "REDUCE", "QUEUE", "SWITCH_PROVIDER"]


class Attachment(BaseModel):
    name: str
    tokens: Optional[int] = None


class AnalyzeRequest(BaseModel):
    prompt: str = Field(..., description="The project the user wants the AI to do.")
    deliverables: Optional[list[str]] = None          # explicit deliverable types/names
    attachments: Optional[list[Attachment]] = None    # files that add input tokens
    history_tokens: Optional[int] = None              # prior conversation size


class DeliverableEstimate(BaseModel):
    name: str
    type: str
    est_tokens: int
    est_runtime_sec: int


class QuotaInfo(BaseModel):
    plan: str
    limit: Optional[int] = None    # None = unlimited (enterprise)
    used: int = 0
    remaining: Optional[int] = None  # None = unlimited


class ProviderComparison(BaseModel):
    key: str
    name: str
    remaining_budget_pct: float
    est_needed_pct: float
    completion_probability: float
    risk: Risk
    score: float
    health: str            # healthy | degraded | down | unknown
    latency_ms: Optional[int] = None


class PhasePlan(BaseModel):
    name: str
    deliverables: list[str]
    est_tokens: int
    est_runtime_sec: int
    est_ai_calls: int


class Recommendation(BaseModel):
    verdict: Verdict5
    phases: Optional[list[PhasePlan]] = None
    best_provider: str
    message: str
    alternatives: list[str] = []


class AnalyzeResponse(BaseModel):
    complexity_score: int
    estimated_tokens: int
    estimated_ai_calls: int
    estimated_runtime_sec: int
    estimated_cost_usd: float
    deliverables: list[DeliverableEstimate]
    quota: QuotaInfo
    completion_probability: float
    risk: Risk
    recommendation: Recommendation
    providers: list[ProviderComparison]
    project_id: Optional[str] = None
    presentation: bool = False


class ExecuteRequest(BaseModel):
    project_id: str
    phase_index: int = 0


class CheckpointRequest(BaseModel):
    project_id: str
    phase_index: int
    output: Any


class ResumeRequest(BaseModel):
    project_id: str


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
