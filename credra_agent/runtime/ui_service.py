"""Config-controlled Chainlit execution using the shared durable Agent Runtime."""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings
from app.llm.gateway import OpenAICompatibleStructuredModel, StructuredModelError
from app.mcp.research_client import ResearchMCPClient
from app.runtime.tasks import (
    get_task_status,
    graph_config,
    open_checkpointer,
    resume_agentic_task,
)
from app.search.content import (
    ContentSnapshotStore,
    HTTPContentFetcher,
    build_content_fetcher,
)
from app.search.providers import SearchConfigurationError, build_search_provider
from app.tools.artifacts import ArtifactStore
from credra_agent.execution.ledger import ActionLedger, LedgerError
from credra_agent.intent.models import IntentResult, TaskSpec
from credra_agent.intent.service import interpret_message
from credra_agent.intent.store import IntentStore
from credra_agent.observability.collector import LoggingUnavailable
from credra_agent.observability.events import log_context
from credra_agent.observability.runtime import (
    config_from_settings,
    emit,
    service_session,
)
from credra_agent.planning.models import Observation
from credra_agent.runtime.executors import build_agentic_executor, build_agentic_model
from credra_agent.runtime.service import execute_intent
from credra_agent.runtime.ui_budget import BudgetedIntentModel
from credra_agent.runtime.ui_policy import UIPolicy, load_ui_policy, model_profile
from credra_agent.runtime.ui_store import UIRequestBlocked, UITaskStore, task_lock


class UIRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str
    outcome: Literal[
        "CONFIGURATION_REQUIRED",
        "WAITING_CLARIFICATION",
        "CONTROL_UNAVAILABLE",
        "TASK_STATE",
        "LIMITED",
        "PAUSED_LOGGING",
    ]
    intent: IntentResult | None = None
    task: dict | None = None
    budget: dict | None = None
    limitations: list[str] = Field(default_factory=list)
    duplicate: bool = False
    observations: list[dict] = Field(default_factory=list)


def _validate_dependencies(settings: Settings):
    if settings.analysis_mode != "llm":
        raise ValueError("ANALYSIS_MODE_LLM_REQUIRED: UI 执行需要 ANALYSIS_MODE=llm")
    if not settings.model_api_key.get_secret_value().strip():
        raise ValueError("UI_MODEL_API_KEY_REQUIRED")
    if not (settings.analysis_model or settings.model_name).strip():
        raise ValueError("UI_MODEL_NAME_REQUIRED")
    if (
        settings.research_provider == "tavily"
        and not settings.tavily_api_key.get_secret_value().strip()
    ):
        raise ValueError("UI_TAVILY_CONFIGURATION_REQUIRED")
    try:
        build_search_provider(settings)
    except SearchConfigurationError as exc:
        raise ValueError("UI_RESEARCH_PROVIDER_CONFIGURATION_REQUIRED") from exc


def ui_execution_description(settings: Settings) -> str:
    if settings.agent_entry_policy_path is not None:
        from credra_agent.entry.policy import load_entry_policy

        if not settings.agent_ui_execution_enabled:
            return "LLM 对话入口已配置但未启用；设置 AGENT_UI_EXECUTION_ENABLED=true 后按独立会话策略运行。"
        try:
            policy = load_entry_policy(settings)
            controls = "、".join(policy.allowed_control_tools) or "未允许调查控制"
            return f"LLM 对话策略已就绪：{policy.policy_id} v{policy.version}；模型配置将在请求前校验。允许的调查控制：{controls}；委派还须有效的独立任务策略、配置和剩余额度。"
        except ValueError:
            return (
                "LLM 对话入口已配置，入口策略尚未批准或额度不完整；不会发起模型调用。"
            )
    if not settings.agent_ui_execution_enabled:
        return "自然语言入口：解析模式。设置 AGENT_UI_EXECUTION_ENABLED=true 并配置 AGENT_UI_POLICY_PATH 后可自动执行。"
    try:
        policy = load_ui_policy(settings)
        _validate_dependencies(settings)
        return f"自然语言入口：Agent 自动执行已启用；新任务使用策略 {policy.policy_id} v{policy.version}。澄清就绪后自动启动，额度按任务独立计账。"
    except ValueError as exc:
        return f"自然语言入口：已开启，配置未就绪。{exc}"


def build_investigation_model(settings: Settings, policy: UIPolicy):
    _validate_dependencies(settings)
    coordinator = build_agentic_model(settings, policy.limits)
    if coordinator is None:
        raise ValueError("ANALYSIS_MODE_LLM_REQUIRED: UI 执行需要 Coordinator 模型")
    return coordinator


def build_ui_models(settings: Settings, policy: UIPolicy):
    coordinator = build_investigation_model(settings, policy)
    intent = None
    if settings.intent_mode == "llm":
        intent = OpenAICompatibleStructuredModel(
            api_key=settings.model_api_key.get_secret_value(),
            base_url=settings.model_base_url,
            model_name=settings.intent_model
            or settings.analysis_model
            or settings.model_name,
            timeout_seconds=settings.analysis_llm_timeout_seconds,
            max_attempts=min(
                settings.analysis_llm_max_retry + 1, policy.intent_attempt_reservation
            ),
            max_input_chars=settings.analysis_llm_max_input_chars,
            max_output_tokens=policy.intent_max_output_tokens,
            enable_thinking=False,
            aggregate_accounting=True,
        )
    return coordinator, intent


def build_ui_executor(settings, spec, coordinator):
    snapshots = ContentSnapshotStore(settings.search_content_snapshot_dir)
    fetcher = build_content_fetcher(settings, restrict_redirect_host=True)
    # Explicit environment keeps programmatic Settings and the MCP child aligned.
    environment = {}
    for field, value in settings.model_dump().items():
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if value is not None:
            environment[field.upper()] = str(value)
    return build_agentic_executor(
        spec,
        research_client=ResearchMCPClient(environment=environment),
        verifier_model=coordinator,
        evidence_source_kind="SYNTHETIC"
        if settings.research_provider == "mock"
        else "UNKNOWN",
        document_loader=snapshots.read,
        content_fetcher=fetcher.fetch if fetcher else None,
        fetch_external_requests=settings.search_fetch_max_redirects + 1
        if isinstance(fetcher, HTTPContentFetcher)
        else 0,
        fetch_actual_external_requests=None
        if isinstance(fetcher, HTTPContentFetcher)
        else 0,
    )


def _task_state(settings, thread_id):
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        exists = checkpointer.get_tuple(graph_config(thread_id)) is not None
    return get_task_status(thread_id=thread_id, settings=settings) if exists else None


def _task_spec(settings, task):
    state = task["state"]
    run_dir = settings.data_dir / state["case_id"] / "runs" / state["run_id"]
    return TaskSpec.model_validate(
        ArtifactStore(run_dir).read_json(state["task_spec_ref"])
    )


def _observations(settings, task):
    if not task or task["state"].get("graph_version") != "agentic_v2":
        return []
    state = task["state"]
    store = ArtifactStore(
        settings.data_dir / state["case_id"] / "runs" / state["run_id"]
    )
    index = store.read_json(state["observation_index_ref"])
    ledger = ActionLedger(settings.checkpoint_db_path)
    observations = []
    for item in index.get("items", [])[-8:]:
        observation = Observation.model_validate(
            store.read_json(item["observation_ref"])
        )
        action = ledger.get_action(task["thread_id"], observation.action_id).action
        public = observation.model_dump(mode="json")
        public.update(tool=action.tool, query=action.arguments.get("query"))
        observations.append(public)
    return observations


def execute_ui_message(
    *,
    thread_id: str,
    message_id: str,
    text: str,
    as_of: date,
    settings: Settings,
    models_factory=None,
    executor_factory=None,
) -> UIRunResult:
    """No paid calls unless a valid user-maintained policy has already been bound."""
    if not settings.agent_ui_execution_enabled:
        return UIRunResult(
            thread_id=thread_id,
            outcome="CONFIGURATION_REQUIRED",
            limitations=["AGENT_UI_EXECUTION_DISABLED"],
        )
    try:
        with (
            service_session("ui_runtime", config_from_settings(settings)),
            task_lock(settings.checkpoint_db_path, thread_id),
            log_context(thread_id=thread_id),
        ):
            return _execute_locked(
                thread_id=thread_id,
                message_id=message_id,
                text=text,
                as_of=as_of,
                settings=settings,
                models_factory=models_factory or build_ui_models,
                executor_factory=executor_factory or build_ui_executor,
            )
    except UIRequestBlocked as exc:
        return UIRunResult(
            thread_id=thread_id, outcome="CONTROL_UNAVAILABLE", limitations=[str(exc)]
        )
    except LoggingUnavailable:
        return UIRunResult(
            thread_id=thread_id,
            outcome="PAUSED_LOGGING",
            limitations=["日志初始化失败，未派发新动作。请恢复日志后重试。"],
        )


def _execute_locked(
    *, thread_id, message_id, text, as_of, settings, models_factory, executor_factory
):
    store = UITaskStore(settings.checkpoint_db_path)
    cached = store.claim(thread_id, message_id, text)
    task = _task_state(settings, thread_id)
    if cached:
        result = UIRunResult.model_validate(cached)
        frozen = store.policy(thread_id)
        return result.model_copy(
            update={
                "duplicate": True,
                "task": task or result.task,
                "observations": _observations(settings, task)
                if task
                else result.observations,
                "budget": ActionLedger(settings.checkpoint_db_path).budget_snapshot(
                    thread_id, frozen[1]
                )
                if frozen
                else result.budget,
            }
        )
    command = text.strip().lower()
    pending_intent = None
    status = bool(
        re.fullmatch(r"(?:status|状态|查看状态|查看进度|当前进度)[？?。]?", command)
    )
    resume = bool(
        re.fullmatch(
            r"(?:resume|继续|恢复|继续执行|恢复任务|日志恢复后继续)[。]?", command
        )
    )

    def respond(
        outcome, *, intent=None, limitations=None, current_task=None, budget=None
    ):
        observations = _observations(settings, current_task)
        if current_task and current_task.get("execution_blocked"):
            outcome = "PAUSED_LOGGING"
            limitations = [
                *(limitations or []),
                "日志不可用，保留已发生请求结果；恢复日志后输入继续。",
            ]
        result = UIRunResult(
            thread_id=thread_id,
            outcome=outcome,
            intent=intent,
            limitations=limitations or [],
            task=current_task,
            budget=budget,
            observations=observations,
        )
        store.save_response(message_id, result.model_dump(mode="json"))
        return result

    if status:
        frozen = store.policy(thread_id)
        latest = IntentStore(settings.checkpoint_db_path).latest_task_spec(thread_id)
        parsed = (
            IntentStore(settings.checkpoint_db_path).find_message(
                latest.source_message_id
            )
            if latest and not task
            else None
        )
        return respond(
            "TASK_STATE"
            if task
            else "WAITING_CLARIFICATION"
            if latest and latest.readiness != "READY"
            else "CONTROL_UNAVAILABLE",
            current_task=task,
            intent=parsed,
            limitations=[]
            if task
            else list(latest.unresolved_fields)
            if latest and latest.readiness != "READY"
            else ["该任务尚未启动调查；信息就绪后可输入继续。"],
            budget=ActionLedger(settings.checkpoint_db_path).budget_snapshot(
                thread_id, frozen[1]
            )
            if frozen
            else None,
        )
    if task and not resume and task["state"].get("status") != "WAITING_CLARIFICATION":
        return respond(
            "CONTROL_UNAVAILABLE",
            current_task=task,
            limitations=[
                "已有运行不接受新的调查指令；使用 agent new 新建任务，或输入状态/继续。运行中修改留待 V2-4。"
            ],
        )
    if resume and task is None:
        latest = IntentStore(settings.checkpoint_db_path).latest_task_spec(thread_id)
        if latest and latest.readiness == "READY":
            pending_intent = IntentStore(settings.checkpoint_db_path).find_message(
                latest.source_message_id
            )
        else:
            return respond(
                "WAITING_CLARIFICATION" if latest else "CONTROL_UNAVAILABLE",
                limitations=list(latest.unresolved_fields)
                if latest
                else ["调查尚未启动；请先补充自然语言任务信息。"],
            )

    frozen = store.policy(thread_id)
    try:
        if frozen:
            policy, authorization, profile = frozen
            if profile != model_profile(settings):
                raise ValueError(
                    "UI_REQUEST_CONFIGURATION_CHANGED: 原任务请求配置已变化，请恢复原配置或新建任务"
                )
        else:
            if task:
                raise ValueError(
                    "UI_POLICY_NOT_BOUND: 该任务不是配置策略创建的 UI 任务，请使用原执行入口恢复"
                )
            policy = load_ui_policy(settings)
            authorization = policy.authorization(thread_id, 1)
        coordinator, intent_model = models_factory(settings, policy)
        if coordinator is None:
            raise ValueError("COORDINATOR_MODEL_REQUIRED")
        if frozen is None:
            store.bind_policy(thread_id, policy, authorization, model_profile(settings))
    except (ValueError, StructuredModelError) as exc:
        # Errors may include user configuration; only publish known safe diagnostics.
        message = (
            str(exc)
            if isinstance(exc, ValueError)
            and str(exc).startswith(
                ("AGENT_UI_", "UI_", "ANALYSIS_MODE_", "COORDINATOR_")
            )
            else "MODEL_CONFIGURATION_REQUIRED: 请检查模型配置"
        )
        return respond(
            "CONFIGURATION_REQUIRED", current_task=task, limitations=[message]
        )

    ledger = ActionLedger(settings.checkpoint_db_path)
    try:
        if resume and task is not None:
            if task["state"].get("status") == "WAITING_CLARIFICATION":
                return respond(
                    "WAITING_CLARIFICATION",
                    current_task=task,
                    limitations=["请回答待澄清问题，单独输入继续不会消除缺项。"],
                )
            spec = _task_spec(settings, task)
            current = resume_agentic_task(
                thread_id=thread_id,
                settings=settings,
                model=coordinator,
                executor=executor_factory(settings, spec, coordinator),
            )
            return respond(
                "TASK_STATE",
                current_task=current,
                budget=ledger.budget_snapshot(thread_id, authorization),
            )

        def validate_spec(spec):
            if spec.subject_id and spec.subject_id not in policy.allowed_subject_ids:
                raise UIRequestBlocked(
                    "SUBJECT_NOT_AUTHORIZED: 主体不在可信策略允许范围内"
                )
            if task:
                previous = _task_spec(settings, task)
                if (
                    spec.case_id != previous.case_id
                    or spec.subject_id != previous.subject_id
                ):
                    raise UIRequestBlocked(
                        "SCOPE_CHANGE_REQUIRES_NEW_TASK: 澄清不能替换已启动任务的主体"
                    )

        budgeted = (
            BudgetedIntentModel(
                intent_model,
                store=store,
                ledger=ledger,
                thread_id=thread_id,
                message_id=message_id,
                policy=policy,
                authorization=authorization,
            )
            if intent_model
            else None
        )
        intent = pending_intent or interpret_message(
            thread_id=thread_id,
            source_message_id=message_id,
            text=text,
            as_of=as_of,
            data_dir=settings.data_dir,
            database_path=settings.checkpoint_db_path,
            model=budgeted,
            task_spec_validator=validate_spec,
        )
        if pending_intent:
            validate_spec(pending_intent.task_spec)
        approved = authorization.model_copy(
            update={"task_spec_version": intent.bound_task_spec_version}
        )
        emit("REQUEST_ACCEPTED", status="ACCEPTED", node="ui_runtime")
        result = execute_intent(
            intent=intent,
            settings=settings,
            authorization=approved,
            coordinator_model=coordinator,
            executor_factory=lambda spec: executor_factory(settings, spec, coordinator),
        )
        emit("REQUEST_END", status="SUCCESS", node="ui_runtime")
        return respond(
            result.outcome,
            intent=intent,
            limitations=result.limitations,
            current_task=result.task,
            budget=ledger.budget_snapshot(thread_id, authorization),
        )
    except LoggingUnavailable:
        current_task = _task_state(settings, thread_id)
        return respond(
            "PAUSED_LOGGING",
            current_task=current_task,
            budget=ledger.budget_snapshot(thread_id, authorization),
            limitations=[
                "日志不可用，暂停新动作；恢复日志后输入继续。"
                if current_task
                else "日志不可用，尚未启动调查；恢复日志后重新发送任务内容，同一任务额度保留。"
            ],
        )
    except LedgerError as exc:
        return respond(
            "LIMITED",
            current_task=_task_state(settings, thread_id),
            budget=ledger.budget_snapshot(thread_id, authorization),
            limitations=[str(exc)],
        )
