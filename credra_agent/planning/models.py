"""Bounded public contracts for Coordinator decisions and observations."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CoordinatorLimits(PlanningModel):
    """Explicit per-run limits; there are deliberately no production defaults."""

    max_decisions: int = Field(ge=1)
    no_progress_limit: int = Field(ge=1)
    model_attempt_reservation: int = Field(ge=1)
    decision_token_reservation: int = Field(ge=1)
    decision_max_output_tokens: int = Field(ge=1)

    @model_validator(mode="after")
    def output_fits_token_reservation(self) -> "CoordinatorLimits":
        if self.decision_max_output_tokens > self.decision_token_reservation:
            raise ValueError("output token limit exceeds total model token reservation")
        return self


class RunAuthorization(PlanningModel):
    """Trusted runtime input that binds approval, caps and limits to one TaskSpec."""

    schema_version: Literal["coordinator_run_authorization_v1"] = (
        "coordinator_run_authorization_v1"
    )
    authorization_id: str = Field(min_length=1, max_length=200)
    authorized_by: Literal["USER", "RUNTIME_POLICY", "OFFLINE_TEST"]
    task_spec_version: int = Field(ge=1)
    approval: Literal["UNCONFIRMED", "APPROVED"]
    external_request_limit: int | None = Field(default=None, ge=1)
    token_limit: int | None = Field(default=None, ge=1)
    active_seconds_limit: float | None = Field(default=None, gt=0)
    limits: CoordinatorLimits

    @model_validator(mode="after")
    def complete_caps_when_approved(self) -> "RunAuthorization":
        if self.approval == "APPROVED" and (
            self.external_request_limit is None
            or self.token_limit is None
            or self.active_seconds_limit is None
        ):
            raise ValueError("approved run authorization requires all budget caps")
        return self


class HypothesisState(PlanningModel):
    hypothesis_id: str = Field(min_length=1)
    question_id: str = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=1200)
    status: Literal["OPEN", "SUPPORTED", "REFUTED", "CONFLICTING", "UNRESOLVED"] = (
        "OPEN"
    )
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)


class HypothesisUpdate(PlanningModel):
    hypothesis_id: str = Field(min_length=1)
    status: Literal["OPEN", "SUPPORTED", "REFUTED", "CONFLICTING", "UNRESOLVED"]
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)


class Observation(PlanningModel):
    observation_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)
    status: Literal["SUCCESS", "NO_RESULT", "UNAVAILABLE", "MISSING_DATA", "FAILED"]
    summary: str = Field(min_length=1, max_length=2000)
    artifact_refs: list[str] = Field(default_factory=list, max_length=50)
    novelty_keys: list[str] = Field(default_factory=list, max_length=100)
    answered_question_ids: list[str] = Field(default_factory=list, max_length=50)
    gap_question_ids: list[str] = Field(default_factory=list, max_length=50)
    conflict_ids: list[str] = Field(default_factory=list, max_length=50)
    error_code: str | None = Field(default=None, max_length=80)


class ObservationIndexItem(PlanningModel):
    observation_id: str
    action_id: str
    status: str
    summary: str = Field(max_length=2000)
    artifact_refs: list[str] = Field(default_factory=list, max_length=50)
    novelty_keys: list[str] = Field(default_factory=list, max_length=100)


class Coverage(PlanningModel):
    required_question_ids: list[str] = Field(default_factory=list)
    answered_question_ids: list[str] = Field(default_factory=list)
    gap_question_ids: list[str] = Field(default_factory=list)
    unresolved_conflict_ids: list[str] = Field(default_factory=list)
    review_required: bool = False

    @property
    def complete(self) -> bool:
        required = set(self.required_question_ids)
        return (
            required <= set(self.answered_question_ids)
            and not self.gap_question_ids
            and not self.unresolved_conflict_ids
            and not self.review_required
        )


class DecisionDraft(PlanningModel):
    """Model output. Runtime-owned identifiers and versions are assigned later."""

    decision: Literal["ACTION", "FINISH"]
    tool: str | None = None
    arguments: dict = Field(default_factory=dict)
    hypothesis_ids: list[str] = Field(default_factory=list, max_length=50)
    hypothesis_updates: list[HypothesisUpdate] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    expected_observation: str | None = Field(default=None, max_length=1200)
    reason_summary: str = Field(min_length=1, max_length=1200)
    finish_reason: (
        Literal["ANSWERED", "BUDGET_EXHAUSTED", "NO_PROGRESS", "NEEDS_REVIEW"] | None
    ) = None
    answered_question_ids: list[str] = Field(default_factory=list, max_length=50)
    gap_question_ids: list[str] = Field(default_factory=list, max_length=50)
    conflict_ids: list[str] = Field(default_factory=list, max_length=50)
    review_required: bool = False
    limitations: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def coherent(self) -> "DecisionDraft":
        if self.decision == "ACTION":
            if not self.tool or not self.expected_observation or self.finish_reason:
                raise ValueError("ACTION requires tool/expected_observation only")
        elif (
            self.tool
            or self.arguments
            or self.expected_observation
            or not self.finish_reason
        ):
            raise ValueError("FINISH requires finish_reason and no action fields")
        return self


class CoordinatorResult(PlanningModel):
    status: Literal["COMPLETED", "LIMITED", "WAITING_CLARIFICATION", "FAILED"]
    stop_reason: str
    actions: list[dict] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    hypotheses: list[HypothesisState] = Field(default_factory=list)
    coverage: Coverage
    artifact_refs: list[str] = Field(default_factory=list)
    authorization_id: str
    budget: dict
    limitations: list[str] = Field(default_factory=list)
