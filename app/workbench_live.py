"""Safe, read-only live progress projection for the Chainlit workbench."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.models.trace import TraceEvent

WORKFLOW_NODES = ("document", "financial", "research", "risk", "approval", "report")
LIVE_TRACE_MAX_BYTES = 512 * 1024
LIVE_TRACE_MAX_EVENTS = 512

_NODE_LABELS = {
    "document": "材料解析",
    "financial": "财务分析",
    "research": "补充调查",
    "risk": "风险分析",
    "approval": "人工审核",
    "report": "报告生成",
}
_STATUS_LABELS = {
    "RUNNING": "实时执行中",
    "WAITING_APPROVAL": "等待人工审核",
    "COMPLETED": "已完成",
    "FAILED": "执行失败",
}
_NODE_STATUS_LABELS = {
    "pending": "待执行",
    "running": "执行中",
    "done": "已完成",
    "waiting": "等待操作",
    "skipped": "本轮跳过",
    "failed": "失败",
}
_PUBLIC_EVENT_TYPES = {
    "NODE_START",
    "NODE_END",
    "TOOL_CALL",
    "RETRY",
    "QUERY_PLAN",
    "LLM_CALL",
    "LLM_SKIP",
    "LLM_PROCESS_START",
    "LLM_PROCESS_FIRST_TOKEN",
    "LLM_PROCESS_PROGRESS",
    "LLM_PROCESS_SUMMARY",
    "LLM_PROCESS_RETRY",
    "LLM_PROCESS_FAILED",
    "LLM_STRUCTURED_START",
    "LLM_STRUCTURED_FIRST_TOKEN",
    "LLM_STRUCTURED_RETRY",
    "INTERRUPT",
    "RESUME",
}

_EVENT_LABELS = {
    "LLM_PROCESS_START": "模型开始思考",
    "LLM_PROCESS_FIRST_TOKEN": "已收到思考流首 Token",
    "LLM_PROCESS_PROGRESS": "模型持续思考中",
    "LLM_PROCESS_SUMMARY": "公开过程摘要已生成",
    "LLM_PROCESS_RETRY": "思考流正在重试",
    "LLM_PROCESS_FAILED": "思考流已降级",
    "LLM_STRUCTURED_START": "正在生成结构化 JSON",
    "LLM_STRUCTURED_FIRST_TOKEN": "已收到结构化输出首 Token",
    "LLM_STRUCTURED_RETRY": "结构化输出正在重试",
}


def safe_live_trace_path(trace_dir: Path, thread_id: str) -> Path | None:
    """Resolve one trace path without allowing a Thread ID to escape its root."""

    if not thread_id:
        return None
    root = trace_dir.resolve()
    path = (root / f"{thread_id}.jsonl").resolve()
    return path if path.parent == root else None


def read_live_trace_events(trace_dir: Path, thread_id: str) -> list[dict[str, Any]]:
    """Read complete, valid events while tolerating a concurrently appended last line."""

    path = safe_live_trace_path(trace_dir, thread_id)
    if path is None or not path.is_file():
        return []
    try:
        size = path.stat().st_size
        with path.open("rb") as file:
            if size > LIVE_TRACE_MAX_BYTES:
                file.seek(-LIVE_TRACE_MAX_BYTES, 2)
                file.readline()
            content = file.read().decode("utf-8", errors="ignore")
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    lines = content.splitlines(keepends=True)
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if index == len(lines) - 1 and not line.endswith(("\n", "\r")):
            continue
        try:
            event = TraceEvent.model_validate(json.loads(line))
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        events.append(event.model_dump(mode="json"))
    return events[-LIVE_TRACE_MAX_EVENTS:]


def _skip_optional_predecessors(node: str, statuses: dict[str, str]) -> None:
    if node in {"risk", "approval", "report"} and statuses["research"] == "pending":
        statuses["research"] = "skipped"
    if node == "report" and statuses["approval"] == "pending":
        statuses["approval"] = "skipped"


def project_live_progress(
    events: list[dict[str, Any]], *, thread_id: str, case_id: str | None
) -> dict[str, Any]:
    """Project append-only Trace events into bounded transient workflow state."""

    statuses = {node: "pending" for node in WORKFLOW_NODES}
    current_node: str | None = None
    last_event: dict[str, Any] | None = None

    for event in events:
        node = str(event.get("node") or "")
        raw_event_type = str(event.get("event_type") or "")
        event_type = (
            raw_event_type if raw_event_type in _PUBLIC_EVENT_TYPES else "OTHER"
        )
        event_status = str(event.get("status") or "UNKNOWN")
        if event_type == "RESUME" and statuses["approval"] == "waiting":
            statuses["approval"] = "running"
            current_node = "approval"
        if node not in statuses:
            continue
        _skip_optional_predecessors(node, statuses)
        if event_type == "NODE_START":
            statuses[node] = "running"
            current_node = node
        elif event_type == "NODE_END":
            statuses[node] = "failed" if event_status == "FAILED" else "done"
            current_node = node if event_status == "FAILED" else None
        elif event_type == "INTERRUPT" and node == "approval":
            statuses[node] = "waiting"
            current_node = node
        last_event = {
            "node": node,
            "eventType": event_type,
            "status": event_status,
            "latencyMs": max(0, int(event.get("latency_ms") or 0)),
        }

    if "failed" in statuses.values():
        workflow_status = "FAILED"
    elif statuses["approval"] == "waiting":
        workflow_status = "WAITING_APPROVAL"
    elif statuses["report"] == "done":
        workflow_status = "COMPLETED"
    else:
        workflow_status = "RUNNING"
    processed = sum(status in {"done", "skipped"} for status in statuses.values())
    return {
        "schemaVersion": "1.0",
        "threadId": thread_id,
        "caseId": case_id or "—",
        "workflowStatus": workflow_status,
        "workflowStatusLabel": _STATUS_LABELS[workflow_status],
        "currentNode": current_node,
        "eventCount": len(events),
        "lastEvent": last_event,
        "progress": {
            "processed": processed,
            "total": len(WORKFLOW_NODES),
            "percent": round(processed / len(WORKFLOW_NODES) * 100),
        },
        "nodes": [
            {
                "id": node,
                "label": _NODE_LABELS[node],
                "status": statuses[node],
                "statusLabel": _NODE_STATUS_LABELS[statuses[node]],
            }
            for node in WORKFLOW_NODES
        ],
    }


def live_progress_markdown(projection: dict[str, Any]) -> str:
    """Render only bounded operational metadata, never raw Trace summaries."""

    progress = projection["progress"]
    current_node = projection.get("currentNode")
    current_label = _NODE_LABELS.get(str(current_node), "等待下一事件")
    last_event = projection.get("lastEvent") or {}
    last_type = str(last_event.get("eventType") or "等待 Trace")
    last_label = _EVENT_LABELS.get(last_type, last_type)
    return (
        "## 实时执行进度\n\n"
        f"- 状态：`{projection['workflowStatusLabel']}`\n"
        f"- 当前节点：`{current_label}`\n"
        f"- 已处理：`{progress['processed']} / {progress['total']}`"
        f"（{progress['percent']}%）\n"
        f"- 已观察事件：`{projection['eventCount']}` · 最新阶段：`{last_label}`\n\n"
        "> 此处为当前 Thread 的受限 Trace 临时投影；操作完成后以 SQLite "
        "Checkpoint 状态收敛。"
    )
