"""Pure routing functions driven only by graph state."""

from typing import Literal

from app.graph.state import AgentState


def route_after_financial(state: AgentState) -> Literal["research", "risk", "end"]:
    if state["status"] == "FAILED":
        return "end"
    return "research" if state["anomaly_flags"] else "risk"


def route_after_risk(state: AgentState) -> Literal["approval", "report", "end"]:
    if state["status"] == "FAILED":
        return "end"
    return "approval" if state["risk_level"] in ("MEDIUM", "HIGH") else "report"


def route_after_approval(state: AgentState) -> Literal["research", "report", "end"]:
    if state["status"] == "FAILED":
        return "end"
    if state["human_decision"] == "research":
        return "research"
    if state["human_decision"] == "approve":
        return "report"
    raise ValueError("human decision must be approve or research")
