"""Tool-specific Action argument schemas."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from credra_agent.intent.models import Period, SourcePolicy


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReferenceArgs(ExecutionModel):
    reference_id: str = Field(min_length=1)


class SearchEvidenceArgs(ExecutionModel):
    query: str = Field(min_length=1, max_length=2000)
    subject_id: str = Field(min_length=1)
    subject_name: str = Field(min_length=1)
    period: Period
    source_policy: SourcePolicy
    category: str = Field(default="custom", min_length=1)


class VerifyClaimArgs(ExecutionModel):
    claim_id: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)


class ComputeMetricsArgs(ExecutionModel):
    metric_ids: list[str] = Field(min_length=1)
    input_refs: list[str] = Field(min_length=1)
    accounting_basis: Literal["CONSOLIDATED", "PARENT"]


class AskUserArgs(ExecutionModel):
    question: str = Field(min_length=1)
    unresolved_fields: list[str] = Field(min_length=1)


class FinishArgs(ExecutionModel):
    coverage_ref: str = Field(min_length=1)
    reason: Literal["ANSWERED", "BUDGET_EXHAUSTED", "NO_PROGRESS", "NEEDS_REVIEW"]


class ComparePeersArgs(ExecutionModel):
    subject_ids: list[str] = Field(min_length=2)
    input_refs: list[str] = Field(min_length=1)
    metric_ids: list[str] = Field(min_length=1)


class RunScenarioArgs(ExecutionModel):
    input_refs: list[str] = Field(min_length=1)
    assumption_ref: str = Field(min_length=1)
    formula_version: str = Field(min_length=1)


ToolName = Literal[
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


class Action(ExecutionModel):
    action_id: str = Field(min_length=1)
    task_spec_version: int = Field(ge=1)
    plan_version: int = Field(ge=1)
    tool: ToolName
    arguments: dict
    hypothesis_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    expected_observation: str = Field(min_length=1)
    reason_summary: str = Field(min_length=1)
    budget_ref: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def typed_arguments(self) -> "Action":
        from credra_agent.execution.registry import default_registry

        default_registry().validate(self.tool, self.arguments, require_available=False)
        return self
