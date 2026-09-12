"""Approved rubric aggregation; humans supply scores, no model self-grading."""

from pydantic import Field

from .contracts import Contract

DIMENSIONS = (
    "instruction_adherence",
    "investigation_decisions",
    "evidence_support",
    "financial_correctness",
    "completion_and_gaps",
)
HARD_FAILURES = frozenset(
    {
        "fabricated_critical_fact",
        "numeric_error",
        "unauthorized_action",
        "mandatory_review_skipped",
        "recovery_corrupted",
    }
)


class Review(Contract):
    instruction_adherence: int = Field(ge=0, le=5, strict=True)
    investigation_decisions: int = Field(ge=0, le=5, strict=True)
    evidence_support: int = Field(ge=0, le=5, strict=True)
    financial_correctness: int = Field(ge=0, le=5, strict=True)
    completion_and_gaps: int = Field(ge=0, le=5, strict=True)
    hard_failures: list[str]
    evidence_refs: list[str] = Field(min_length=1)
    reviewer: str = Field(min_length=1)

    def outcome(self) -> dict:
        if set(self.hard_failures) - HARD_FAILURES:
            raise ValueError("unknown hard failure")
        mean = sum(getattr(self, name) for name in DIMENSIONS) / len(DIMENSIONS)
        return {"mean": mean, "passed": mean >= 4 and not self.hard_failures}
