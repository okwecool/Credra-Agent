"""Join natural-language intent, trusted authorization and graph execution."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path

from app.config import Settings
from app.llm.gateway import StructuredModel
from app.runtime.tasks import (
    amend_agentic_task,
    get_task_status,
    resume_agentic_task,
    start_agentic_task,
    start_task,
)
from credra_agent.execution.executor import ActionExecutor
from credra_agent.intent.models import TaskSpec
from credra_agent.intent.service import interpret_message
from credra_agent.planning.coordinator import DecisionFallback
from credra_agent.planning.models import RunAuthorization
from credra_agent.runtime.models import NaturalLanguageRunResult

ExecutorFactory = Callable[[TaskSpec], ActionExecutor]


def interpret_and_execute(
    *,
    thread_id: str,
    source_message_id: str,
    text: str,
    as_of: date,
    settings: Settings,
    execution_mode: str = "agentic",
    authorization: RunAuthorization | dict | None = None,
    intent_model: StructuredModel | None = None,
    coordinator_model: StructuredModel | None = None,
    executor_factory: ExecutorFactory | None = None,
    fallback: DecisionFallback | None = None,
) -> NaturalLanguageRunResult:
    """Interpret a message and execute only when trusted dependencies are explicit."""

    if execution_mode not in {"baseline", "shadow", "agentic"}:
        raise ValueError("execution_mode must be baseline, shadow or agentic")
    intent = interpret_message(
        thread_id=thread_id,
        source_message_id=source_message_id,
        text=text,
        as_of=as_of,
        data_dir=settings.data_dir,
        database_path=settings.checkpoint_db_path,
        model=intent_model,
    )

    if intent.operation == "status":
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode=execution_mode,
            outcome="TASK_STATE",
            task=get_task_status(thread_id=thread_id, settings=settings),
        )
    if intent.operation == "pause":
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode=execution_mode,
            outcome="CONTROL_UNAVAILABLE",
            limitations=["V2-1 未提供运行中节点的异步暂停；不会强制终止请求。"],
        )
    if intent.operation == "approve_report":
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode=execution_mode,
            outcome="CONTROL_UNAVAILABLE",
            limitations=[
                "V2-1 agentic 结果尚无可批准的最终授信报告；报告审核继续使用 baseline 审核入口。"
            ],
        )
    if intent.operation == "resume":
        if coordinator_model is None or executor_factory is None:
            return NaturalLanguageRunResult(
                intent=intent,
                execution_mode=execution_mode,
                outcome="CONFIGURATION_REQUIRED",
                limitations=["恢复 agentic 任务需要 Coordinator 模型和执行器。"],
            )
        spec = _latest_spec(settings.checkpoint_db_path, thread_id)
        task = resume_agentic_task(
            thread_id=thread_id,
            settings=settings,
            model=coordinator_model,
            executor=executor_factory(spec),
            fallback=fallback,
        )
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode=execution_mode,
            outcome="TASK_STATE",
            task=task,
        )

    spec = intent.task_spec
    assert spec is not None
    if spec.readiness != "READY":
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode=execution_mode,
            outcome="WAITING_CLARIFICATION",
            limitations=list(spec.unresolved_fields),
        )
    if execution_mode == "baseline":
        task = start_task(
            thread_id=thread_id,
            case_id=spec.case_id or "",
            settings=settings,
        )
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode="baseline",
            outcome="TASK_STATE",
            task=task,
        )
    if authorization is None:
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode=execution_mode,
            outcome="AUTHORIZATION_REQUIRED",
            limitations=["agentic/shadow 执行需要绑定当前 TaskSpec 的运行授权。"],
        )
    approved = RunAuthorization.model_validate(authorization)
    if approved.task_spec_version != spec.version:
        raise ValueError("AUTHORIZATION_SCOPE_MISMATCH")
    if coordinator_model is None or executor_factory is None:
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode=execution_mode,
            outcome="CONFIGURATION_REQUIRED",
            limitations=["agentic/shadow 执行需要 Coordinator 模型和执行器。"],
        )
    executor = executor_factory(spec)

    if intent.operation == "amend":
        task = amend_agentic_task(
            thread_id=thread_id,
            task_spec=spec,
            authorization=approved,
            settings=settings,
            model=coordinator_model,
            executor=executor,
            fallback=fallback,
        )
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode=execution_mode,
            outcome="TASK_STATE",
            task=task,
        )

    task = start_agentic_task(
        thread_id=thread_id,
        task_spec=spec,
        authorization=approved,
        settings=settings,
        model=coordinator_model,
        executor=executor,
        execution_mode=execution_mode,
        fallback=fallback,
    )
    if execution_mode == "agentic":
        return NaturalLanguageRunResult(
            intent=intent,
            execution_mode="agentic",
            outcome="TASK_STATE",
            task=task,
        )

    baseline_thread_id = f"{thread_id}-baseline"
    baseline = start_task(
        thread_id=baseline_thread_id,
        case_id=spec.case_id or "",
        settings=settings,
    )
    return NaturalLanguageRunResult(
        intent=intent,
        execution_mode="shadow",
        outcome="TASK_STATE",
        task=task,
        baseline_task=baseline,
        baseline_thread_id=baseline_thread_id,
    )


def _latest_spec(database_path: Path, thread_id: str) -> TaskSpec:
    from credra_agent.intent.store import IntentStore

    spec = IntentStore(database_path).latest_task_spec(thread_id)
    if spec is None:
        raise ValueError("CONTROL_COMMAND_REQUIRES_PERSISTED_TASK")
    return spec
