"""Pure routing functions driven only by graph state."""

from typing import Literal

from app.graph.state import AgentState


def route_after_financial(state: AgentState) -> Literal["research", "risk"]:
    return "research" if state["anomaly_flags"] else "risk"
