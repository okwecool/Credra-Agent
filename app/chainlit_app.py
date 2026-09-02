"""Chainlit workbench over the shared durable Credra Agent runtime."""

import asyncio
import json
import re
import uuid
from functools import partial
from pathlib import Path
from typing import Any

import chainlit as cl

from app.cases import validate_case
from app.config import Settings, get_settings
from app.mcp.research_client import ResearchServiceError
from app.runtime.tasks import get_task_status, resume_task, start_task
from app.workbench import (
    friendly_error_summary,
    load_workbench_details,
    workbench_detail_markdown,
)

WORKFLOW_NODES = ("document", "financial", "research", "risk", "approval", "report")
_STATUS_LABELS = {
    "CREATED": "⚪ 已创建",
    "RUNNING": "🔵 执行中",
    "WAITING_APPROVAL": "🟠 等待人工审核",
    "COMPLETED": "🟢 已完成",
    "FAILED": "🔴 执行失败",
}
_CASE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")


def discover_cases(data_dir: Path) -> list[dict[str, Any]]:
    """List source-backed Cases with their non-mutating preflight result."""

    if not data_dir.is_dir():
        return []
    cases: list[dict[str, Any]] = []
    for path in sorted(data_dir.iterdir(), key=lambda item: item.name):
        if not path.is_dir() or not _CASE_ID.fullmatch(path.name):
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
    return [
        "## 执行进度",
        " → ".join(
            f"{symbols[states[node]]} `{node}`（{states[node]}）"
            for node in WORKFLOW_NODES
        ),
    ]


def _artifact_markdown(state: dict[str, Any]) -> list[str]:
    artifacts = [
        ("Company", state.get("company_artifact")),
        ("Financial", state.get("financial_artifact")),
        ("Intent", state.get("investigation_intent_artifact")),
        ("Query Plan", state.get("query_plan_artifact")),
        ("Research", state.get("research_artifact")),
        ("Risk", state.get("risk_artifact")),
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


async def _send_payload(payload: dict[str, Any]) -> None:
    cl.user_session.set("credra_thread_id", payload["thread_id"])
    details = await asyncio.to_thread(load_workbench_details, payload, get_settings())
    content = _risk_markdown(payload)
    detail_content = workbench_detail_markdown(details)
    if detail_content:
        content += "\n\n---\n\n" + detail_content
    await cl.Message(
        content=content,
        actions=_task_actions(payload),
        elements=_report_elements(payload, details),
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


def _start_validated_case(case_id: str, settings: Settings) -> dict[str, Any]:
    validation = validate_case(case_id, settings.data_dir)
    if not validation.valid:
        codes = ", ".join(issue.code for issue in validation.errors)
        raise ValueError(f"Case 预检失败：{codes or 'UNKNOWN_VALIDATION_ERROR'}")
    payload = start_task(
        thread_id=f"{case_id}-ui-{uuid.uuid4().hex[:10]}",
        case_id=case_id,
        settings=settings,
    )
    payload["validation"] = validation.model_dump(mode="json")
    return payload


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


@cl.on_chat_start
async def on_chat_start() -> None:
    await _send_case_catalog(get_settings())


@cl.on_message
async def on_message(message: cl.Message) -> None:
    parts = message.content.strip().split(maxsplit=3)
    settings = get_settings()
    try:
        if len(parts) == 1 and parts[0].lower() == "cases":
            await _send_case_catalog(settings)
            return
        if len(parts) == 2 and parts[0].lower() == "start":
            payload = await asyncio.to_thread(_start_validated_case, parts[1], settings)
        elif len(parts) == 2 and parts[0].lower() == "status":
            payload = await asyncio.to_thread(
                partial(get_task_status, thread_id=parts[1], settings=settings)
            )
        elif len(parts) >= 3 and parts[0].lower() == "resume":
            payload = await asyncio.to_thread(
                partial(
                    resume_task,
                    thread_id=parts[1],
                    decision=parts[2],
                    comment=parts[3] if len(parts) == 4 else None,
                    settings=settings,
                )
            )
        else:
            raise ValueError(
                "无法识别输入；可使用页面按钮，或输入 cases、start、status、resume。"
            )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(payload)


@cl.action_callback("start_case")
async def start_case(action: cl.Action) -> None:
    try:
        payload = await asyncio.to_thread(
            _start_validated_case,
            str(action.payload["case_id"]),
            get_settings(),
        )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(payload)


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


@cl.action_callback("approve_task")
async def approve_task(action: cl.Action) -> None:
    comment = await _ask_text("请输入人工审核意见；确认后任务将继续生成报告。")
    if comment is None:
        return
    try:
        payload = await asyncio.to_thread(
            partial(
                resume_task,
                thread_id=str(action.payload["thread_id"]),
                decision="approve",
                comment=comment,
                settings=get_settings(),
            )
        )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(payload)


@cl.action_callback("research_task")
async def research_task(action: cl.Action) -> None:
    comment = await _ask_text(
        "请输入补充调查意图，例如：核查监管处罚、重大诉讼和债务逾期。"
    )
    if comment is None:
        return
    try:
        payload = await asyncio.to_thread(
            partial(
                resume_task,
                thread_id=str(action.payload["thread_id"]),
                decision="research",
                comment=comment,
                settings=get_settings(),
            )
        )
    except (OSError, ResearchServiceError, ValueError) as exc:
        await _send_error(exc)
        return
    await _send_payload(payload)


def serialize_for_debug(payload: dict[str, Any]) -> str:
    """Keep UI payloads easy to inspect without adding another state store."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
