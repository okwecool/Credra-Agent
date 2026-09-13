"""Budgeted model/tool dialogue and acknowledged shared-Runtime investigation commands."""

import hashlib
import json
import re
from time import perf_counter
from typing import Literal

from pydantic import Field

from app.llm.gateway import OpenAICompatibleStructuredModel, StructuredModelError
from credra_agent.entry.budget import (
    EntryBudgetLimited,
    EntryBudgetStore,
    EntryRequestUncertain,
)
from credra_agent.entry.context import (
    ContextLimited,
    EntryContextBuilder,
    workspace_reference,
)
from credra_agent.entry.delegation import InvestigationDelegation
from credra_agent.entry.models import (
    EntryDecision,
    EntryModel,
    EntryPermissions,
    EntryToolResult,
)
from credra_agent.entry.policy import load_entry_policy, request_profile
from credra_agent.entry.query import TaskQueryService
from credra_agent.entry.registry import EntryToolRegistry
from credra_agent.entry.store import EntryConflict, EntryStore
from credra_agent.observability.collector import LoggingUnavailable
from credra_agent.observability.events import log_context
from credra_agent.observability.runtime import (
    config_from_settings,
    emit,
    require_logging,
    service_session,
)
from credra_agent.prompts.entry import PROMPT_VERSION, SYSTEM_PROMPT
from credra_agent.runtime.ui_store import UIRequestBlocked, task_lock


class EntryRunResult(EntryModel):
    conversation_id: str
    status: Literal[
        "REPLY", "ASK_USER", "LIMITED", "PAUSED_LOGGING", "CONFIGURATION_REQUIRED"
    ]
    text: str
    selected_task_id: str | None = None
    budget: dict | None = None
    tools: list[EntryToolResult] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    duplicate: bool = False


def build_entry_model(settings, policy):
    return OpenAICompatibleStructuredModel(
        api_key=settings.model_api_key.get_secret_value(),
        base_url=settings.model_base_url,
        model_name=settings.intent_model
        or settings.analysis_model
        or settings.model_name,
        timeout_seconds=settings.analysis_llm_timeout_seconds,
        max_attempts=min(
            settings.analysis_llm_max_retry + 1, policy.model_attempt_reservation
        ),
        max_input_chars=min(
            settings.analysis_llm_max_input_chars, policy.max_input_chars
        ),
        max_output_tokens=policy.max_output_tokens,
        enable_thinking=False,
        aggregate_accounting=True,
    )


def input_size(context):
    return len(SYSTEM_PROMPT) + len(
        json.dumps(
            {
                "purpose": "entry_decision",
                "prompt_version": PROMPT_VERSION,
                "input": context.model_dump(mode="json"),
                "output_json_schema": EntryDecision.model_json_schema(),
                "public_process_summary": None,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def execute_entry_message(
    *,
    conversation_id,
    message_id,
    text,
    as_of,
    settings,
    model_factory=None,
    investigation_model_factory=None,
    executor_factory=None,
):
    if not settings.agent_ui_execution_enabled:
        return EntryRunResult(
            conversation_id=conversation_id,
            status="CONFIGURATION_REQUIRED",
            text="对话入口尚未启用。",
            limitations=["AGENT_UI_EXECUTION_DISABLED"],
        )
    try:
        with (
            service_session("entry_runtime", config_from_settings(settings)),
            task_lock(settings.checkpoint_db_path, conversation_id),
            log_context(
                conversation_id=conversation_id, message_id=message_id, node="entry"
            ),
        ):
            return _execute_locked(
                conversation_id,
                message_id,
                text,
                as_of,
                settings,
                model_factory or build_entry_model,
                investigation_model_factory,
                executor_factory,
            )
    except LoggingUnavailable:
        return EntryRunResult(
            conversation_id=conversation_id,
            status="PAUSED_LOGGING",
            text="日志不可用，暂停新对话动作；已返回结果保留，恢复日志后重试。",
        )
    except UIRequestBlocked:
        return EntryRunResult(
            conversation_id=conversation_id,
            status="LIMITED",
            text="当前会话正在处理消息，请稍后重试。",
            limitations=["ENTRY_CONVERSATION_BUSY"],
        )


def _execute_locked(
    conversation_id,
    message_id,
    text,
    as_of,
    settings,
    model_factory,
    investigation_model_factory=None,
    executor_factory=None,
):
    store = EntryStore(settings.checkpoint_db_path)
    workspace = workspace_reference(settings)
    store.ensure_conversation(conversation_id, workspace)
    budget = EntryBudgetStore(store)
    try:
        frozen = budget.frozen(conversation_id)
        policy = frozen[0] if frozen else load_entry_policy(settings)
        profile = request_profile(settings, policy, PROMPT_VERSION)
        if frozen and frozen[1] != profile:
            raise ValueError("ENTRY_REQUEST_PROFILE_CHANGED")
        model = model_factory(settings, policy)
        if frozen is None:
            budget.bind(conversation_id, policy, profile)
    except (ValueError, StructuredModelError):
        return EntryRunResult(
            conversation_id=conversation_id,
            status="CONFIGURATION_REQUIRED",
            text="入口会话策略或模型配置未就绪；请检查批准状态、额度、模型及原会话配置。",
            limitations=["ENTRY_CONFIGURATION_REQUIRED"],
        )
    query = TaskQueryService(settings, allowed_subject_ids=policy.allowed_subject_ids)
    delegation = InvestigationDelegation(
        settings=settings,
        query=query,
        conversation_id=conversation_id,
        workspace_ref=workspace,
        message_id=message_id,
        text=text,
        as_of=as_of,
        policy=policy,
        model_factory=investigation_model_factory,
        executor_factory=executor_factory,
    )
    cached = budget.claim_turn(conversation_id, message_id, text)
    if cached:
        previous = EntryRunResult.model_validate(cached)
        control = next(
            (
                tool
                for tool in reversed(previous.tools)
                if tool.tool in policy.allowed_control_tools
                and tool.data.get("command")
            ),
            None,
        )
        if control:
            command = store.command(control.data["command"]["command_id"])
            if command and command["conversation_id"] == conversation_id:
                if command["status"] == "QUEUED":
                    delegation.schedule(command["command_id"])
                refreshed = delegation._receipt(command, control.call_id)
                return previous.model_copy(
                    update={
                        "duplicate": True,
                        "budget": budget.snapshot(conversation_id, policy),
                        "status": "LIMITED"
                        if refreshed.status in {"UNKNOWN", "REJECTED"}
                        else "REPLY",
                        "text": _investigation_receipt(refreshed),
                        "tools": [*previous.tools[:-1], refreshed],
                    }
                )
        return previous.model_copy(
            update={
                "duplicate": True,
                "budget": budget.snapshot(conversation_id, policy),
            }
        )
    registry = EntryToolRegistry(
        settings=settings,
        query_service=query,
        conversation_id=conversation_id,
        workspace_ref=workspace,
        delegation=delegation,
    )
    builder = EntryContextBuilder(
        registry,
        max_input_chars=min(
            policy.max_input_chars, settings.analysis_llm_max_input_chars
        ),
        input_measurer=input_size,
    )
    store.append(
        conversation_id=conversation_id,
        workspace_ref=workspace,
        message_id=f"entry-user:{message_id}",
        turn_id=message_id,
        role="user",
        payload={"text": text},
    )
    results = []

    def respond(status, public, limitations=None):
        result = EntryRunResult(
            conversation_id=conversation_id,
            status=status,
            text=public,
            selected_task_id=store.conversation(conversation_id, workspace)[
                "selected_task_id"
            ],
            budget=budget.snapshot(conversation_id, policy),
            tools=results,
            limitations=limitations or [],
        )
        budget.save_response(message_id, result.model_dump(mode="json"))
        return result

    def permissions():
        spent = budget.snapshot(conversation_id, policy)
        selected = store.conversation(conversation_id, workspace)["selected_task_id"]
        task_budget = query.get(selected).budget if selected else None
        return EntryPermissions(
            allowed_subject_ids=policy.allowed_subject_ids,
            entry_authorized=True,
            entry_policy_ref=policy.reference,
            conversation_remaining_requests=max(
                0, policy.external_request_limit - spent["external_spent"]
            ),
            conversation_remaining_tokens=max(
                0, policy.token_limit - spent["token_spent"]
            ),
            logging_healthy=True,
            request_profile_compatible=True,
            investigation_control_enabled=bool(policy.allowed_control_tools),
            allowed_control_tools=policy.allowed_control_tools,
            task_policy_ref=task_budget.get("policy_ref") if task_budget else None,
            task_remaining_requests=task_budget.get("remaining_external")
            if task_budget
            else None,
            task_remaining_tokens=task_budget.get("remaining_tokens")
            if task_budget
            else None,
        )

    try:
        for round_number in range(policy.max_model_rounds):
            operation = f"entry:{message_id}:{round_number}"
            row = budget.operation(conversation_id, operation)
            if row is None:
                context, _ = builder.build(
                    message_id=message_id,
                    query=text,
                    anchor_date=as_of,
                    permissions=permissions(),
                    turn_events=store.turn_events(conversation_id, message_id),
                    investigation_executor=delegation.selected_executor(
                        store.conversation(conversation_id, workspace)[
                            "selected_task_id"
                        ]
                    ),
                )
                payload = context.model_dump(mode="json")
                require_logging()
                budget.reserve(conversation_id, operation, payload, policy)
                row = budget.operation(conversation_id, operation)
            if row["result_json"] is None:
                if row["status"] != "RESERVED":
                    budget.uncertain(conversation_id, operation)
                    raise EntryRequestUncertain("ENTRY_MODEL_RESULT_UNCERTAIN")
                require_logging()
                budget.dispatch(conversation_id, operation)
                started = perf_counter()
                try:
                    generated = model.generate(
                        output_schema=EntryDecision,
                        purpose="entry_decision",
                        prompt_version=PROMPT_VERSION,
                        system_prompt=SYSTEM_PROMPT,
                        payload=json.loads(row["payload_json"]),
                        max_output_tokens=policy.max_output_tokens,
                    )
                    complete = (
                        generated.accounting_complete
                        and type(generated.input_tokens) is int
                        and generated.input_tokens >= 0
                        and type(generated.output_tokens) is int
                        and generated.output_tokens >= 0
                    )
                    returned = {
                        "decision": generated.output.model_dump(mode="json"),
                        "error": None,
                        "external": generated.external_requests
                        if generated.external_requests is not None
                        else generated.attempts,
                        "tokens": generated.input_tokens + generated.output_tokens
                        if complete
                        else None,
                        "active_seconds": perf_counter() - started,
                    }
                except StructuredModelError as exc:
                    returned = {
                        "decision": None,
                        "error": exc.code,
                        "external": exc.external_requests,
                        "tokens": 0 if exc.external_requests == 0 else None,
                        "active_seconds": perf_counter() - started,
                    }
                except BaseException:
                    budget.uncertain(conversation_id, operation)
                    raise
                budget.save_result(conversation_id, operation, returned)
            returned = budget.settle(conversation_id, operation)
            settled = budget.operation(conversation_id, operation)
            emit(
                "BUDGET_STATE",
                status="UNKNOWN"
                if settled["actual_external"] is None
                or settled["actual_tokens"] is None
                else "SUCCESS",
                external_requests=settled["actual_external"]
                if settled["actual_external"] is not None
                else policy.model_attempt_reservation,
                token_units=settled["actual_tokens"]
                if settled["actual_tokens"] is not None
                else policy.model_token_reservation,
                call_id=operation,
            )
            if returned["error"]:
                return respond(
                    "LIMITED",
                    "入口模型未能返回可采用的结构化决策；没有创建或派发调查。",
                    [returned["error"]],
                )
            decision = EntryDecision.model_validate(returned["decision"]).root
            emit(
                "ROUTE",
                status="REUSED" if row["result_json"] is not None else "SUCCESS",
                call_id=operation,
                target_node=decision.decision,
                tool=decision.tool if decision.decision == "call_tool" else None,
            )
            store.append(
                conversation_id=conversation_id,
                workspace_ref=workspace,
                message_id=f"entry-decision:{operation}",
                turn_id=message_id,
                role="assistant",
                payload=decision.model_dump(mode="json"),
            )
            if decision.decision == "ask_user":
                require_logging()
                store.pending(conversation_id, workspace, decision)
                return respond(
                    "ASK_USER", decision.question + "\n" + "\n".join(decision.options)
                )
            if decision.decision == "reply":
                public = _ground_reply(
                    decision, json.loads(row["payload_json"]), results
                )
                require_logging()
                store.pending(conversation_id, workspace, None)
                return respond("REPLY", public)
            tool_operation = f"tool:{operation}"
            budget.claim_tool(conversation_id, tool_operation, message_id, policy)
            tool_row = budget.tool(conversation_id, tool_operation)
            if tool_row["result_json"] is None:
                definition = registry.definitions.get(decision.tool)
                if (
                    tool_row["status"] == "DISPATCHED"
                    and definition
                    and definition.effect
                    not in {"READ_LOCAL", "DISPATCH_INVESTIGATION"}
                ):
                    raise EntryRequestUncertain("ENTRY_LOCAL_EFFECT_RESULT_UNCERTAIN")
                require_logging()
                budget.dispatch_tool(conversation_id, tool_operation)
                started = perf_counter()
                tool_result = registry.execute(
                    tool=decision.tool,
                    arguments=decision.arguments,
                    call_id=tool_operation,
                    permissions=permissions(),
                )
                tool_result.fact_refs.append(
                    "entry-result:"
                    + hashlib.sha256(tool_operation.encode()).hexdigest()
                )
                budget.save_tool(
                    conversation_id,
                    tool_operation,
                    tool_result.model_dump(mode="json"),
                    perf_counter() - started,
                )
                tool_row = budget.tool(conversation_id, tool_operation)
            tool_result = EntryToolResult.model_validate_json(tool_row["result_json"])
            results.append(tool_result)
            store.append(
                conversation_id=conversation_id,
                workspace_ref=workspace,
                message_id=f"entry-result:{tool_operation}",
                turn_id=message_id,
                role="tool",
                payload=tool_result.model_dump(mode="json"),
            )
            definition = registry.definitions.get(decision.tool)
            if (
                definition
                and definition.effect == "DISPATCH_INVESTIGATION"
                and tool_result.status
                in {"ACCEPTED", "WAITING_CLARIFICATION", "SUCCESS", "UNKNOWN"}
            ):
                store.pending(conversation_id, workspace, None)
                return respond(
                    "REPLY" if tool_result.status != "UNKNOWN" else "LIMITED",
                    _investigation_receipt(tool_result),
                    tool_result.limitations,
                )
        return respond(
            "LIMITED",
            "本条消息的入口决策轮次已用完；已读取结果保留，没有派发调查。",
            ["ENTRY_MODEL_ROUND_LIMIT"],
        )
    except (EntryBudgetLimited, ContextLimited) as exc:
        return respond(
            "LIMITED",
            "入口预算、容量或结果确定性受限；没有派发新调查。",
            [str(exc).split(":")[0]],
        )
    except LoggingUnavailable:
        return EntryRunResult(
            conversation_id=conversation_id,
            status="PAUSED_LOGGING",
            text="日志不可用，暂停新动作；恢复日志后重试同一消息以回放已保存结果。",
            budget=budget.snapshot(conversation_id, policy),
            tools=results,
        )
    except EntryConflict:
        return respond(
            "LIMITED",
            "入口决策引用或事实不受支持；没有派发调查。",
            ["ENTRY_FACT_OR_SCOPE_REJECTED"],
        )


def _investigation_receipt(result):
    data = result.data
    command = data.get("command", {})
    task_id = data.get("task_id", command.get("task_id", "未知"))
    if result.status == "WAITING_CLARIFICATION":
        fields = data.get("unresolved_fields", [])
        text = (
            f"任务 {task_id} 等待澄清；待补充：{'、'.join(fields) if fields else '请核对任务中尚未解决的问题'}。"
            + (
                "草稿已保存，尚未派发调查。"
                if data.get("summary", {}).get("kind") == "DRAFT"
                else "当前调查等待补充，未新增派发。"
            )
        )
    elif data.get("terminal"):
        text = f"任务 {task_id} 已无待执行节点，返回原保存状态 {data['status']}；未新增调用。"
    elif result.status == "UNKNOWN":
        text = f"任务 {task_id} 的命令结果不确定，未自动重发；请核查已保存状态。"
    elif result.status == "REJECTED":
        text = f"任务 {task_id} 的命令被拒绝：{'、'.join(result.limitations)}。未新增调查派发。"
    elif command.get("status") == "ACKED":
        text = f"任务 {task_id} 的命令已处理；当前保存状态 {data.get('summary', {}).get('status', '未知')}。返回保存结果，不表示报告已获批准。"
    else:
        text = f"调查命令已接受：{task_id}；命令 {command.get('command_id')}，提交快照状态 {command.get('status')}。后台按原任务授权执行；此回执不表示报告已完成。"
    task_budget = data.get("budget")
    if task_budget:
        text += f"\n调查预算（TASK）：请求 {task_budget['external_spent']}/{task_budget['external_limit']}；Token {task_budget['token_spent']}/{task_budget['token_limit']}。入口调用另计会话。"
    if data.get("pending_clarification"):
        text += "\n待回答：" + data["pending_clarification"]["question"]
    return text


def _ground_reply(reply, payload, results):
    if reply.content_kind == "capabilities":
        available = [item["name"] for item in payload["tools"] if item["available"]]
        unavailable = [
            f"{item['name']}：{item['unavailable_reason']}"
            for item in payload["tools"]
            if not item["available"]
        ]
        return (
            "当前可用入口工具："
            + "、".join(available)
            + "。\n尚不可用："
            + "；".join(unavailable)
        )
    facts = {item["state_ref"]: item for item in payload["task_page"]["items"]}
    if payload["current_task"]:
        item = payload["current_task"]["summary"]
        facts[item["state_ref"]] = item
    limitations = [*payload["limitations"], *payload["task_page"]["limitations"]]
    tool_refs = set()
    scopes = {}
    for result in results:
        tool_refs.update(result.fact_refs)
        scopes[
            "entry-result:" + hashlib.sha256(result.call_id.encode()).hexdigest()
        ] = result
        limitations.extend(result.limitations)
        if result.tool == "list_tasks":
            for item in result.data.get("items", []):
                facts[item["state_ref"]] = item
        elif result.tool in {"get_task_status", "select_task"} and result.data.get(
            "summary"
        ):
            item = result.data["summary"]
            facts[item["state_ref"]] = item
        elif result.tool == "get_task_result":
            scopes.update({ref: result for ref in result.fact_refs})
    if reply.content_kind == "conversation":
        if reply.fact_refs or re.search(
            r"任务|调查|报告|授信|完成|批准|拒绝|贷款|金额|利润|现金流|收入|债务|应收|担保|处罚|比亚迪|上汽|\d|\b(?:RUNNING|COMPLETED|READY|LIMITED)\b",
            reply.text,
        ):
            raise EntryConflict("ENTRY_UNREFERENCED_TASK_CLAIM")
        return reply.text
    if not reply.fact_refs or any(
        ref not in facts and ref not in tool_refs for ref in reply.fact_refs
    ):
        raise EntryConflict("ENTRY_REPLY_FACT_REFERENCE_INVALID")
    selected = [facts[ref] for ref in reply.fact_refs if ref in facts]
    case_rows = []
    referenced_results = []
    for ref in reply.fact_refs:
        if ref in scopes:
            result = scopes[ref]
            if not any(saved.call_id == result.call_id for saved in referenced_results):
                referenced_results.append(result)
            if result.tool == "list_tasks":
                selected.extend(result.data.get("items", []))
            elif result.tool == "list_cases":
                case_rows.extend(result.data.get("items", []))
            elif result.tool in {"get_task_status", "select_task"} and result.data.get(
                "summary"
            ):
                selected.append(result.data["summary"])
    selected = list({item["state_ref"]: item for item in selected}.values())
    lines = [f"以下为查询时保存的任务记录（快照 {payload['snapshot_at']}）："]
    lines.extend(
        f"- {item['task_id']} · {item['subject_name'] or '主体未知'} · {item['kind']} · {item['status']} · 年度 {item['years'] or '未知'}"
        for item in selected
    )
    if any(result.tool == "list_cases" for result in referenced_results):
        lines.append("以下为已导入资料包，不代表已创建调查任务：")
        lines.extend(
            f"- {item['case_id']} · {item['subject_name']} · 年度 {item['available_years']} · 预检通过 {item['preflight_valid']}"
            for item in case_rows
        )
        if not case_rows:
            lines.append("本次结果未返回资料包。")
    for result in referenced_results:
        if result.tool == "get_task_result" and result.data:
            lines.append(
                f"任务 {result.data['task_id']} · 状态 {result.data['status']} · 停止原因 {result.data['stop_reason']} · 保存引用 {result.data['references']}"
            )
        if result.status not in {"SUCCESS", "NO_RESULT"}:
            lines.append(f"工具 {result.tool} 状态 {result.status}")
    if (
        not selected
        and not case_rows
        and not any(
            result.tool == "get_task_result" and result.data
            for result in referenced_results
        )
    ):
        lines.append("本次结果未返回任务记录。")
    if payload["task_page"]["has_more"] or any(
        result.data.get("has_more") for result in results
    ):
        lines.append("存在后续分页，本次结果不代表全部任务。")
    lines.extend(dict.fromkeys(limitations))
    return "\n".join(lines)
