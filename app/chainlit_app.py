"""Minimal Chainlit UI over the durable task runtime."""

import json
import uuid
from typing import Any

import chainlit as cl

from app.config import get_settings
from app.mcp.research_client import ResearchServiceError
from app.runtime.tasks import get_task_status, resume_task, start_task


def _risk_markdown(payload: dict[str, Any]) -> str:
    state = payload["state"]
    lines = [
        f"**Task ID:** `{payload['thread_id']}`",
        f"**Status:** `{state.get('status')}`",
        f"**Current node:** `{state.get('current_node')}`",
        f"**Risk level:** `{state.get('risk_level')}`",
    ]
    if payload["interrupts"]:
        interrupt_value = payload["interrupts"][0]["value"]
        lines.append("\n**风险项：**")
        for flag in interrupt_value.get("risk_flags", []):
            evidence = ", ".join(flag.get("evidence", []))
            lines.append(
                f"- [{flag['severity']}] {flag['description']}  \n  Evidence: {evidence}"
            )
    return "\n".join(lines)


async def _send_payload(payload: dict[str, Any]) -> None:
    actions: list[cl.Action] = []
    if payload["interrupts"]:
        thread_id = payload["thread_id"]
        actions = [
            cl.Action(
                name="approve_task",
                label="继续生成报告",
                payload={"thread_id": thread_id},
            ),
            cl.Action(
                name="research_task",
                label="补充调查",
                payload={"thread_id": thread_id},
            ),
        ]
    await cl.Message(content=_risk_markdown(payload), actions=actions).send()


@cl.on_chat_start
async def on_chat_start() -> None:
    await cl.Message(
        content=(
            "Credra Agent durable demo 已就绪。\n\n"
            "- `start case_normal` 或 `start case_risky`\n"
            "- `status <thread_id>`\n"
            "- `resume <thread_id> approve|research [comment]`"
        )
    ).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    parts = message.content.strip().split(maxsplit=3)
    settings = get_settings()
    try:
        if len(parts) == 2 and parts[0].lower() == "start":
            payload = start_task(
                thread_id=str(uuid.uuid4()),
                case_id=parts[1],
                settings=settings,
            )
        elif len(parts) == 2 and parts[0].lower() == "status":
            payload = get_task_status(thread_id=parts[1], settings=settings)
        elif len(parts) >= 3 and parts[0].lower() == "resume":
            payload = resume_task(
                thread_id=parts[1],
                decision=parts[2],
                comment=parts[3] if len(parts) == 4 else None,
                settings=settings,
            )
        else:
            raise ValueError("无法识别命令，请使用 start、status 或 resume。")
    except (OSError, ResearchServiceError, ValueError) as exc:
        await cl.Message(content=f"任务执行失败：`{type(exc).__name__}: {exc}`").send()
        return
    await _send_payload(payload)


@cl.action_callback("approve_task")
async def approve_task(action: cl.Action) -> None:
    payload = resume_task(
        thread_id=action.payload["thread_id"],
        decision="approve",
        comment="通过 Chainlit 审核继续生成报告。",
        settings=get_settings(),
    )
    await _send_payload(payload)


@cl.action_callback("research_task")
async def research_task(action: cl.Action) -> None:
    payload = resume_task(
        thread_id=action.payload["thread_id"],
        decision="research",
        comment="通过 Chainlit 请求补充调查。",
        settings=get_settings(),
    )
    await _send_payload(payload)


def serialize_for_debug(payload: dict[str, Any]) -> str:
    """Keep UI payloads easy to inspect without adding another state store."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
