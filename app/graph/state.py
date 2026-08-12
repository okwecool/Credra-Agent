"""Minimal state contract for the long-running workflow."""

from typing import Literal, TypedDict

TaskStatus = Literal["CREATED", "RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED"]


class AgentState(TypedDict):
    task_id: str
    case_id: str
    status: TaskStatus
    current_node: str
    company_artifact: str | None
    financial_artifact: str | None
    research_artifact: str | None
    risk_artifact: str | None
    anomaly_flags: list[str]
    risk_level: str | None
    human_decision: str | None
    human_comment: str | None
    external_research_incomplete: bool


def initial_state(task_id: str, case_id: str) -> AgentState:
    return {
        "task_id": task_id,
        "case_id": case_id,
        "status": "CREATED",
        "current_node": "",
        "company_artifact": None,
        "financial_artifact": None,
        "research_artifact": None,
        "risk_artifact": None,
        "anomaly_flags": [],
        "risk_level": None,
        "human_decision": None,
        "human_comment": None,
        "external_research_incomplete": False,
    }
