"""Append-only JSONL tracing with deliberately small summaries."""

import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from app.models.trace import TraceEvent, TraceStatus


class TraceWriter:
    """Write one independently parseable JSON object per trace event."""

    def __init__(self, trace_dir: Path) -> None:
        self.trace_dir = trace_dir

    def write(
        self,
        *,
        task_id: str,
        node: str,
        event_type: str,
        status: TraceStatus,
        start_time: datetime,
        end_time: datetime,
        latency_ms: int,
        input_summary: str | None = None,
        output_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        event = TraceEvent(
            task_id=task_id,
            node=node,
            event_type=event_type,
            input_summary=input_summary,
            output_summary=output_summary,
            start_time=start_time,
            end_time=end_time,
            latency_ms=latency_ms,
            status=status,
            error=error,
        )
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        path = self.trace_dir / f"{task_id}.jsonl"
        with path.open("a", encoding="utf-8") as file:
            file.write(event.model_dump_json() + "\n")

    def instant(
        self,
        *,
        task_id: str,
        node: str,
        event_type: str,
        status: TraceStatus = TraceStatus.SUCCESS,
        input_summary: str | None = None,
        output_summary: str | None = None,
        error: str | None = None,
    ) -> None:
        now = datetime.now(UTC)
        self.write(
            task_id=task_id,
            node=node,
            event_type=event_type,
            status=status,
            start_time=now,
            end_time=now,
            latency_ms=0,
            input_summary=input_summary,
            output_summary=output_summary,
            error=error,
        )


class TimedTrace:
    """Small timer helper for node and tool calls."""

    def __init__(self) -> None:
        self.start_time = datetime.now(UTC)
        self._start_counter = perf_counter()

    def finish(self) -> tuple[datetime, int]:
        end_time = datetime.now(UTC)
        latency_ms = max(0, round((perf_counter() - self._start_counter) * 1000))
        return end_time, latency_ms


def read_trace(path: Path) -> list[dict]:
    """Read a trace file while asserting every non-empty line is valid JSON."""

    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
