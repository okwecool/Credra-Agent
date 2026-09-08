"""Executable contract prototypes, deliberately separate from production state."""

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Period(Contract):
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self) -> "Period":
        if self.end < self.start:
            raise ValueError("period end precedes start")
        return self


class SourcePolicy(Contract):
    preferred: list[str] = Field(default_factory=list)
    allowed: list[str] | None = None
    denied: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unambiguous(self) -> "SourcePolicy":
        if self.allowed is not None:
            if not self.allowed or set(self.allowed) & set(self.denied):
                raise ValueError("empty or contradictory source scope")
            if not set(self.preferred) <= set(self.allowed):
                raise ValueError("preferred source outside allowed scope")
        if set(self.preferred) & set(self.denied):
            raise ValueError("preferred source is denied")
        return self


class Question(Contract):
    question_id: str
    text: str = Field(min_length=1)
    priority: Literal["REQUIRED", "OPTIONAL"]
    completion_criteria: str = Field(min_length=1)


class ConditionalInstruction(Contract):
    instruction_id: str
    condition: str
    requested_action: str
    status: Literal["PENDING", "MET", "NOT_MET", "UNKNOWN"] = "PENDING"
    evidence_refs: list[str] = Field(default_factory=list)


class TaskSpec(Contract):
    schema_version: Literal["task_spec_v2"] = "task_spec_v2"
    version: int = Field(ge=1)
    source_message_id: str
    operation: Literal["start", "status", "amend", "pause", "resume", "approve_report"]
    subject_id: str | None
    subject_name: str
    as_of: date
    periods: list[Period]
    source_policy: SourcePolicy
    questions: list[Question] = Field(min_length=1)
    conditions: list[ConditionalInstruction] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_fields: list[str] = Field(default_factory=list)
    readiness: Literal["READY", "NEEDS_CLARIFICATION"]

    @model_validator(mode="after")
    def ready_means_resolved(self) -> "TaskSpec":
        if self.readiness == "READY" and (
            not self.subject_id or self.unresolved_fields
        ):
            raise ValueError("READY requires an unambiguous subject and constraints")
        if self.readiness == "NEEDS_CLARIFICATION" and not self.unresolved_fields:
            raise ValueError("clarification needs named unresolved fields")
        if any(period.end > self.as_of for period in self.periods):
            raise ValueError("observed financial period extends beyond as_of")
        ids = [q.question_id for q in self.questions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate question id")
        return self


class Hypothesis(Contract):
    hypothesis_id: str
    question_id: str
    statement: str
    alternatives: list[str]
    status: Literal["OPEN", "SUPPORTED", "REFUTED", "CONFLICTING", "UNRESOLVED"]
    supporting_refs: list[str] = Field(default_factory=list)
    refuting_refs: list[str] = Field(default_factory=list)


class Plan(Contract):
    plan_id: str
    version: int = Field(ge=1)
    task_spec_version: int = Field(ge=1)
    previous_plan_id: str | None = None
    question_ids: list[str]
    hypothesis_ids: list[str]
    pending_action_ids: list[str]
    reason_summary: str


class SearchArgs(Contract):
    query: str = Field(min_length=1)
    subject_id: str
    period: Period
    source_policy: SourcePolicy


class ReferenceArgs(Contract):
    reference_id: str


class VerifyArgs(Contract):
    claim_id: str
    source_refs: list[str] = Field(min_length=1)


class AnalysisArgs(Contract):
    metric_ids: list[str] = Field(min_length=1)
    input_refs: list[str] = Field(min_length=1)
    accounting_basis: Literal["CONSOLIDATED", "PARENT"]


class PeerArgs(Contract):
    subject_ids: list[str] = Field(min_length=2)
    input_refs: list[str] = Field(min_length=1)
    metric_ids: list[str] = Field(min_length=1)


class ScenarioArgs(Contract):
    input_refs: list[str] = Field(min_length=1)
    assumption_ref: str
    formula_version: str


class AskArgs(Contract):
    question: str
    unresolved_fields: list[str] = Field(min_length=1)


class FinishArgs(Contract):
    coverage_ref: str
    reason: Literal["ANSWERED", "BUDGET_EXHAUSTED", "NO_PROGRESS", "NEEDS_REVIEW"]


class Action(Contract):
    action_id: str
    task_spec_version: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    tool: Literal[
        "read_document",
        "search_evidence",
        "fetch_content",
        "verify_claim",
        "compute_metrics",
        "compare_peers",
        "run_scenario",
        "ask_user",
        "finish",
    ]
    arguments: dict
    hypothesis_ids: list[str]
    evidence_refs: list[str]
    expected_observation: str
    reason_summary: str

    @model_validator(mode="after")
    def typed_arguments(self) -> "Action":
        schema = {
            "read_document": ReferenceArgs,
            "fetch_content": ReferenceArgs,
            "search_evidence": SearchArgs,
            "verify_claim": VerifyArgs,
            "compute_metrics": AnalysisArgs,
            "compare_peers": PeerArgs,
            "run_scenario": ScenarioArgs,
            "ask_user": AskArgs,
            "finish": FinishArgs,
        }[self.tool]
        schema.model_validate(self.arguments)
        return self


class Observation(Contract):
    observation_id: str
    action_id: str
    attempt: int = Field(ge=1)
    status: Literal["SUCCESS", "NO_RESULT", "UNAVAILABLE", "MISSING_DATA", "FAILED"]
    artifact_refs: list[str]
    error_code: str | None = None

    @model_validator(mode="after")
    def failure_has_reason(self) -> "Observation":
        if (
            self.status in {"FAILED", "UNAVAILABLE", "MISSING_DATA"}
            and not self.error_code
        ):
            raise ValueError("incomplete observation needs a reason")
        if self.status == "SUCCESS" and self.error_code:
            raise ValueError("successful observation cannot carry an error")
        return self


class ExecutionRecord(Contract):
    """Durable action ledger contract; this prototype performs no side effects."""

    action_id: str
    task_spec_version: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    budget_reservation_id: str
    attempt: int = Field(ge=1)
    status: Literal["RESERVED", "DISPATCHED", "RESULT_STORED", "UNCERTAIN", "FAILED"]
    observation_ref: str | None = None
    usage_uncertain: bool = False

    @model_validator(mode="after")
    def stored_result_has_reference(self) -> "ExecutionRecord":
        if self.status == "RESULT_STORED" and not self.observation_ref:
            raise ValueError("stored result requires a durable observation reference")
        if self.status == "UNCERTAIN" and not self.usage_uncertain:
            raise ValueError("uncertain dispatch must retain conservative accounting")
        return self


class Coverage(Contract):
    required_question_ids: list[str]
    answered_question_ids: list[str]
    gap_question_ids: list[str]
    unresolved_conflicts: list[str]
    review_required: bool

    @model_validator(mode="after")
    def classify_every_required_question(self) -> "Coverage":
        answered, gaps = set(self.answered_question_ids), set(self.gap_question_ids)
        if answered & gaps or not set(self.required_question_ids) <= answered | gaps:
            raise ValueError("required questions must be answered or disclosed as gaps")
        if self.unresolved_conflicts and not self.review_required:
            raise ValueError("unresolved conflicts require review")
        return self


class BudgetLedger(Contract):
    approval: Literal["UNCONFIRMED", "APPROVED"]
    external_limit: int | None = Field(default=None, ge=0)
    external_spent: int = Field(ge=0)
    external_reserved: int = Field(ge=0)
    token_limit: int | None = Field(default=None, ge=0)
    tokens_spent: int = Field(ge=0)
    tokens_reserved: int = Field(ge=0)
    active_seconds_limit: Decimal | None = Field(default=None, gt=0)
    usage_uncertain: bool = False

    @model_validator(mode="after")
    def reservations_fit(self) -> "BudgetLedger":
        for limit, used, held in (
            (self.external_limit, self.external_spent, self.external_reserved),
            (self.token_limit, self.tokens_spent, self.tokens_reserved),
        ):
            if limit is not None and held > max(0, limit - used):
                raise ValueError("reservation exceeds remaining limit")
        if self.approval == "APPROVED" and (
            self.external_limit is None
            or self.token_limit is None
            or self.active_seconds_limit is None
        ):
            raise ValueError("approved budget needs explicit caps")
        return self


class Evidence(Contract):
    evidence_id: str
    subject_id: str
    original_source_id: str
    publisher: str
    published_at: date | None
    retrieved_at: datetime
    event_date: date | None
    document_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    location: str
    excerpt: str
    source_kind: Literal["REAL", "SYNTHETIC"]
    reliability: Literal["PRIMARY", "SECONDARY", "UNKNOWN"]
    relation: Literal["SUPPORTS", "REFUTES", "IRRELEVANT", "INSUFFICIENT"]
    verification_status: Literal["PENDING", "ACCEPTED", "REJECTED"]
    verifier_version: str | None

    @model_validator(mode="after")
    def accepted_has_verifier(self) -> "Evidence":
        if self.verification_status == "ACCEPTED" and not self.verifier_version:
            raise ValueError("accepted evidence requires verifier provenance")
        return self


class Claim(Contract):
    claim_id: str
    subject_id: str
    statement: str
    status: Literal["UNVERIFIED", "SUPPORTED", "REFUTED", "CONFLICTING", "UNRESOLVED"]
    supporting_evidence_ids: list[str]
    refuting_evidence_ids: list[str]

    @model_validator(mode="after")
    def support_requires_references(self) -> "Claim":
        if (
            self.status in {"SUPPORTED", "CONFLICTING"}
            and not self.supporting_evidence_ids
        ):
            raise ValueError("supported claim needs evidence")
        if self.status in {"REFUTED", "CONFLICTING"} and not self.refuting_evidence_ids:
            raise ValueError("refuted claim needs evidence")
        return self


class Assessment(Contract):
    rule_risk_level: Literal["LOW", "MEDIUM", "HIGH"]
    analysis_summary: str
    claim_ids: list[str]
    metric_refs: list[str]
    limitations: list[str]
    review_required: bool

    @model_validator(mode="after")
    def preserve_review_floor(self) -> "Assessment":
        if self.rule_risk_level != "LOW" and not self.review_required:
            raise ValueError("rule review floor cannot be bypassed")
        return self


class FinancialDatum(Contract):
    metric: str
    period: Period
    accounting_basis: Literal["CONSOLIDATED", "PARENT"]
    profit_attribution: Literal["GROUP_TOTAL", "OWNERS_OF_PARENT", "NOT_APPLICABLE"]
    currency: Literal["CNY"]
    source_unit: str
    normalized_unit: Literal["CNY_1000"]
    revision: str
    value: Decimal | None
    missing_reason: str | None = None
    source_refs: list[str]

    @model_validator(mode="after")
    def missing_is_not_zero(self) -> "FinancialDatum":
        if self.metric == "net_profit" and self.profit_attribution == "NOT_APPLICABLE":
            raise ValueError("net profit requires explicit attribution")
        if self.value is None and not self.missing_reason:
            raise ValueError("missing financial datum needs a reason")
        if self.value is not None and (self.missing_reason or not self.source_refs):
            raise ValueError("observed datum needs lineage, not a missing reason")
        return self


class MetricResult(Contract):
    metric_id: str
    status: Literal["COMPUTED", "NOT_COMPUTABLE"]
    value: Decimal | None
    reason: str | None
    formula_version: str
    input_refs: list[str]

    @model_validator(mode="after")
    def result_matches_status(self) -> "MetricResult":
        if self.status == "COMPUTED" and (
            self.value is None or not self.input_refs or self.reason
        ):
            raise ValueError("computed result needs a value and input lineage")
        if self.status == "NOT_COMPUTABLE" and (
            self.value is not None or not self.reason
        ):
            raise ValueError("not computable must not manufacture a number")
        return self


class LegacyVersion(Contract):
    graph_version: Literal["legacy_v1"]
    execution_mode: Literal["baseline"]


class AgenticVersion(Contract):
    graph_version: Literal["agentic_v2"]
    execution_mode: Literal["shadow", "agentic"]


GraphVersion = Annotated[
    LegacyVersion | AgenticVersion, Field(discriminator="graph_version")
]


def resolve_graph_version(state: dict) -> LegacyVersion | AgenticVersion:
    """Read-only prototype: old checkpoints resolve to legacy; unknown versions fail."""
    from pydantic import TypeAdapter

    if "graph_version" not in state:
        if state.get("execution_mode") not in (None, "baseline"):
            raise ValueError("non-legacy state requires an explicit graph version")
        return LegacyVersion(graph_version="legacy_v1", execution_mode="baseline")
    return TypeAdapter(GraphVersion).validate_python(
        {key: state[key] for key in ("graph_version", "execution_mode")}
    )
