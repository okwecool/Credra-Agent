"""SQLite-backed start, inspect, and resume operations for Credra tasks."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.config import Settings
from app.graph.state import initial_state
from app.graph.workflow import build_workflow


def graph_config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _serialize_interrupts(snapshot: Any) -> list[dict[str, Any]]:
    return [
        {"id": item.id, "value": item.value}
        for task in snapshot.tasks
        for item in task.interrupts
    ]


def snapshot_payload(snapshot: Any, thread_id: str) -> dict[str, Any]:
    values = dict(snapshot.values) if snapshot.values else {}
    return {
        "thread_id": thread_id,
        "exists": bool(values),
        "state": values,
        "next": list(snapshot.next),
        "interrupts": _serialize_interrupts(snapshot),
    }


@contextmanager
def open_checkpointer(db_path: Path) -> Iterator[SqliteSaver]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(db_path.resolve())) as checkpointer:
        yield checkpointer


def _case_id_from_checkpoint(checkpointer: SqliteSaver, thread_id: str) -> str:
    checkpoint = checkpointer.get_tuple(graph_config(thread_id))
    if checkpoint is None:
        raise ValueError(f"thread does not exist: {thread_id}")
    case_id = checkpoint.checkpoint.get("channel_values", {}).get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"thread has no persisted case_id: {thread_id}")
    return case_id


def _case_dir(settings: Settings, case_id: str) -> Path:
    case_dir = settings.data_dir / case_id
    if not (case_dir / "source").is_dir():
        raise ValueError(f"case does not exist: {case_id}")
    return case_dir


def start_task(
    *,
    thread_id: str,
    case_id: str,
    settings: Settings,
    research_client: Any | None = None,
) -> dict[str, Any]:
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        config = graph_config(thread_id)
        if checkpointer.get_tuple(config) is not None:
            raise ValueError(f"thread already exists: {thread_id}")
        graph = build_workflow(
            _case_dir(settings, case_id), settings, research_client, checkpointer
        )
        graph.invoke(initial_state(thread_id, case_id), config=config)
        return snapshot_payload(graph.get_state(config), thread_id)


def get_task_status(
    *,
    thread_id: str,
    settings: Settings,
    research_client: Any | None = None,
) -> dict[str, Any]:
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        case_id = _case_id_from_checkpoint(checkpointer, thread_id)
        graph = build_workflow(
            _case_dir(settings, case_id), settings, research_client, checkpointer
        )
        return snapshot_payload(graph.get_state(graph_config(thread_id)), thread_id)


def resume_task(
    *,
    thread_id: str,
    decision: str,
    comment: str | None,
    settings: Settings,
    research_client: Any | None = None,
) -> dict[str, Any]:
    if decision not in ("approve", "research"):
        raise ValueError("decision must be approve or research")
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        case_id = _case_id_from_checkpoint(checkpointer, thread_id)
        config = graph_config(thread_id)
        graph = build_workflow(
            _case_dir(settings, case_id), settings, research_client, checkpointer
        )
        snapshot = graph.get_state(config)
        if not any(task.interrupts for task in snapshot.tasks):
            raise ValueError(f"thread is not waiting for approval: {thread_id}")
        graph.invoke(
            Command(resume={"decision": decision, "comment": comment}),
            config=config,
        )
        return snapshot_payload(graph.get_state(config), thread_id)
