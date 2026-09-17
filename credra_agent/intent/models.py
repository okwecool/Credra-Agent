"""Production contracts for natural-language task interpretation."""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Operation = Literal["start", "status", "amend", "pause", "resume", "approve_report"]


class IntentModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Period(IntentModel):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> "Period":
        if self.end < self.start:
            raise ValueError("period end precedes start")
        return self


class SourcePolicy(IntentModel):
    preferred: list[str] = Field(default_factory=list)
    allowed: list[str] | None = None
    denied: list[str] = Field(default_factory=list)

    @staticmethod
    def canonical_tags(values) -> set[str]:
        """One declared synonym; retain all other channel and category tags."""
        return {
            "exchange_disclosure" if value == "exchange" else value for value in values
        }

    def permits(self, values) -> bool:
        tags = self.canonical_tags(values)
        return bool(
            not tags & self.canonical_tags(self.denied)
            and (not self.denied or tags)
            and (self.allowed is None or tags & self.canonical_tags(self.allowed))
        )

    @model_validator(mode="after")
    def unambiguous(self) -> "SourcePolicy":
        preferred = self.canonical_tags(self.preferred)
        denied = self.canonical_tags(self.denied)
        if preferred & denied:
            raise ValueError("preferred source is denied")
        if self.allowed is not None:
            allowed = self.canonical_tags(self.allowed)
            if not allowed or allowed & denied:
                raise ValueError("empty or contradictory source scope")
            if not preferred <= allowed:
                raise ValueError("preferred source outside allowed scope")
        return self


class Question(IntentModel):
    question_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    priority: Literal["REQUIRED", "OPTIONAL"] = "REQUIRED"
    completion_criteria: str = Field(min_length=1)
    required_metric_ids: list[str] = Field(default_factory=list, max_length=20)
    minimum_verified_findings: int = Field(default=0, ge=0, le=20)
    required_finding_aspects: list[str] = Field(default_factory=list, max_length=20)
    focus: Literal[
        "cash_quality",
        "receivables",
        "guarantee",
        "regulatory",
        "legal",
        "debt",
        "general",
    ] = "general"

    @model_validator(mode="after")
    def finding_requirements_are_distinct(self) -> "Question":
        if len(set(self.required_finding_aspects)) != len(
            self.required_finding_aspects
        ):
            raise ValueError("required finding aspects must be distinct")
        if self.required_finding_aspects and self.minimum_verified_findings < len(
            self.required_finding_aspects
        ):
            raise ValueError("minimum findings cannot be below required aspects")
        return self


class ConditionalInstruction(IntentModel):
    instruction_id: str = Field(min_length=1)
    condition: str = Field(min_length=1)
    requested_action: str = Field(min_length=1)
    status: Literal["PENDING", "MET", "NOT_MET", "UNKNOWN"] = "PENDING"
    evidence_refs: list[str] = Field(default_factory=list)


class TaskSpec(IntentModel):
    schema_version: Literal["task_spec_v2"] = "task_spec_v2"
    version: int = Field(ge=1)
    source_message_id: str = Field(min_length=1)
    operation: Literal["start", "amend"]
    subject_id: str | None
    subject_name: str = ""
    case_id: str | None = None
    as_of: date
    periods: list[Period]
    comparison_periods: list[Period] = Field(default_factory=list)
    source_policy: SourcePolicy
    questions: list[Question] = Field(min_length=1)
    conditions: list[ConditionalInstruction] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    readiness: Literal["READY", "NEEDS_CLARIFICATION"]

    @model_validator(mode="after")
    def coherent(self) -> "TaskSpec":
        if self.readiness == "READY" and (
            not self.subject_id or not self.case_id or self.unresolved_fields
        ):
            raise ValueError(
                "READY requires an imported subject and resolved constraints"
            )
        if self.readiness == "NEEDS_CLARIFICATION" and not self.unresolved_fields:
            raise ValueError("clarification needs named unresolved fields")
        if any(
            period.end > self.as_of
            for period in [*self.periods, *self.comparison_periods]
        ):
            raise ValueError("period extends beyond as_of")
        question_ids = [item.question_id for item in self.questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("duplicate question id")
        return self


class IntentResult(IntentModel):
    schema_version: Literal["intent_result_v2"] = "intent_result_v2"
    source_message_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    operation: Operation
    task_spec: TaskSpec | None = None
    bound_task_spec_version: int | None = Field(default=None, ge=1)
    parser_mode: Literal["LLM", "RULE_FALLBACK"]
    duplicate: bool = False
    execution_status: Literal["NOT_STARTED", "CONTROL_PENDING"] = "NOT_STARTED"
    warnings: list[str] = Field(default_factory=list)
    rejected_instructions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def command_binding(self) -> "IntentResult":
        if self.operation in {"start", "amend"} and self.task_spec is None:
            raise ValueError("start/amend needs a TaskSpec snapshot")
        if (
            self.operation not in {"start", "amend"}
            and self.bound_task_spec_version is None
        ):
            raise ValueError("control command must bind a persisted TaskSpec")
        return self


class IntentDraft(IntentModel):
    """LLM output; trusted identifiers and dates are resolved after generation."""

    operation: Operation
    subject_hint: str | None = None
    years: list[int] = Field(default_factory=list)
    comparison_years: list[int] = Field(default_factory=list)
    periods: list[Period] = Field(default_factory=list)
    as_of: date | None = None
    focus: list[
        Literal[
            "cash_quality",
            "receivables",
            "guarantee",
            "regulatory",
            "legal",
            "debt",
            "general",
        ]
    ] = Field(default_factory=lambda: ["general"])
    preferred_sources: list[str] = Field(default_factory=list)
    allowed_sources: list[str] | None = None
    denied_sources: list[str] = Field(default_factory=list)
    condition: str | None = None
    requested_action: str | None = None
    unresolved_fields: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    rejected_instructions: list[str] = Field(default_factory=list)
