"""Structured contracts for constrained LLM analysis artifacts."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AnalysisExecutionStatus = Literal[
    "NOT_REQUESTED",
    "NOT_NEEDED",
    "COMPLETE",
    "DEGRADED",
]
AnalysisErrorCode = Literal[
    "CONFIG_ERROR",
    "INPUT_TOO_LARGE",
    "MODEL_ERROR",
    "INVALID_OUTPUT",
    "UNSUPPORTED_EVIDENCE",
]


class RiskNarrativeExplanation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_id: str = Field(pattern=r"^risk:\d+:[a-z][a-z0-9_]*$")
    explanation: str = Field(min_length=1, max_length=1_000)
    evidence_ids: list[str] = Field(min_length=1)


class RiskNarrativeDraft(BaseModel):
    """Model-authored fields before citation boundary validation."""

    model_config = ConfigDict(extra="forbid")

    overall_summary: str = Field(min_length=1, max_length=2_000)
    summary_evidence_ids: list[str] = Field(min_length=1)
    explanations: list[RiskNarrativeExplanation] = Field(min_length=1)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class RiskNarrativeArtifact(BaseModel):
    """Run-scoped accepted LLM narrative or deterministic fallback."""

    model_config = ConfigDict(extra="forbid")

    risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    mode: Literal["deterministic", "llm"]
    execution_status: AnalysisExecutionStatus
    model_name: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    overall_summary: str = Field(min_length=1)
    summary_evidence_ids: list[str] = Field(default_factory=list)
    explanations: list[RiskNarrativeExplanation] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    attempts: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    error_code: AnalysisErrorCode | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def execution_fields_are_consistent(self) -> "RiskNarrativeArtifact":
        if self.execution_status == "DEGRADED":
            if not self.error_code or not self.error_message:
                raise ValueError("degraded analysis requires a safe error")
        elif self.error_code or self.error_message:
            raise ValueError("successful or skipped analysis cannot contain an error")
        if self.execution_status == "COMPLETE" and not self.explanations:
            raise ValueError("complete narrative requires explanations")
        return self
