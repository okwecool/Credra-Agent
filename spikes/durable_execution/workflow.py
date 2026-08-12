"""Minimal durable LangGraph workflow used by the Day 0 spike."""

from __future__ import annotations

from typing import Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

Decision = Literal["approve", "reject"]
TaskStatus = Literal["CREATED", "WAITING_APPROVAL", "RUNNING", "COMPLETED"]


class SpikeState(TypedDict):
    """Small, JSON-serializable state for the persistence experiment."""

    task_id: str
    status: TaskStatus
    prepare_runs: int
    decision: Decision | None
    result: str | None


def prepare_node(state: SpikeState) -> dict[str, object]:
    """Represent work that must not run again after the interrupt."""

    return {
        "status": "WAITING_APPROVAL",
        "prepare_runs": state["prepare_runs"] + 1,
    }


def approval_node(state: SpikeState) -> dict[str, object]:
    """Pause safely and receive a decision when the graph is resumed.

    LangGraph restarts this node from the beginning on resume, so everything
    before ``interrupt`` is intentionally free of side effects.
    """

    decision = interrupt(
        {
            "task_id": state["task_id"],
            "question": "Continue this durable execution task?",
            "allowed_decisions": ["approve", "reject"],
        }
    )
    if decision not in ("approve", "reject"):
        raise ValueError("decision must be 'approve' or 'reject'")
    return {"decision": decision, "status": "RUNNING"}


def complete_node(state: SpikeState) -> dict[str, object]:
    """Finish the task after the human decision has been persisted."""

    result = "approved" if state["decision"] == "approve" else "rejected"
    return {"status": "COMPLETED", "result": result}


def build_graph(checkpointer: BaseCheckpointSaver) -> CompiledStateGraph:
    """Build a fresh graph around a durable checkpointer."""

    builder = StateGraph(SpikeState)
    builder.add_node("prepare", prepare_node)
    builder.add_node("approval", approval_node)
    builder.add_node("complete", complete_node)
    builder.add_edge(START, "prepare")
    builder.add_edge("prepare", "approval")
    builder.add_edge("approval", "complete")
    builder.add_edge("complete", END)
    return builder.compile(checkpointer=checkpointer)


def initial_state(thread_id: str) -> SpikeState:
    """Create the deterministic input state for a new spike thread."""

    return {
        "task_id": thread_id,
        "status": "CREATED",
        "prepare_runs": 0,
        "decision": None,
        "result": None,
    }
