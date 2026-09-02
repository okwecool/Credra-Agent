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
EvidenceSummaryStatus = Literal[
    "SUPPORTED",
    "CORROBORATED",
    "CONFLICTING",
    "UNVERIFIED",
]
EvidenceGapReason = Literal[
    "NO_CANDIDATE",
    "TOOL_FAILED",
    "CONTENT_FETCH_INCOMPLETE",
    "VERIFICATION_INCOMPLETE",
]
QueryProposalDecision = Literal["ACCEPTED_FOR_REVIEW", "REJECTED"]
QueryProposalRejectionReason = Literal[
    "UNAUTHORIZED_CATEGORY",
    "INVALID_QUERY_TYPE",
    "SUBJECT_MISMATCH",
    "UNKNOWN_REFERENCE",
    "DUPLICATE_BASELINE_QUERY",
    "DUPLICATE_PROPOSAL",
    "INVALID_QUERY_FORMAT",
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


class EvidenceSummaryDraftItem(BaseModel):
    """One model summary aligned to one deterministic Evidence index record."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^evidence:[0-9a-f]{64}$")
    verification_status: EvidenceSummaryStatus
    summary: str | None = Field(default=None, max_length=1_000)

    @model_validator(mode="after")
    def non_facts_cannot_receive_model_summaries(self) -> "EvidenceSummaryDraftItem":
        if self.verification_status in {"SUPPORTED", "CORROBORATED"}:
            if not self.summary:
                raise ValueError("verified evidence requires a summary")
        elif self.summary is not None:
            raise ValueError("conflicting or unverified evidence has no model summary")
        return self


class EvidenceGapDraftItem(BaseModel):
    """One model explanation aligned to a deterministic evidence gap."""

    model_config = ConfigDict(extra="forbid")

    gap_id: str = Field(pattern=r"^gap:[0-9a-f]{64}$")
    summary: str = Field(min_length=1, max_length=800)


class QueryProposalDraft(BaseModel):
    """Untrusted candidate Query wording; a deterministic gate decides usability."""

    model_config = ConfigDict(extra="forbid")

    query_type: Literal["company", "industry"]
    category: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=320)
    reference_ids: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=800)


class ResearchAnalysisDraft(BaseModel):
    """Model-authored text before deterministic Evidence and proposal validation."""

    model_config = ConfigDict(extra="forbid")

    verified_summary: str = Field(min_length=1, max_length=2_000)
    summary_evidence_ids: list[str] = Field(default_factory=list, max_length=40)
    evidence_summaries: list[EvidenceSummaryDraftItem] = Field(
        default_factory=list, max_length=40
    )
    gap_summaries: list[EvidenceGapDraftItem] = Field(
        default_factory=list, max_length=40
    )
    query_proposals: list[QueryProposalDraft] = Field(
        default_factory=list, max_length=12
    )
    limitations: list[str] = Field(default_factory=list, max_length=20)


class EvidenceSummaryEntry(BaseModel):
    """Citation-bound Evidence summary with identifiers owned by the runtime."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(pattern=r"^evidence:[0-9a-f]{64}$")
    verification_status: EvidenceSummaryStatus
    query_type: Literal["company", "industry"]
    category: str = Field(min_length=1)
    source_category: str | None = Field(default=None, min_length=1)
    source_id: str = Field(min_length=1)
    claim_id: str | None = Field(default=None, pattern=r"^claim:[0-9a-f]{64}$")
    fact_id: str | None = Field(default=None, pattern=r"^fact:[0-9a-f]{64}$")
    fact_statement: str | None = Field(default=None, max_length=2_000)
    summary: str = Field(min_length=1, max_length=1_000)


class EvidenceGap(BaseModel):
    """A deterministic research coverage gap with a bounded model explanation."""

    model_config = ConfigDict(extra="forbid")

    gap_id: str = Field(pattern=r"^gap:[0-9a-f]{64}$")
    query_type: Literal["company", "industry"]
    category: str = Field(min_length=1)
    reason: EvidenceGapReason
    summary: str = Field(min_length=1, max_length=800)


class QueryProposal(BaseModel):
    """A model proposal retained for review, never a command to execute."""

    model_config = ConfigDict(extra="forbid")

    proposal_id: str = Field(pattern=r"^query-proposal:[0-9a-f]{64}$")
    query_type: Literal["company", "industry"]
    category: str = Field(min_length=1, max_length=80)
    query: str = Field(min_length=1, max_length=320)
    reference_ids: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=800)
    decision: QueryProposalDecision
    rejection_reasons: list[QueryProposalRejectionReason] = Field(
        default_factory=list, max_length=8
    )

    @model_validator(mode="after")
    def decision_has_consistent_reasons(self) -> "QueryProposal":
        if self.decision == "ACCEPTED_FOR_REVIEW" and self.rejection_reasons:
            raise ValueError("accepted proposal cannot have rejection reasons")
        if self.decision == "REJECTED" and not self.rejection_reasons:
            raise ValueError("rejected proposal requires rejection reasons")
        return self


class EvidenceSummaryArtifact(BaseModel):
    """Run-scoped research Evidence summary, preserving deterministic statuses."""

    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(pattern=r"^intent:[0-9a-f]{64}$")
    query_plan_id: str = Field(pattern=r"^query-plan:[0-9a-f]{64}$")
    research_status: Literal["COMPLETE", "PARTIAL", "EMPTY", "INCOMPLETE"]
    verification_status: Literal[
        "SUPPORTED", "CORROBORATED", "CONFLICTING", "UNVERIFIED", "NOT_FOUND"
    ]
    mode: Literal["deterministic", "llm"]
    execution_status: AnalysisExecutionStatus
    model_name: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    verified_summary: str = Field(min_length=1, max_length=2_000)
    summary_evidence_ids: list[str] = Field(default_factory=list, max_length=40)
    entries: list[EvidenceSummaryEntry] = Field(default_factory=list, max_length=40)
    gaps: list[EvidenceGap] = Field(default_factory=list, max_length=40)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    attempts: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    error_code: AnalysisErrorCode | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def execution_fields_are_consistent(self) -> "EvidenceSummaryArtifact":
        if self.execution_status == "DEGRADED":
            if not self.error_code or not self.error_message:
                raise ValueError("degraded summary requires a safe error")
        elif self.error_code or self.error_message:
            raise ValueError("successful or skipped summary cannot contain an error")
        return self


class QueryProposalArtifact(BaseModel):
    """Run-scoped, review-only Query proposals beside the rule Query Plan."""

    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(pattern=r"^intent:[0-9a-f]{64}$")
    query_plan_id: str = Field(pattern=r"^query-plan:[0-9a-f]{64}$")
    allowed_categories: list[str] = Field(min_length=1, max_length=8)
    mode: Literal["deterministic", "llm"]
    execution_status: AnalysisExecutionStatus
    model_name: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    proposals: list[QueryProposal] = Field(default_factory=list, max_length=12)
    limitations: list[str] = Field(default_factory=list, max_length=20)
    attempts: int = Field(default=0, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    error_code: AnalysisErrorCode | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def execution_fields_are_consistent(self) -> "QueryProposalArtifact":
        if self.execution_status == "DEGRADED":
            if not self.error_code or not self.error_message:
                raise ValueError("degraded proposals require a safe error")
        elif self.error_code or self.error_message:
            raise ValueError("successful or skipped proposals cannot contain an error")
        return self
