"""Pure routing functions driven only by graph state."""

from typing import Literal

from app.graph.state import AgentState


def route_after_financial(state: AgentState) -> Literal["research", "risk"]:
    return "research" if state["anomaly_flags"] else "risk"


def route_after_risk(state: AgentState) -> Literal["approval", "report"]:
    return "approval" if state["risk_level"] in ("MEDIUM", "HIGH") else "report"


def route_after_approval(state: AgentState) -> Literal["research", "report"]:
    if state["human_decision"] == "research":
        return "research"
    if state["human_decision"] == "approve":
        return "report"
    raise ValueError("human decision must be approve or research")
