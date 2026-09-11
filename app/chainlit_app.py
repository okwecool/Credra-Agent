"""Chainlit workbench over the shared durable Credra Agent runtime."""

import asyncio
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import chainlit as cl

from app.cases import HIDDEN_DEMO_CASE_IDS, validate_case
from app.config import Settings, get_settings
from app.llm.gateway import StructuredModelError
from app.mcp.research_client import ResearchServiceError
from app.runtime.tasks import get_task_status, resume_task, start_task
from app.workbench import (
    AUDIT_STEP_DISPLAY_LIMIT,
    friendly_error_summary,
    load_workbench_details,
    public_trace_summary,
    workbench_detail_markdown,
)
from app.workbench_charts import (
    build_workbench_figures,
    chart_status_markdown,
    project_workbench_charts,
)
from app.workbench_live import (
    live_progress_markdown,
    project_live_progress,
    read_live_trace_events,
)
from credra_agent.intent.models import IntentResult
from credra_agent.intent.service import build_intent_model, interpret_message

WORKFLOW_NODES = ("document", "financial", "research", "risk", "approval", "report")
AGENTIC_WORKFLOW_NODES = ("decide", "execute")
_NODE_LABELS = {
    "document": "材料解析",
    "financial": "财务分析",
    "research": "补充调查",
    "risk": "风险分析",
    "approval": "人工审核",
    "report": "报告生成",
    "decide": "模型决策",
    "execute": "受控执行",
}
_NODE_DESCRIPTIONS = {
    "document": "读取并校验企业材料",
    "financial": "计算统一口径财务指标",
    "research": "检索、抓取并核验外部证据",
    "risk": "生成确定性风险项与解释",
    "approval": "等待审核意见或继续决策",
    "report": "生成可追溯的最终报告",
    "decide": "依据任务、证据索引和预算选择下一步",
    "execute": "校验并持久化 Action，执行已授权工具",
}
_FLOW_STATE_CODES = {
    "待执行": "pending",
    "执行中": "running",
    "已完成": "done",
    "等待操作": "waiting",
    "本轮跳过": "skipped",
    "无需审核": "skipped",
    "失败": "failed",
}
_TASK_STATE_CODES = {
    "pending": cl.TaskStatus.READY,
    "running": cl.TaskStatus.RUNNING,
    "done": cl.TaskStatus.DONE,
    "waiting": cl.TaskStatus.RUNNING,
    "skipped": cl.TaskStatus.DONE,
    "failed": cl.TaskStatus.FAILED,
}
_TASK_LIST_STATUS = {
    "CREATED": "准备中",
    "RUNNING": "执行中",
    "WAITING_APPROVAL": "等待人工审核",
    "WAITING_CLARIFICATION": "等待用户澄清",
    "LIMITED": "受限结束",
    "PAUSED_LOGGING": "日志恢复后继续",
    "COMPLETED": "已完成",
    "FAILED": "执行失败",
}
_AUDIT_EVENT_VIEW = {
    "NODE_END": ("节点执行", "Workflow", "Workflow"),
    "TOOL_CALL": ("工具调用", "Wrench", "Tool"),
    "RETRY": ("工具重试", "RefreshCw", "Retry"),
    "QUERY_PLAN": ("调查计划", "ListChecks", "Plan"),
    "LLM_CALL": ("受约束模型表达", "Sparkles", "LLM"),
    "LLM_PROCESS_SUMMARY": ("模型过程摘要", "Brain", "LLM Process"),
    "LLM_PROCESS_RETRY": ("模型思考重试", "RefreshCw", "LLM Retry"),
    "LLM_PROCESS_FAILED": ("模型思考降级", "CircleAlert", "LLM Process"),
    "LLM_STRUCTURED_RETRY": ("结构化输出重试", "RefreshCw", "LLM Retry"),
    "INTERRUPT": ("等待人工审核", "PauseCircle", "HITL"),
    "RESUME": ("恢复任务", "PlayCircle", "Resume"),
}
_STATUS_LABELS = {
    "CREATED": "⚪ 已创建",
    "RUNNING": "🔵 执行中",
    "WAITING_APPROVAL": "🟠 等待人工审核",
    "WAITING_CLARIFICATION": "🟠 等待用户澄清",
    "LIMITED": "🟡 受限结束",
    "PAUSED_LOGGING": "🟠 日志暂停",
    "COMPLETED": "🟢 已完成",
    "FAILED": "🔴 执行失败",
}
_CASE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_BACKGROUND_OPERATIONS: set[asyncio.Task[dict[str, Any]]] = set()


def discover_cases(
    data_dir: Path, *, include_hidden: bool = False
) -> list[dict[str, Any]]:
    """List visible Cases; keep synthetic fixtures available for explicit audits."""

    if not data_dir.is_dir():
        return []
    cases: list[dict[str, Any]] = []
    for path in sorted(data_dir.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or not _CASE_ID.fullmatch(path.name):
            continue
        if not include_hidden and path.name in HIDDEN_DEMO_CASE_IDS:
            continue
        if not (path / "source").is_dir():
            continue
        validation = validate_case(path.name, data_dir)
        cases.append(
            {
                "case_id": path.name,
                "valid": validation.valid,
                "status": validation.status,
                "warnings": len(validation.warnings),
                "confirmations": len(validation.confirmations),
                "errors": len(validation.errors),
            }
        )
    return cases


def _case_catalog_markdown(cases: list[dict[str, Any]]) -> str:
    lines = [
        "# Credra Agent 工作台",
        "",
        "选择一个通过预检的 Case 启动任务，或恢复已有 Thread。",
        "",
        "| Case | 预检状态 | 警告 | 待确认 |",
        "|---|---:|---:|---:|",
    ]
    for item in cases:
        status = "✅ " + item["status"] if item["valid"] else "❌ INVALID"
        lines.append(
            f"| `{item['case_id']}` | {status} | {item['warnings']} | "
            f"{item['confirmations']} |"
        )
    if not cases:
        lines.append("| _未发现 Case_ | — | — | — |")
    lines.extend(
        (
            "",
            "> UI 与 `app.task_cli` 共用 SQLite Checkpoint、Runtime 和 Artifact。",
        )
    )
    return "\n".join(lines)


def _validation_markdown(validation: dict[str, Any] | None) -> list[str]:
    if not validation:
        return []
    lines = [
        "## 输入预检",
        f"- 状态：`{validation['status']}`",
        f"- 归一化单位：`{validation.get('normalized_currency') or '未提供'}`",
    ]
    for heading, key in (
        ("错误", "errors"),
        ("警告", "warnings"),
        ("人工确认项", "confirmations"),
    ):
        issues = validation.get(key, [])
        if issues:
            lines.append(f"- {heading}：")
            lines.extend(
                f"  - `{issue['code']}` {issue['message']}" for issue in issues
            )
    return lines


def _node_states(payload: dict[str, Any]) -> dict[str, str]:
    state = payload["state"]
    if state.get("graph_version") == "agentic_v2":
        return _agentic_node_states(payload)
    node_states = {node: "待执行" for node in WORKFLOW_NODES}
    references = {
        "document": state.get("company_artifact"),
        "financial": state.get("financial_artifact"),
        "research": state.get("research_artifact"),
        "risk": state.get("risk_artifact"),
        "report": state.get("report_artifact"),
    }
    for node, reference in references.items():
        if reference:
            node_states[node] = "已完成"
    if state.get("human_decision"):
        node_states["approval"] = "已完成"
    if payload.get("interrupts"):
        node_states["approval"] = "等待操作"
    current = state.get("current_node")
    if state.get("status") == "RUNNING" and current in node_states:
        node_states[current] = "执行中"
    if state.get("status") == "FAILED" and state.get("failed_node") in node_states:
        node_states[state["failed_node"]] = "失败"
    if current in {"risk", "report"} and not state.get("research_artifact"):
        node_states["research"] = "本轮跳过"
    if state.get("status") == "COMPLETED" and not state.get("human_decision"):
        node_states["approval"] = "无需审核"
    return node_states


def _agentic_node_states(payload: dict[str, Any]) -> dict[str, str]:
    state = payload["state"]
    status = state.get("status")
    current = state.get("current_node")
    node_states = {node: "待执行" for node in AGENTIC_WORKFLOW_NODES}
    if state.get("iteration", 0) > 0 or current in AGENTIC_WORKFLOW_NODES:
        node_states["decide"] = "已完成"
    if state.get("observation_index_ref") and state.get("coverage_ref"):
        node_states["execute"] = "已完成"
    if status in {"COMPLETED", "LIMITED"}:
        return {node: "已完成" for node in AGENTIC_WORKFLOW_NODES}
    if status in {"WAITING_CLARIFICATION", "PAUSED_LOGGING"}:
        node_states[current if current in node_states else "execute"] = "等待操作"
        return node_states
    if status == "FAILED":
        node_states[current if current in node_states else "decide"] = "失败"
        return node_states
    if status == "RUNNING":
        next_node = next(
            (node for node in payload.get("next", []) if node in node_states), None
        )
        node_states[next_node or current or "decide"] = "执行中"
    return node_states


def _workflow_nodes(payload: dict[str, Any]) -> tuple[str, ...]:
    if payload["state"].get("graph_version") == "agentic_v2":
        return AGENTIC_WORKFLOW_NODES
    return WORKFLOW_NODES


def _timeline_markdown(payload: dict[str, Any]) -> list[str]:
    symbols = {
        "已完成": "✅",
        "等待操作": "🟠",
        "执行中": "🔵",
        "失败": "❌",
        "本轮跳过": "➖",
        "无需审核": "➖",
        "待执行": "⚪",
    }
    states = _node_states(payload)
    workflow_nodes = _workflow_nodes(payload)
    return [
        "## 执行进度",
        " → ".join(
            f"{symbols[states[node]]} `{node}`（{states[node]}）"
            for node in workflow_nodes
        ),
    ]


def _flow_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Build the bounded state projection consumed by the AgentFlow element."""

    state = payload["state"]
    node_states = _node_states(payload)
    workflow_nodes = _workflow_nodes(payload)
    nodes = [
        {
            "id": node,
            "label": _NODE_LABELS[node],
            "description": _NODE_DESCRIPTIONS[node],
            "status": _FLOW_STATE_CODES[node_states[node]],
            "statusLabel": node_states[node],
        }
        for node in workflow_nodes
    ]
    processed = sum(node["status"] in {"done", "skipped"} for node in nodes)
    workflow_status = str(state.get("status") or "UNKNOWN")
    current_node = state.get("failed_node") or state.get("current_node")
    return {
        "schemaVersion": "1.0",
        "caseId": str(state.get("case_id") or "—"),
        "threadId": str(payload.get("thread_id") or "—"),
        "runId": str(state.get("run_id") or "legacy"),
        "workflowStatus": workflow_status,
        "workflowStatusLabel": _STATUS_LABELS.get(workflow_status, workflow_status),
        "currentNode": current_node if current_node in workflow_nodes else None,
        "nextNodes": [
            node for node in payload.get("next", []) if node in workflow_nodes
        ],
        "riskLevel": str(state.get("risk_level") or "—"),
        "progress": {
            "processed": processed,
            "total": len(nodes),
            "percent": round(processed / len(nodes) * 100),
        },
        "nodes": nodes,
    }


def _flow_element(payload: dict[str, Any]) -> cl.CustomElement:
    """Create a snapshot element without exposing raw artifacts or trace payloads."""

    return cl.CustomElement(
        thread_id=str(payload.get("thread_id") or "workbench"),
        name="AgentFlow",
        display="inline",
        size="large",
        props=_flow_view(payload),
    )


def _task_list(payload: dict[str, Any]) -> cl.TaskList:
    """Create the compact side-panel projection for the current workflow snapshot."""

    flow = _flow_view(payload)
    tasks = [
        cl.Task(
            title=f"{node['label']} · {node['statusLabel']}",
            status=_TASK_STATE_CODES[node["status"]],
        )
        for node in flow["nodes"]
    ]
    return cl.TaskList(
        thread_id=str(payload.get("thread_id") or "workbench"),
        status=_TASK_LIST_STATUS.get(flow["workflowStatus"], "状态未知"),
        tasks=tasks,
    )


def _audit_step_views(details: dict[str, Any]) -> list[dict[str, Any]]:
    """Project meaningful Trace events into bounded, public operational steps."""

    events = details.get("trace_events") or []
    views: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        event_type = str(event.get("event_type") or "")
        if event_type not in _AUDIT_EVENT_VIEW:
            continue
        if event_type == "INTERRUPT" and _is_replayed_interrupt(events, index):
            continue
        label, icon, kind = _AUDIT_EVENT_VIEW[event_type]
        node = str(event.get("node") or "runtime")
        status = str(event.get("status") or "UNKNOWN")
        latency_ms = max(0, int(event.get("latency_ms") or 0))
        end_time = str(event.get("end_time") or "")
        key = f"{node}|{event_type}|{status}|{end_time}"
        views.append(
            {
                "key": key,
                "name": f"{_NODE_LABELS.get(node, node)} · {label}",
                "icon": icon,
                "kind": kind,
                "status": status,
                "latencyMs": latency_ms,
                "summary": public_trace_summary(event),
                "defaultOpen": status in {"FAILED", "RETRY"}
                or event_type == "INTERRUPT",
                "metadata": {
                    "event_type": event_type,
                    "node": node,
                    "status": status,
                    "latency_ms": latency_ms,
                },
            }
        )
    return views[-AUDIT_STEP_DISPLAY_LIMIT:]


def _is_replayed_interrupt(events: list[dict[str, Any]], index: int) -> bool:
    """Hide resume-time interrupt replay that immediately resolves in the same node."""

    node = str(events[index].get("node") or "")
    for later in events[index + 1 :]:
        later_type = str(later.get("event_type") or "")
        if later_type == "NODE_END" and str(later.get("node") or "") == node:
            return True
        if later_type in {"TASK_STATE", "RESUME"}:
            return False
    return False


async def _sync_task_list(
    payload: dict[str, Any], task_list: cl.TaskList | None = None
) -> cl.TaskList:
    snapshot = _task_list(payload)
    if task_list is None:
        await snapshot.send()
        return snapshot
    task_list.status = snapshot.status
    task_list.tasks = snapshot.tasks
    await task_list.update()
    return task_list


def _live_task_list(projection: dict[str, Any]) -> cl.TaskList:
    tasks = [
        cl.Task(
            title=f"{node['label']} · {node['statusLabel']}",
            status=_TASK_STATE_CODES[node["status"]],
        )
        for node in projection["nodes"]
    ]
    return cl.TaskList(
        thread_id=str(projection["threadId"]),
        status=str(projection["workflowStatusLabel"]),
        tasks=tasks,
    )


async def _update_live_task_list(
    task_list: cl.TaskList, projection: dict[str, Any]
) -> None:
    snapshot = _live_task_list(projection)
    task_list.status = snapshot.status
    task_list.tasks = snapshot.tasks
    await task_list.update()


async def _send_new_audit_steps(
    payload: dict[str, Any], details: dict[str, Any]
) -> None:
    thread_id = str(payload.get("thread_id") or "")
    session_thread = cl.user_session.get("credra_audit_step_thread")
    if session_thread != thread_id:
        seen: set[str] = set()
    else:
        seen = set(cl.user_session.get("credra_audit_step_keys") or [])
    for view in _audit_step_views(details):
        if view["key"] in seen:
            continue
        step = cl.Step(
            name=view["name"],
            type="tool",
            icon=view["icon"],
            default_open=view["defaultOpen"],
            show_input=False,
            metadata=view["metadata"],
        )
        step.output = (
            f"类型：{view['kind']}\n\n"
            f"状态：{view['status']}\n\n"
            f"耗时：{view['latencyMs']} ms\n\n"
            f"摘要：{view['summary']}"
        )
        await step.send()
        seen.add(view["key"])
    cl.user_session.set("credra_audit_step_thread", thread_id)
    cl.user_session.set("credra_audit_step_keys", sorted(seen))


def _artifact_markdown(state: dict[str, Any]) -> list[str]:
    if state.get("graph_version") == "agentic_v2":
        artifacts = [
            ("TaskSpec", state.get("task_spec_ref")),
            ("Run Authorization", state.get("authorization_ref")),
            ("Hypotheses", state.get("hypotheses_ref")),
            ("Observation Index", state.get("observation_index_ref")),
            ("Coverage", state.get("coverage_ref")),
            ("Budget Ledger", state.get("budget_ledger_ref")),
            ("Active Decision", state.get("active_decision_ref")),
            ("Active Action", state.get("active_action_ref")),
        ]
    else:
        artifacts = [
            ("Company", state.get("company_artifact")),
            ("Financial", state.get("financial_artifact")),
            ("Intent", state.get("investigation_intent_artifact")),
            ("Query Plan", state.get("query_plan_artifact")),
            ("Research", state.get("research_artifact")),
            ("Evidence Summary", state.get("evidence_summary_artifact")),
            ("Query Proposal", state.get("query_proposal_artifact")),
            ("Risk", state.get("risk_artifact")),
            ("LLM Narrative", state.get("risk_narrative_artifact")),
            ("Report Draft", state.get("report_draft_artifact")),
            ("Report Expression", state.get("report_expression_artifact")),
            ("Report", state.get("report_artifact")),
        ]
    lines = ["## Artifact 引用", "| 类型 | 当前版本 |", "|---|---|"]
    lines.extend(f"| {name} | `{reference or '—'}` |" for name, reference in artifacts)
    return lines


def _risk_markdown(payload: dict[str, Any]) -> str:
    """Render one self-contained task dashboard for Chainlit and tests."""

    state = payload["state"]
    next_nodes = ", ".join(payload.get("next", [])) or "无"
    status = state.get("status")
    lines = [
        "# 任务状态总览",
        "",
        "| Case | Thread ID | Run ID | 状态 | 当前节点 | 下一节点 | 风险等级 |",
        "|---|---|---|---|---|---|---|",
        (
            f"| `{state.get('case_id') or '—'}` | `{payload['thread_id']}` | "
            f"`{state.get('run_id') or 'legacy'}` | "
            f"{_STATUS_LABELS.get(status, status or 'UNKNOWN')} `{status or 'UNKNOWN'}` | "
            f"`{state.get('current_node') or '—'}` | `{next_nodes}` | "
            f"`{state.get('risk_level') or '—'}` |"
        ),
        "",
        *_validation_markdown(payload.get("validation")),
        "",
        *_timeline_markdown(payload),
    ]
    if payload.get("interrupts"):
        interrupt_value = payload["interrupts"][0]["value"]
        lines.extend(("", "## 待审核风险项"))
        for flag in interrupt_value.get("risk_flags", []):
            evidence = ", ".join(flag.get("evidence", []))
            lines.append(
                f"- **{flag['severity']} · {flag.get('type', 'risk')}**："
                f"{flag['description']}  \n  证据：`{evidence}`"
            )
    if status == "FAILED":
        lines.extend(
            (
                "",
                "## 失败说明",
                f"- 失败节点：`{state.get('failed_node') or 'unknown'}`",
                f"- 安全摘要：`{state.get('error_summary') or '未提供'}`",
            )
        )
    lines.extend(("", *_artifact_markdown(state)))
    return "\n".join(lines)


def _task_actions(payload: dict[str, Any]) -> list[cl.Action]:
    thread_id = payload["thread_id"]
    actions = [
        cl.Action(
            name="refresh_task",
            label="刷新状态",
            payload={"thread_id": thread_id},
        )
    ]
    if payload.get("execution_blocked"):
        actions.insert(
            0,
            cl.Action(
                name="resume_logging",
                label="日志恢复后继续",
                payload={"thread_id": thread_id},
            ),
        )
        return actions
    if payload.get("interrupts"):
        actions[:0] = [
            cl.Action(
                name="approve_task",
                label="批准并继续",
                payload={"thread_id": thread_id},
            ),
            cl.Action(
                name="research_task",
                label="补充调查",
                payload={"thread_id": thread_id},
            ),
        ]
    return actions


def _report_elements(payload: dict[str, Any], details: dict[str, Any]) -> list[cl.File]:
    markdown = details.get("report_markdown")
    html = details.get("report_html")
    if not isinstance(markdown, str) or not isinstance(html, str):
        return []
    case_id = str(payload.get("state", {}).get("case_id") or "credra")
    if not _CASE_ID.fullmatch(case_id):
        case_id = "credra"
    thread_id = str(payload.get("thread_id") or "workbench")
    return [
        cl.File(
            thread_id=thread_id,
            name=f"{case_id}_credit_report.md",
            content=markdown.encode("utf-8"),
            display="inline",
            mime="text/markdown",
        ),
        cl.File(
            thread_id=thread_id,
            name=f"{case_id}_credit_report.html",
            content=html.encode("utf-8"),
            display="inline",
            mime="text/html",
        ),
    ]


def _chart_elements(
    payload: dict[str, Any], projection: dict[str, Any]
) -> list[cl.Plotly]:
    thread_id = str(payload.get("thread_id") or "workbench")
    return [
        cl.Plotly(
            thread_id=thread_id,
            name=name,
            display="inline",
            size="large",
            figure=figure,
        )
        for name, figure in build_workbench_figures(projection)
    ]


def _live_trace_signature(events: list[dict[str, Any]]) -> tuple[Any, ...]:
    if not events:
        return (0,)
    last = events[-1]
    return (
        len(events),
        last.get("node"),
        last.get("event_type"),
        last.get("status"),
        last.get("end_time"),
    )


def _track_background_operation(
    task: asyncio.Task[dict[str, Any]],
) -> asyncio.Task[dict[str, Any]]:
    """Keep Runtime work alive if a browser handler is cancelled on disconnect."""

    _BACKGROUND_OPERATIONS.add(task)

    def release(completed: asyncio.Task[dict[str, Any]]) -> None:
        _BACKGROUND_OPERATIONS.discard(completed)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(release)
    return task


async def _run_with_live_updates(
    operation: Callable[[], dict[str, Any]],
    *,
    thread_id: str,
    case_id: str | None,
    settings: Settings,
) -> tuple[dict[str, Any], cl.TaskList, cl.Message]:
    """Run the unchanged synchronous Runtime while projecting its append-only Trace."""

    events = await asyncio.to_thread(
        read_live_trace_events, settings.trace_dir, thread_id
    )
    projection = project_live_progress(events, thread_id=thread_id, case_id=case_id)
    task_list = _live_task_list(projection)
    await task_list.send()
    progress_message = cl.Message(content=live_progress_markdown(projection))
    await progress_message.send()
    signature = _live_trace_signature(events)
    operation_task = _track_background_operation(
        asyncio.create_task(asyncio.to_thread(operation))
    )

    while not operation_task.done():
        await asyncio.sleep(0.35)
        events = await asyncio.to_thread(
            read_live_trace_events, settings.trace_dir, thread_id
        )
        current_signature = _live_trace_signature(events)
        if current_signature == signature:
            continue
        signature = current_signature
        projection = project_live_progress(events, thread_id=thread_id, case_id=case_id)
        await _update_live_task_list(task_list, projection)
        progress_message.content = live_progress_markdown(projection)
        await progress_message.update()
        await _send_new_audit_steps({"thread_id": thread_id}, {"trace_events": events})

    try:
        payload = await asyncio.shield(operation_task)
    except (OSError, ResearchServiceError, ValueError):
        task_list.status = "操作失败"
        await task_list.update()
        progress_message.content = (
            "## 实时执行进度\n\n"
            "⚠️ Runtime 操作返回错误；受限 Trace 临时投影未被当作最终状态。\n\n"
            "> 请查看下方安全错误摘要，并可通过 Thread ID 重新查询 Checkpoint。"
        )
        await progress_message.update()
        raise
    events = await asyncio.to_thread(
        read_live_trace_events, settings.trace_dir, thread_id
    )
    projection = project_live_progress(events, thread_id=thread_id, case_id=case_id)
    if _live_trace_signature(events) != signature:
        await _update_live_task_list(task_list, projection)
        progress_message.content = live_progress_markdown(projection)
        await progress_message.update()
        await _send_new_audit_steps({"thread_id": thread_id}, {"trace_events": events})
    return payload, task_list, progress_message


async def _send_payload(
    payload: dict[str, Any],
    *,
    live_task_list: cl.TaskList | None = None,
    live_message: cl.Message | None = None,
) -> None:
    cl.user_session.set("credra_thread_id", payload["thread_id"])
    details = await asyncio.to_thread(load_workbench_details, payload, get_settings())
    await _sync_task_list(payload, live_task_list)
    await _send_new_audit_steps(payload, details)
    if live_message is not None:
        status = str(payload.get("state", {}).get("status") or "UNKNOWN")
        live_message.content = (
            "## 实时执行进度\n\n"
            f"✅ 已按 SQLite Checkpoint 收敛，最终状态：`{status}`。\n\n"
            "> 下方任务面板、流程图和 Artifact 为最终可信快照。"
        )
        await live_message.update()
    content = _risk_markdown(payload)
    if payload.get("execution_blocked"):
        content = (
            "日志不可用，任务已在下一节点执行前暂停。恢复日志服务（必要时重启应用）后，点击“日志恢复后继续”。已保存的结果会保留。\n\n"
            + content
        )
    detail_content = workbench_detail_markdown(details)
    if detail_content:
        content += "\n\n---\n\n" + detail_content
    chart_projection = project_workbench_charts(details)
    try:
        chart_elements = _chart_elements(payload, chart_projection)
    except (KeyError, TypeError, ValueError):
        chart_elements = []
        chart_projection["notices"].append(
            "图表渲染：当前 Artifact 无法安全生成图表，其他任务状态不受影响。"
        )
    content += "\n\n---\n\n" + chart_status_markdown(chart_projection)
    await cl.Message(
        content=content,
        actions=_task_actions(payload),
        elements=[
            _flow_element(payload),
            *chart_elements,
            *_report_elements(payload, details),
        ],
    ).send()


async def _send_case_catalog(settings: Settings) -> None:
    cases = await asyncio.to_thread(discover_cases, settings.data_dir)
    actions = [
        cl.Action(
            name="start_case",
            label=f"启动 {item['case_id']}",
            payload={"case_id": item["case_id"]},
        )
        for item in cases
        if item["valid"]
    ]
    actions.append(cl.Action(name="restore_task", label="恢复已有任务", payload={}))
    await cl.Message(content=_case_catalog_markdown(cases), actions=actions).send()


def _start_validated_case(
    case_id: str, settings: Settings, thread_id: str | None = None
) -> dict[str, Any]:
    validation = validate_case(case_id, settings.data_dir)
    if not validation.valid:
        codes = ", ".join(issue.code for issue in validation.errors)
        raise ValueError(f"Case 预检失败：{codes or 'UNKNOWN_VALIDATION_ERROR'}")
    payload = start_task(
        thread_id=thread_id or f"{case_id}-ui-{uuid.uuid4().hex[:10]}",
        case_id=case_id,
        settings=settings,
    )
    payload["validation"] = validation.model_dump(mode="json")
    return payload


async def _start_case_with_live_updates(
    case_id: str, settings: Settings
) -> tuple[dict[str, Any], cl.TaskList, cl.Message]:
    thread_id = f"{case_id}-ui-{uuid.uuid4().hex[:10]}"
    return await _run_with_live_updates(
        partial(_start_validated_case, case_id, settings, thread_id),
        thread_id=thread_id,
        case_id=case_id,
        settings=settings,
    )


async def _resume_with_live_updates(
    *,
    thread_id: str,
    decision: str,
    comment: str | None,
    settings: Settings,
) -> tuple[dict[str, Any], cl.TaskList, cl.Message]:
    snapshot = await asyncio.to_thread(
        partial(get_task_status, thread_id=thread_id, settings=settings)
    )
    case_id = snapshot.get("state", {}).get("case_id")
    return await _run_with_live_updates(
        partial(
            resume_task,
            thread_id=thread_id,
            decision=decision,
            comment=comment,
            settings=settings,
        ),
        thread_id=thread_id,
        case_id=str(case_id) if case_id else None,
        settings=settings,
    )


async def _ask_text(prompt: str) -> str | None:
    answer = await cl.AskUserMessage(content=prompt, timeout=180).send()
    if not answer:
        await cl.Message(content="操作已取消：等待输入超时。").send()
        return None
    value = str(answer.get("output", "")).strip()
    if not value:
        await cl.Message(content="操作已取消：输入不能为空。").send()
        return None
    return value


def _safe_error(exc: Exception) -> str:
    message = friendly_error_summary(exc)
    return f"{type(exc).__name__}: {message}"


async def _send_error(exc: Exception) -> None:
    await cl.Message(content=f"## 操作失败\n\n`{_safe_error(exc)}`").send()


def _intent_markdown(result: IntentResult) -> str:
    lines = [
        "## 自然语言任务已解析",
        "",
        f"- 操作：`{result.operation}`",
        f"- 解析方式：`{result.parser_mode}`",
        f"- 消息去重：`{'是' if result.duplicate else '否'}`",
    ]
    spec = result.task_spec
    if spec is not None:
        periods = (
            "、".join(
                f"{item.start.isoformat()} 至 {item.end.isoformat()}"
                for item in spec.periods
            )
            or "待澄清"
        )
        focuses = "、".join(question.focus for question in spec.questions)
        lines.extend(
            [
                f"- 主体：`{spec.subject_name or '待澄清'}`（`{spec.subject_id or '—'}`）",
                f"- 截止日：`{spec.as_of.isoformat()}`",
                f"- 期间：{periods}",
                f"- 调查重点：{focuses}",
                f"- TaskSpec：`v{spec.version}` / `{spec.readiness}`",
            ]
        )
        if spec.unresolved_fields:
            lines.append("- 待澄清：" + "、".join(spec.unresolved_fields))
    else:
        lines.append(f"- 绑定 TaskSpec：`v{result.bound_task_spec_version}`")
    if result.rejected_instructions:
        lines.append("- 已拒绝指令：" + "、".join(result.rejected_instructions))
    if result.warnings:
        lines.append("- 限制：" + "、".join(result.warnings))
    lines.extend(
        [
            "",
            "> 解析结果已持久化。可通过受控 Agent Runtime 或 `app.task_cli agent` 执行；当前界面不会自行创建运行授权或发起付费调用。",
        ]
    )
    return "\n".join(lines)


async def _interpret_natural_language(message: cl.Message, settings: Settings) -> None:
    thread_id = cl.user_session.get("intent_thread_id")
    if not thread_id:
        thread_id = f"agentic-ui-{uuid.uuid4().hex[:16]}"
        cl.user_session.set("intent_thread_id", thread_id)
    source_message_id = str(getattr(message, "id", None) or uuid.uuid4().hex)
    result = await asyncio.to_thread(
        partial(
            interpret_message,
            thread_id=str(thread_id),
            source_message_id=source_message_id,
            text=message.content,
            as_of=datetime.now(UTC).date(),
            data_dir=settings.data_dir,
            database_path=settings.checkpoint_db_path,
            model=build_intent_model(settings),
        )
    )
    await cl.Message(content=_intent_markdown(result)).send()


@cl.on_chat_start
async def on_chat_start() -> None:
    await _send_case_catalog(get_settings())


@cl.on_message
async def on_message(message: cl.Message) -> None:
    parts = message.content.strip().split(maxsplit=3)
    settings = get_settings()
    live_task_list: cl.TaskList | None = None
    live_message: cl.Message | None = None
    try:
        if len(parts) == 1 and parts[0].lower() == "cases":
            await _send_case_catalog(settings)
            return
        if len(parts) == 2 and parts[0].lower() == "start":
            payload, live_task_list, live_message = await _start_case_with_live_updates(
                parts[1], settings
            )
        elif len(parts) == 2 and parts[0].lower() == "status":
            payload = await asyncio.to_thread(
                partial(get_task_status, thread_id=parts[1], settings=settings)
            )
        elif len(parts) >= 3 and parts[0].lower() == "resume":
            payload, live_task_list, live_message = await _resume_with_live_updates(
                thread_id=parts[1],
                decision=parts[2],
                comment=parts[3] if len(parts) == 4 else None,
                settings=settings,
            )
        else:
            await _interpret_natural_language(message, settings)
            return
    except (OSError, ResearchServiceError, StructuredModelError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(
        payload, live_task_list=live_task_list, live_message=live_message
    )


@cl.action_callback("start_case")
async def start_case(action: cl.Action) -> None:
    try:
        payload, live_task_list, live_message = await _start_case_with_live_updates(
            str(action.payload["case_id"]), get_settings()
        )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(
        payload, live_task_list=live_task_list, live_message=live_message
    )


@cl.action_callback("restore_task")
async def restore_task(_: cl.Action) -> None:
    thread_id = await _ask_text("请输入要恢复的 Thread ID。")
    if thread_id is None:
        return
    try:
        payload = await asyncio.to_thread(
            partial(get_task_status, thread_id=thread_id, settings=get_settings())
        )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(payload)


@cl.action_callback("refresh_task")
async def refresh_task(action: cl.Action) -> None:
    try:
        payload = await asyncio.to_thread(
            partial(
                get_task_status,
                thread_id=str(action.payload["thread_id"]),
                settings=get_settings(),
            )
        )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(payload)


@cl.action_callback("resume_logging")
async def resume_logging(action: cl.Action) -> None:
    try:
        payload, live_task_list, live_message = await _resume_with_live_updates(
            thread_id=str(action.payload["thread_id"]),
            decision="resume_logging",
            comment=None,
            settings=get_settings(),
        )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(
        payload, live_task_list=live_task_list, live_message=live_message
    )


@cl.action_callback("approve_task")
async def approve_task(action: cl.Action) -> None:
    comment = await _ask_text("请输入人工审核意见；确认后任务将继续生成报告。")
    if comment is None:
        return
    try:
        payload, live_task_list, live_message = await _resume_with_live_updates(
            thread_id=str(action.payload["thread_id"]),
            decision="approve",
            comment=comment,
            settings=get_settings(),
        )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(
        payload, live_task_list=live_task_list, live_message=live_message
    )


@cl.action_callback("research_task")
async def research_task(action: cl.Action) -> None:
    comment = await _ask_text(
        "请输入补充调查意图，例如：核查监管处罚、重大诉讼和债务逾期。"
    )
    if comment is None:
        return
    try:
        payload, live_task_list, live_message = await _resume_with_live_updates(
            thread_id=str(action.payload["thread_id"]),
            decision="research",
            comment=comment,
            settings=get_settings(),
        )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(
        payload, live_task_list=live_task_list, live_message=live_message
    )


def serialize_for_debug(payload: dict[str, Any]) -> str:
    """Keep UI payloads easy to inspect without adding another state store."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
