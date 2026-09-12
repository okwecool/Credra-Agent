"""Bounded checkpoint state for the versioned agentic graph."""

from datetime import UTC, datetime
from typing import Literal, TypedDict

AgenticStatus = Literal[
    "CREATED",
    "RUNNING",
    "WAITING_CLARIFICATION",
    "LIMITED",
    "COMPLETED",
    "FAILED",
    "PAUSED_LOGGING",
]


class AgenticState(TypedDict):
    task_id: str
    case_id: str
    run_id: str
    graph_version: Literal["agentic_v2"]
    execution_mode: Literal["shadow", "agentic"]
    status: AgenticStatus
    current_node: str
    started_at: str
    task_spec_ref: str
    authorization_ref: str
    hypotheses_ref: str
    observation_index_ref: str
    coverage_ref: str | None
    active_action_id: str | None
    active_action_ref: str | None
    active_decision_ref: str | None
    iteration: int
    budget_ledger_ref: str
    stop_reason: str | None
    pending_instruction_version: int | None
    no_progress_count: int
    policy_rejections: list[dict[str, str]]
    artifact_refs: list[str]
    limitations: list[str]


def initial_agentic_state(
    *,
    task_id: str,
    case_id: str,
    run_id: str,
    execution_mode: Literal["shadow", "agentic"],
    task_spec_ref: str,
    authorization_ref: str,
    hypotheses_ref: str,
    observation_index_ref: str,
    budget_ledger_ref: str,
    artifact_refs: list[str],
) -> AgenticState:
    return {
        "task_id": task_id,
        "case_id": case_id,
        "run_id": run_id,
        "graph_version": "agentic_v2",
        "execution_mode": execution_mode,
        "status": "CREATED",
        "current_node": "",
        "started_at": datetime.now(UTC).isoformat(),
        "task_spec_ref": task_spec_ref,
        "authorization_ref": authorization_ref,
        "hypotheses_ref": hypotheses_ref,
        "observation_index_ref": observation_index_ref,
        "coverage_ref": None,
        "active_action_id": None,
        "active_action_ref": None,
        "active_decision_ref": None,
        "iteration": 0,
        "budget_ledger_ref": budget_ledger_ref,
        "stop_reason": None,
        "pending_instruction_version": None,
        "no_progress_count": 0,
        "policy_rejections": [],
        "artifact_refs": list(artifact_refs),
        "limitations": [],
    }
