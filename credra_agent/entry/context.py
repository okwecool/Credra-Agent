"""Assemble and persist bounded, trusted input before an entry model is called."""

import hashlib

from credra_agent.entry.models import EntryContext, ListCasesArgs
from credra_agent.entry.store import EntryConflict, now
from credra_agent.execution.registry import default_registry
from credra_agent.observability.events import log_context
from credra_agent.observability.runtime import (
    config_from_settings,
    emit,
    service_session,
)


class ContextLimited(EntryConflict):
    pass


def workspace_reference(settings):
    # Scope is a local deployment binding, not multi-user authentication.
    value = f"{settings.data_dir.resolve()}\n{settings.checkpoint_db_path.resolve()}"
    return "workspace-" + hashlib.sha256(value.encode()).hexdigest()


class EntryContextBuilder:
    def __init__(
        self,
        registry,
        *,
        max_input_chars=30000,
        max_history_messages=12,
        task_page_size=20,
        input_measurer=None,
    ):
        if (
            max_input_chars < 1000
            or not 1 <= max_history_messages <= 100
            or not 1 <= task_page_size <= 50
        ):
            raise ValueError("ENTRY_CONTEXT_LIMIT_INVALID")
        self.registry = registry
        self.store = registry.store
        self.max_input_chars = max_input_chars
        self.max_history_messages = max_history_messages
        self.task_page_size = task_page_size
        self.input_measurer = input_measurer or (
            lambda context: len(context.model_dump_json())
        )

    def build(
        self,
        *,
        message_id,
        query,
        anchor_date,
        permissions,
        timezone="Asia/Singapore",
        investigation_executor=None,
        turn_events=None,
    ):
        with service_session(
            "entry_context", config_from_settings(self.registry.settings)
        ):
            return self._build(
                message_id=message_id,
                query=query,
                anchor_date=anchor_date,
                permissions=permissions,
                timezone=timezone,
                investigation_executor=investigation_executor,
                turn_events=turn_events,
            )

    def _build(
        self,
        *,
        message_id,
        query,
        anchor_date,
        permissions,
        timezone="Asia/Singapore",
        investigation_executor=None,
        turn_events=None,
    ):
        if (
            frozenset(permissions.allowed_subject_ids)
            != self.registry.query.allowed_subject_ids
        ):
            raise EntryConflict("ENTRY_PERMISSION_SCOPE_MISMATCH")
        conversation = self.store.conversation(
            self.registry.conversation_id, self.registry.workspace_ref
        )
        history, limited = self.store.history(
            self.registry.conversation_id,
            self.registry.workspace_ref,
            max_messages=self.max_history_messages,
            exclude_turn=message_id,
        )
        current = (
            self.registry.query.get(conversation["selected_task_id"])
            if conversation["selected_task_id"]
            else None
        )
        page = self.registry.query.list(limit=self.task_page_size)
        case_page = self.registry._list_cases(ListCasesArgs(limit=self.task_page_size))
        executable = (
            investigation_executor.executable_tools if investigation_executor else set()
        )
        capabilities = []
        for tool in default_registry().catalog():
            capabilities.append(
                {
                    "name": tool["name"],
                    "read_only": tool["read_only"],
                    "registered_available": tool["available"],
                    "available_in_selected_executor": tool["available"]
                    and tool["name"] in executable,
                    "description": tool.get("description", "")
                    + "仅由已授权调查 Runtime 调用，不能作为入口工具直接执行。",
                    "unavailable_reason": tool["unavailable_reason"]
                    or (
                        None
                        if tool["name"] in executable
                        else "当前未提供实际调查执行器"
                    ),
                }
            )
        context = EntryContext(
            conversation_id=self.registry.conversation_id,
            message_id=message_id,
            query=query,
            anchor_date=anchor_date,
            timezone=timezone,
            snapshot_at=now(),
            history=history,
            turn_events=turn_events or [],
            selected_task_id=conversation["selected_task_id"],
            current_task=current,
            pending_question=conversation["pending_question"],
            task_page=page,
            imported_cases=case_page["items"],
            case_catalog_has_more=case_page["has_more"],
            case_catalog_next_cursor=case_page["next_cursor"],
            tools=self.registry.catalog(permissions),
            tool_contracts=self.registry.contracts(),
            investigation_capabilities=capabilities,
            permissions=permissions,
            limitations=["会话历史已截断，仅保留完整消息轮次。"] if limited else [],
        )
        # Remove only optional complete history turns/page rows. Never cut query,
        # negative constraints, current task, pending questions or tool schemas.
        while self.input_measurer(context) > self.max_input_chars and context.history:
            turn = context.history[0]["turn_id"]
            context.history = [
                item for item in context.history if item["turn_id"] != turn
            ]
            if "输入容量限制：历史轮次已缩减。" not in context.limitations:
                context.limitations.append("输入容量限制：历史轮次已缩减。")
        while (
            self.input_measurer(context) > self.max_input_chars
            and context.task_page.items
        ):
            context.task_page.items.pop()
            context.task_page.has_more = True
            # A stale cursor from the larger page must not skip removed rows.
            context.task_page.next_cursor = None
            if (
                "上下文任务摘要已缩减；通过 list_tasks 从第一页读取。"
                not in context.limitations
            ):
                context.limitations.append(
                    "上下文任务摘要已缩减；通过 list_tasks 从第一页读取。"
                )
        if self.input_measurer(context) > self.max_input_chars:
            raise ContextLimited(
                "CONTEXT_LIMITED: 必需上下文超过容量，未保存快照或调用模型"
            )
        context_id = self.store.snapshot(context)
        with log_context(
            conversation_id=context.conversation_id,
            message_id=message_id,
            context_id=context_id,
            node="entry_context",
        ):
            emit("REQUEST_ACCEPTED", status="ACCEPTED")
        return context, context_id
