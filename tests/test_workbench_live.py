"""M3-D4 live Trace projections remain isolated, bounded, and deterministic."""

from datetime import UTC, datetime
from pathlib import Path

from app.models.trace import TraceEvent, TraceStatus
from app.workbench_live import (
    live_progress_markdown,
    project_live_progress,
    read_live_trace_events,
    safe_live_trace_path,
)


def _event(
    node: str,
    event_type: str,
    *,
    status: TraceStatus = TraceStatus.SUCCESS,
) -> dict:
    now = datetime(2026, 9, 7, tzinfo=UTC)
    return TraceEvent(
        task_id="live-001",
        node=node,
        event_type=event_type,
        start_time=now,
        end_time=now,
        latency_ms=0,
        status=status,
    ).model_dump(mode="json")


def test_live_projection_tracks_running_nodes_and_optional_skips() -> None:
    events = [
        _event("document", "NODE_START"),
        _event("document", "NODE_END"),
        _event("financial", "NODE_START"),
        _event("financial", "NODE_END"),
        _event("risk", "NODE_START"),
    ]

    projection = project_live_progress(
        events, thread_id="live-001", case_id="case_saic_600104"
    )
    statuses = {node["id"]: node["status"] for node in projection["nodes"]}

    assert projection["workflowStatus"] == "RUNNING"
    assert projection["currentNode"] == "risk"
    assert projection["progress"] == {"processed": 3, "total": 6, "percent": 50}
    assert statuses["document"] == "done"
    assert statuses["financial"] == "done"
    assert statuses["research"] == "skipped"
    assert statuses["risk"] == "running"
    assert "case_saic_600104" not in live_progress_markdown(projection)


def test_live_projection_converges_interrupt_resume_and_completion() -> None:
    events = [
        _event("document", "NODE_START"),
        _event("document", "NODE_END"),
        _event("financial", "NODE_START"),
        _event("financial", "NODE_END"),
        _event("risk", "NODE_START"),
        _event("risk", "NODE_END"),
        _event("approval", "NODE_START"),
        _event("approval", "INTERRUPT"),
    ]
    waiting = project_live_progress(events, thread_id="live-001", case_id=None)
    assert waiting["workflowStatus"] == "WAITING_APPROVAL"

    events.extend(
        [
            _event("runtime", "RESUME"),
            _event("approval", "NODE_START"),
            _event("approval", "NODE_END"),
            _event("report", "NODE_START"),
            _event("report", "NODE_END"),
        ]
    )
    completed = project_live_progress(events, thread_id="live-001", case_id=None)
    statuses = {node["id"]: node["status"] for node in completed["nodes"]}

    assert completed["workflowStatus"] == "COMPLETED"
    assert completed["progress"]["percent"] == 100
    assert statuses["approval"] == "done"
    assert statuses["report"] == "done"


def test_live_projection_marks_failed_node_without_raw_error() -> None:
    failed = _event("research", "NODE_END", status=TraceStatus.FAILED)
    failed["error"] = "api_key=must-not-leak"
    failed["event_type"] = "NODE_END"

    projection = project_live_progress(
        [failed], thread_id="live-001", case_id="case_risky"
    )

    assert projection["workflowStatus"] == "FAILED"
    assert projection["currentNode"] == "research"
    assert "must-not-leak" not in str(projection)
    assert "must-not-leak" not in live_progress_markdown(projection)

    failed["event_type"] = "[unsafe](javascript:alert(1))"
    bounded = project_live_progress(
        [failed], thread_id="live-001", case_id="case_risky"
    )
    assert bounded["lastEvent"]["eventType"] == "OTHER"


def test_live_projection_exposes_only_public_model_phase() -> None:
    event = _event("risk", "LLM_PROCESS_PROGRESS")
    event["output_summary"] = "private reasoning must not leak"

    projection = project_live_progress(
        [event], thread_id="live-001", case_id="case_risky"
    )
    rendered = live_progress_markdown(projection)

    assert projection["lastEvent"]["eventType"] == "LLM_PROCESS_PROGRESS"
    assert "模型持续思考中" in rendered
    assert "private reasoning" not in str(projection)
    assert "private reasoning" not in rendered


def test_live_trace_reader_ignores_partial_tail_and_rejects_escape(
    tmp_path: Path,
) -> None:
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    complete = _event("document", "NODE_START")
    path = trace_dir / "live-001.jsonl"
    path.write_text(
        TraceEvent.model_validate(complete).model_dump_json() + "\n" + '{"partial":',
        encoding="utf-8",
    )

    events = read_live_trace_events(trace_dir, "live-001")

    assert len(events) == 1
    assert events[0]["event_type"] == "NODE_START"
    assert safe_live_trace_path(trace_dir, "../escape") is None
    assert read_live_trace_events(trace_dir, "../escape") == []


def test_live_trace_reader_isolates_threads_and_bounds_history(tmp_path: Path) -> None:
    trace_dir = tmp_path / "traces"
    trace_dir.mkdir()
    first_lines = []
    for index in range(520):
        event = TraceEvent.model_validate(_event("document", "NODE_START"))
        event.task_id = "first"
        event.latency_ms = index
        first_lines.append(event.model_dump_json())
    (trace_dir / "first.jsonl").write_text(
        "\n".join(first_lines) + "\n", encoding="utf-8"
    )
    second = TraceEvent.model_validate(_event("risk", "NODE_START"))
    second.task_id = "second"
    (trace_dir / "second.jsonl").write_text(
        second.model_dump_json() + "\n", encoding="utf-8"
    )

    first_events = read_live_trace_events(trace_dir, "first")
    second_events = read_live_trace_events(trace_dir, "second")

    assert len(first_events) == 512
    assert first_events[0]["latency_ms"] == 8
    assert [event["task_id"] for event in second_events] == ["second"]
