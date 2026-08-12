"""Command-line entry point for the cross-process durable execution spike."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Restrict checkpoint deserialization before importing the SQLite checkpointer.
os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from spikes.durable_execution.workflow import build_graph, initial_state

DEFAULT_DB_PATH = Path("checkpoints") / "spike.db"


def graph_config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def serialize_interrupts(tasks: Sequence[Any]) -> list[dict[str, Any]]:
    serialized: list[dict[str, Any]] = []
    for task in tasks:
        for item in task.interrupts:
            serialized.append({"id": item.id, "value": item.value})
    return serialized


def snapshot_payload(snapshot: Any, thread_id: str) -> dict[str, Any]:
    values = dict(snapshot.values) if snapshot.values else {}
    return {
        "thread_id": thread_id,
        "exists": bool(values),
        "state": values,
        "next": list(snapshot.next),
        "interrupts": serialize_interrupts(snapshot.tasks),
    }


def open_graph(db_path: Path) -> tuple[Any, Any]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    context = SqliteSaver.from_conn_string(str(db_path))
    checkpointer = context.__enter__()
    return context, build_graph(checkpointer)


def run_start(graph: Any, thread_id: str) -> dict[str, Any]:
    config = graph_config(thread_id)
    existing = graph.get_state(config)
    if existing.values:
        raise ValueError(f"thread already exists: {thread_id}")
    graph.invoke(initial_state(thread_id), config=config)
    return snapshot_payload(graph.get_state(config), thread_id)


def run_status(graph: Any, thread_id: str) -> dict[str, Any]:
    return snapshot_payload(graph.get_state(graph_config(thread_id)), thread_id)


def run_resume(graph: Any, thread_id: str, decision: str) -> dict[str, Any]:
    config = graph_config(thread_id)
    current = graph.get_state(config)
    if not current.values:
        raise ValueError(f"thread does not exist: {thread_id}")
    if not any(task.interrupts for task in current.tasks):
        raise ValueError(f"thread is not waiting for a decision: {thread_id}")
    graph.invoke(Command(resume=decision), config=config)
    return snapshot_payload(graph.get_state(config), thread_id)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start and pause a new task")
    start.add_argument("--thread-id", required=True)

    status = subparsers.add_parser("status", help="inspect persisted task state")
    status.add_argument("--thread-id", required=True)

    resume = subparsers.add_parser("resume", help="resume a paused task")
    resume.add_argument("--thread-id", required=True)
    resume.add_argument("--decision", choices=("approve", "reject"), required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    context, graph = open_graph(args.db.resolve())
    try:
        if args.command == "start":
            payload = run_start(graph, args.thread_id)
        elif args.command == "status":
            payload = run_status(graph, args.thread_id)
        else:
            payload = run_resume(graph, args.thread_id, args.decision)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    finally:
        context.__exit__(None, None, None)

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
