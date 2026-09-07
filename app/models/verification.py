"""Structured contracts for claim-level verification of untrusted web content."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.content import ContentSegment

VerificationRelation = Literal[
    "SUPPORTS",
    "REFUTES",
    "IRRELEVANT",
    "INSUFFICIENT",
]
VerificationExecutionStatus = Literal[
    "NOT_NEEDED",
    "DISABLED",
    "COMPLETE",
    "PARTIAL",
    "FAILED",
]
VerificationErrorCode = Literal[
    "BUDGET_EXCEEDED",
    "CONTENT_UNAVAILABLE",
    "SNAPSHOT_MISSING",
    "MODEL_ERROR",
    "INVALID_OUTPUT",
    "CLAIM_MISMATCH",
    "EVIDENCE_MISMATCH",
    "LOW_CONFIDENCE",
]


class VerificationClaim(BaseModel):
    """One stable claim derived from a deterministic search category."""

    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(pattern=r"^claim:[0-9a-f]{64}$")
    subject: str = Field(min_length=1)
    subject_aliases: list[str] = Field(default_factory=list)
    category: str = Field(min_length=1)
    statement: str = Field(min_length=1)


class VerifierInput(BaseModel):
    """Bounded untrusted document input passed to one verifier call."""

    model_config = ConfigDict(extra="forbid")

    claim: VerificationClaim
    source_id: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_tier: Literal["A", "B", "C"]
    document_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    segments: list[ContentSegment] = Field(min_length=1)


class VerifierDecision(BaseModel):
    """Strict provider output before trust-boundary validation."""

    model_config = ConfigDict(extra="forbid")

    subject_match: Literal["EXACT", "ALIAS", "NONE"]
    relation: VerificationRelation
    claim: str = Field(min_length=1)
    evidence_excerpt: str | None = None
    evidence_location: str | None = None
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)

    @model_validator(mode="after")
    def direct_relations_require_evidence(self) -> "VerifierDecision":
        if self.relation in {"SUPPORTS", "REFUTES"} and (
            not self.evidence_excerpt or not self.evidence_location
        ):
            raise ValueError("SUPPORTS/REFUTES require excerpt and location")
        return self


class ClaimVerification(BaseModel):
    """Auditable verification record attached to one candidate evidence item."""

    model_config = ConfigDict(extra="forbid")

    claim: VerificationClaim
    subject_match: Literal["EXACT", "ALIAS", "NONE"]
    relation: VerificationRelation
    evidence_excerpt: str | None = None
    evidence_location: str | None = None
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    accepted: bool = False
    verifier: str = Field(min_length=1)
    verifier_model: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    verified_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    cache_hit: bool = False
    attempts: int = Field(default=1, ge=0)
    error_code: VerificationErrorCode | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def accepted_direct_relations_require_evidence(self) -> "ClaimVerification":
        if self.accepted and self.relation not in {"SUPPORTS", "REFUTES"}:
            raise ValueError("accepted verification must support or refute the claim")
        if self.relation in {"SUPPORTS", "REFUTES"} and (
            not self.evidence_excerpt or not self.evidence_location
        ):
            raise ValueError("SUPPORTS/REFUTES require excerpt and location")
        if self.error_code and not self.error_message:
            raise ValueError("verification error requires a safe error message")
        return self
