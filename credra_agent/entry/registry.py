"""Executable local entry tools and optional, policy-controlled investigation delegation."""

import base64
import hashlib
import json
from dataclasses import dataclass

from pydantic import ValidationError

from app.cases import validate_case
from credra_agent.entry.models import (
    ClarifyArgs,
    EntryToolResult,
    ListCasesArgs,
    ListTasksArgs,
    PrepareInvestigationArgs,
    ResumeArgs,
    TaskArgs,
    TaskPage,
    TaskResultArgs,
    TaskView,
)
from credra_agent.entry.store import EntryConflict, EntryStore, now
from credra_agent.intent.catalog import load_subject_catalog
from credra_agent.observability.events import log_context
from credra_agent.observability.runtime import (
    config_from_settings,
    emit,
    require_logging,
    service_session,
)


@dataclass(frozen=True)
class EntryToolDefinition:
    name: str
    description: str
    arguments_model: type
    effect: str
    prerequisites: str
    handler: object | None
    data_model: type | None = None


class EntryToolRegistry:
    def __init__(
        self,
        *,
        settings,
        query_service,
        conversation_id,
        workspace_ref,
        delegation=None,
    ):
        self.settings = settings
        self.query = query_service
        self.store = EntryStore(settings.checkpoint_db_path)
        self.conversation_id = conversation_id
        self.workspace_ref = workspace_ref
        self.delegation = delegation
        self.store.ensure_conversation(conversation_id, workspace_ref)
        definitions = [
            EntryToolDefinition(
                "list_tasks",
                "查询本地工作区已有任务和未启动草稿；返回分页与回填范围，不创建调查。",
                ListTasksArgs,
                "READ_LOCAL",
                "工作区可见性及企业范围",
                self._list_tasks,
                TaskPage,
            ),
            EntryToolDefinition(
                "get_task_status",
                "读取指定可见任务的最新持久化状态；不恢复任务或等待调查结束。",
                TaskArgs,
                "READ_LOCAL",
                "task_id 可见",
                self._get_task,
                TaskView,
            ),
            EntryToolDefinition(
                "select_task",
                "选择一个已存在的可见任务供后续对话指代；仅改变会话，不派发调查。",
                TaskArgs,
                "WRITE_CONVERSATION",
                "task_id 可见且日志健康",
                self._select_task,
                TaskView,
            ),
            EntryToolDefinition(
                "list_cases",
                "查询已导入资料包与财务年度、预检和材料范围；同企业不同资料包分别返回。",
                ListCasesArgs,
                "READ_LOCAL",
                "企业范围",
                self._list_cases,
            ),
            EntryToolDefinition(
                "get_task_result",
                "读取指定任务的结果状态和安全引用索引；当前不读取报告或证据全文。",
                TaskResultArgs,
                "READ_LOCAL",
                "task_id 可见；引用必须属于该任务",
                self._get_result,
            ),
            EntryToolDefinition(
                "prepare_investigation",
                "准备新调查；字段就绪后可能启动外部调用。",
                PrepareInvestigationArgs,
                "DISPATCH_INVESTIGATION",
                "有效任务策略、独立任务预算、日志及配置",
                delegation.execute if delegation else None,
            ),
            EntryToolDefinition(
                "submit_clarification",
                "向草稿或等待澄清任务补充字段，可能启动或恢复调查。",
                ClarifyArgs,
                "DISPATCH_INVESTIGATION",
                "原主体/来源/版本/授权不扩大",
                delegation.execute if delegation else None,
            ),
            EntryToolDefinition(
                "resume_investigation",
                "用原授权、配置和账本恢复调查；不确定请求不自动重发。",
                ResumeArgs,
                "DISPATCH_INVESTIGATION",
                "任务可见，状态版本匹配，原策略和预算有效",
                delegation.execute if delegation else None,
            ),
        ]
        self.definitions = {item.name: item for item in definitions}

    @staticmethod
    def _schema(model):
        schema = model.model_json_schema()
        definitions = schema.pop("$defs", {})

        def references(value, properties=False):
            if isinstance(value, dict):
                return {
                    key: item.replace("#/$defs/", "#/tool_contracts/")
                    if key == "$ref"
                    else references(item, properties=key == "properties")
                    for key, item in value.items()
                    if key != "title" or properties
                }
            if isinstance(value, list):
                return [references(item) for item in value]
            return value

        return references(schema), references(definitions)

    def contracts(self):
        contracts = {}
        for model in [
            EntryToolResult,
            *[item.arguments_model for item in self.definitions.values()],
            TaskPage,
            TaskView,
        ]:
            schema, definitions = self._schema(model)
            for name, definition in {**definitions, model.__name__: schema}.items():
                if name in contracts and contracts[name] != definition:
                    raise EntryConflict("ENTRY_SCHEMA_NAME_CONFLICT")
                contracts[name] = definition
        return contracts

    def catalog(self, permissions):
        return [
            {
                "name": item.name,
                "description": item.description,
                "arguments_schema": self._schema(item.arguments_model)[0],
                "result_schema": {"$ref": "#/tool_contracts/EntryToolResult"},
                "data_schema": {"$ref": f"#/tool_contracts/{item.data_model.__name__}"}
                if item.data_model
                else None,
                "effect": item.effect,
                "prerequisites": item.prerequisites,
                "cost_scope": "TASK"
                if item.effect == "DISPATCH_INVESTIGATION"
                else "LOCAL",
                "external_requests": None
                if item.effect == "DISPATCH_INVESTIGATION"
                else 0,
                "replayable": item.effect == "READ_LOCAL",
                "available": item.handler is not None
                and (item.effect != "WRITE_CONVERSATION" or permissions.logging_healthy)
                and (
                    item.effect != "DISPATCH_INVESTIGATION"
                    or self.delegation.unavailable_reason(item.name, permissions)
                    is None
                ),
                "unavailable_reason": "P26-3 调查委派尚未接入"
                if item.handler is None
                else "日志不可用"
                if item.effect == "WRITE_CONVERSATION"
                and not permissions.logging_healthy
                else self.delegation.unavailable_reason(item.name, permissions)
                if item.effect == "DISPATCH_INVESTIGATION"
                else None,
            }
            for item in self.definitions.values()
        ]

    def execute(self, *, tool, arguments, call_id, permissions):
        definition = self.definitions.get(tool)
        if definition is None:
            return EntryToolResult(
                call_id=call_id,
                tool=tool,
                status="REJECTED",
                observed_at=now(),
                limitations=["ENTRY_TOOL_UNREGISTERED"],
            )
        capability = next(
            item for item in self.catalog(permissions) if item["name"] == tool
        )
        if not capability["available"]:
            return EntryToolResult(
                call_id=call_id,
                tool=tool,
                status="UNAVAILABLE",
                observed_at=now(),
                limitations=[capability["unavailable_reason"]],
            )
        try:
            parsed = definition.arguments_model.model_validate(arguments)
        except ValidationError as exc:
            from credra_agent.observability.validation import schema_issues

            return EntryToolResult(
                call_id=call_id,
                tool=tool,
                status="REJECTED",
                observed_at=now(),
                data=schema_issues(exc, definition.arguments_model),
                limitations=["ENTRY_TOOL_ARGUMENTS_INVALID"],
            )
        with (
            service_session("entry_tools", config_from_settings(self.settings)),
            log_context(
                conversation_id=self.conversation_id, call_id=call_id, node="entry"
            ),
        ):
            emit("TOOL_START", tool=tool, status="STARTED")
            try:
                if definition.effect != "READ_LOCAL":
                    require_logging()
                if definition.effect == "DISPATCH_INVESTIGATION":
                    result = self.delegation.execute(tool, parsed, call_id=call_id)
                else:
                    result = self._execute_local(definition, parsed, call_id, tool)
            except EntryConflict as exc:
                result = EntryToolResult(
                    call_id=call_id,
                    tool=tool,
                    status="LIMITED"
                    if str(exc) == "ENTRY_TASK_BUDGET_EXHAUSTED"
                    else "REJECTED",
                    observed_at=now(),
                    limitations=[
                        str(exc)
                        if str(exc).startswith("ENTRY_")
                        else "ENTRY_TASK_OR_REFERENCE_NOT_VISIBLE"
                    ],
                )
            except (OSError, ValueError):
                result = EntryToolResult(
                    call_id=call_id,
                    tool=tool,
                    status="UNKNOWN",
                    observed_at=now(),
                    limitations=["ENTRY_LOCAL_STATE_UNREADABLE"],
                )
            emit(
                "TOOL_END",
                tool=tool,
                status=result.status
                if result.status
                in {"NO_RESULT", "REJECTED", "UNKNOWN", "ACCEPTED", "UNAVAILABLE"}
                else "SUCCESS",
            )
            return result

    def _execute_local(self, definition, parsed, call_id, tool):
        data = definition.handler(parsed)
        result = EntryToolResult(
            call_id=call_id,
            tool=tool,
            status="NO_RESULT"
            if tool in {"list_tasks", "list_cases"} and not data["items"]
            else "SUCCESS",
            data=data,
            observed_at=now(),
        )
        if tool == "list_tasks":
            result.fact_refs = [item["state_ref"] for item in data["items"]]
            result.limitations = data["limitations"]
            result.next_cursor = data["next_cursor"]
        elif tool == "list_cases":
            result.next_cursor = data["next_cursor"]
            result.limitations = data["limitations"]
        elif tool in {"get_task_status", "select_task"}:
            result.state_ref = data["summary"]["state_ref"]
            result.fact_refs = [result.state_ref]
        elif tool == "get_task_result":
            result.state_ref = data["state_ref"]
            result.fact_refs = [result.state_ref]
        return result

    def _list_tasks(self, args):
        return self.query.list(**args.model_dump()).model_dump(mode="json")

    def _get_task(self, args):
        return self.query.get(args.task_id).model_dump(mode="json")

    def _select_task(self, args):
        view = self.query.get(args.task_id)
        self.store.select(self.conversation_id, self.workspace_ref, args.task_id)
        return view.model_dump(mode="json")

    def _list_cases(self, args):
        scope = hashlib.sha256(
            json.dumps(
                [
                    self.workspace_ref,
                    sorted(self.query.allowed_subject_ids),
                    args.subject_hint,
                    args.year,
                ]
            ).encode()
        ).hexdigest()
        after = ""
        if args.cursor:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(args.cursor.encode()))
                if (
                    set(decoded) != {"scope", "after"}
                    or decoded["scope"] != scope
                    or not isinstance(decoded["after"], str)
                ):
                    raise ValueError
                after = decoded["after"]
            except (ValueError, TypeError, UnicodeError) as exc:
                raise EntryConflict("ENTRY_CASE_CURSOR_INVALID") from exc
        items = []
        more = False
        for record in load_subject_catalog(self.settings.data_dir):
            if record.case_id <= after:
                continue
            if record.subject_id not in self.query.allowed_subject_ids:
                continue
            if args.subject_hint and not any(
                args.subject_hint.lower() in value.lower() for value in record.aliases
            ):
                continue
            if args.year is not None and args.year not in record.available_years:
                continue
            if len(items) >= args.limit:
                more = True
                break
            checked = validate_case(record.case_id, self.settings.data_dir)
            items.append(
                {
                    **record.model_dump(mode="json"),
                    "preflight_valid": checked.valid,
                    "material_scope": "已导入 source 文件；财务 v2 与证据快照声明仍需启动时校验，证据快照须匹配 material_as_of；不代表已调查或外部渠道可用",
                }
            )
        next_cursor = (
            base64.urlsafe_b64encode(
                json.dumps({"after": items[-1]["case_id"], "scope": scope}).encode()
            ).decode()
            if more
            else None
        )
        return {
            "items": items,
            "scope": "IMPORTED_CASES",
            "limitations": [],
            "has_more": more,
            "next_cursor": next_cursor,
        }

    def _get_result(self, args):
        view = self.query.get(args.task_id)
        if (
            args.reference_id is not None
            and args.reference_id not in view.result_refs.values()
        ):
            raise EntryConflict("ENTRY_REFERENCE_NOT_VISIBLE")
        return {
            "task_id": args.task_id,
            "state_ref": view.summary.state_ref,
            "status": view.summary.status,
            "stop_reason": view.stop_reason,
            "references": {
                key: value
                for key, value in view.result_refs.items()
                if args.reference_id is None or value == args.reference_id
            },
            "limitations": ["当前接口仅返回状态及引用索引，不读取正文。"],
        }
