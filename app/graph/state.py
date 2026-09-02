"""Minimal state contract for the long-running workflow."""

from typing import Literal, TypedDict

TaskStatus = Literal["CREATED", "RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED"]


class AgentState(TypedDict):
    task_id: str
    case_id: str
    run_id: str | None
    status: TaskStatus
    current_node: str
    company_artifact: str | None
    financial_artifact: str | None
    research_artifact: str | None
    risk_artifact: str | None
    investigation_intent_artifact: str | None
    query_plan_artifact: str | None
    evidence_summary_artifact: str | None
    query_proposal_artifact: str | None
    risk_narrative_artifact: str | None
    report_artifact: str | None
    anomaly_flags: list[str]
    risk_level: str | None
    human_decision: str | None
    human_comment: str | None
    external_research_incomplete: bool
    failed_node: str | None
    error_summary: str | None


def initial_state(task_id: str, case_id: str, run_id: str | None = None) -> AgentState:
    return {
        "task_id": task_id,
        "case_id": case_id,
        "run_id": run_id,
        "status": "CREATED",
        "current_node": "",
        "company_artifact": None,
        "financial_artifact": None,
        "research_artifact": None,
        "risk_artifact": None,
        "investigation_intent_artifact": None,
        "query_plan_artifact": None,
        "evidence_summary_artifact": None,
        "query_proposal_artifact": None,
        "risk_narrative_artifact": None,
        "report_artifact": None,
        "anomaly_flags": [],
        "risk_level": None,
        "human_decision": None,
        "human_comment": None,
        "external_research_incomplete": False,
        "failed_node": None,
        "error_summary": None,
    }
