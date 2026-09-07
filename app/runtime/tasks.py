"""SQLite-backed start, inspect, and resume operations for Credra tasks."""

import hashlib
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
from app.llm.gateway import StructuredModel
from app.runtime.fault import ResearchFaultInjector
from app.runtime.tracing import TraceWriter


def _runtime_services(settings: Settings) -> tuple[TraceWriter, ResearchFaultInjector]:
    trace = TraceWriter(settings.trace_dir)
    fault = ResearchFaultInjector(
        settings.trace_dir / ".fault_state", settings.research_fail_first
    )
    return trace, fault


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


def run_id_for_thread(thread_id: str) -> str:
    """Create a stable filesystem-safe run id without trusting thread text as a path."""

    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:20]


def _run_dir_from_checkpoint(
    checkpointer: SqliteSaver, case_dir: Path, thread_id: str
) -> Path:
    checkpoint = checkpointer.get_tuple(graph_config(thread_id))
    values = checkpoint.checkpoint.get("channel_values", {}) if checkpoint else {}
    run_id = values.get("run_id")
    if isinstance(run_id, str) and run_id:
        return case_dir / "runs" / run_id
    # Explicit compatibility: checkpoints created before M2.2 keep their case-level paths.
    return case_dir


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
    analysis_model: StructuredModel | None = None,
) -> dict[str, Any]:
    trace, fault = _runtime_services(settings)
    trace.instant(
        task_id=thread_id,
        node="runtime",
        event_type="TASK_START",
        input_summary=f"case_id={case_id}",
    )
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        config = graph_config(thread_id)
        if checkpointer.get_tuple(config) is not None:
            raise ValueError(f"thread already exists: {thread_id}")
        case_dir = _case_dir(settings, case_id)
        run_id = run_id_for_thread(thread_id)
        graph = build_workflow(
            case_dir,
            settings,
            research_client,
            checkpointer,
            trace,
            fault,
            run_dir=case_dir / "runs" / run_id,
            analysis_model=analysis_model,
        )
        graph.invoke(initial_state(thread_id, case_id, run_id), config=config)
        payload = snapshot_payload(graph.get_state(config), thread_id)
        trace.instant(
            task_id=thread_id,
            node="runtime",
            event_type="TASK_STATE",
            output_summary=f"status={payload['state'].get('status')}",
        )
        return payload


def get_task_status(
    *,
    thread_id: str,
    settings: Settings,
    research_client: Any | None = None,
    analysis_model: StructuredModel | None = None,
) -> dict[str, Any]:
    trace, fault = _runtime_services(settings)
    trace.instant(
        task_id=thread_id,
        node="runtime",
        event_type="STATUS_QUERY",
    )
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        case_id = _case_id_from_checkpoint(checkpointer, thread_id)
        case_dir = _case_dir(settings, case_id)
        graph = build_workflow(
            case_dir,
            settings,
            research_client,
            checkpointer,
            trace,
            fault,
            run_dir=_run_dir_from_checkpoint(checkpointer, case_dir, thread_id),
            analysis_model=analysis_model,
        )
        return snapshot_payload(graph.get_state(graph_config(thread_id)), thread_id)


def resume_task(
    *,
    thread_id: str,
    decision: str,
    comment: str | None,
    settings: Settings,
    research_client: Any | None = None,
    analysis_model: StructuredModel | None = None,
) -> dict[str, Any]:
    if decision not in ("approve", "research"):
        raise ValueError("decision must be approve or research")
    trace, fault = _runtime_services(settings)
    trace.instant(
        task_id=thread_id,
        node="runtime",
        event_type="RESUME",
        input_summary=f"decision={decision}",
    )
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        case_id = _case_id_from_checkpoint(checkpointer, thread_id)
        config = graph_config(thread_id)
        case_dir = _case_dir(settings, case_id)
        graph = build_workflow(
            case_dir,
            settings,
            research_client,
            checkpointer,
            trace,
            fault,
            run_dir=_run_dir_from_checkpoint(checkpointer, case_dir, thread_id),
            analysis_model=analysis_model,
        )
        snapshot = graph.get_state(config)
        if not any(task.interrupts for task in snapshot.tasks):
            raise ValueError(f"thread is not waiting for approval: {thread_id}")
        graph.invoke(
            Command(resume={"decision": decision, "comment": comment}),
            config=config,
        )
        payload = snapshot_payload(graph.get_state(config), thread_id)
        trace.instant(
            task_id=thread_id,
            node="runtime",
            event_type="TASK_STATE",
            output_summary=f"status={payload['state'].get('status')}",
        )
        return payload
