"""P26-0/1 truthful local context, real Checkpoint versions, scope and input bounds."""

import json
import shutil
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import ValidationError

from app.config import Settings
from app.llm.gateway import OpenAICompatibleStructuredModel
from app.tools.artifacts import ArtifactStore
from credra_agent.entry.context import (
    ContextLimited,
    EntryContextBuilder,
    workspace_reference,
)
from credra_agent.entry.models import EntryAskUser, EntryDecision, EntryPermissions
from credra_agent.entry.query import TaskQueryService
from credra_agent.entry.registry import EntryToolRegistry
from credra_agent.entry.store import EntryConflict
from credra_agent.execution.executor import ActionExecutor, HandlerDefinition
from credra_agent.intent.service import interpret_message
from credra_agent.observability.events import event_record, log_context, reference_id
from credra_agent.observability.runtime import config_from_settings, service_session
from credra_agent.observability.text import render_event
from credra_agent.observability.wire import validate_event
from credra_agent.runtime.ui_store import task_lock

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = date(2026, 6, 30)


@pytest.fixture
def settings(tmp_path, monkeypatch):
    for case in ("case_byd_002594", "case_saic_600104"):
        shutil.copytree(
            ROOT / "data" / case / "source", tmp_path / "data" / case / "source"
        )

    def forbid(*args, **kwargs):
        raise AssertionError("P26-0/1 must never call a model")

    monkeypatch.setattr(OpenAICompatibleStructuredModel, "generate", forbid)
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        checkpoint_db_path=tmp_path / "entry.db",
        service_log_dir=tmp_path / "logs",
        trace_dir=tmp_path / "traces",
        model_api_key="private-config-sentinel",
        research_provider="mock",
    )


def checkpoint(
    settings,
    task,
    version,
    *,
    case="case_byd_002594",
    namespace="",
    status="RUNNING",
    extra=None,
):
    saved = empty_checkpoint()
    saved.update(
        id=f"{version:08d}",
        ts=f"2026-06-{version % 28 + 1:02d}T12:00:00+00:00",
        channel_values={
            "case_id": case,
            "status": status,
            "graph_version": "agentic_v2",
            **(extra or {}),
        },
    )
    with SqliteSaver.from_conn_string(str(settings.checkpoint_db_path)) as saver:
        saver.put(
            {"configurable": {"thread_id": task, "checkpoint_ns": namespace}},
            saved,
            {"source": "update", "step": version, "parents": {}},
            {},
        )


def draft(
    settings, task="draft", message="draft-msg", text="调查比亚迪2025年的监管消息"
):
    return interpret_message(
        thread_id=task,
        source_message_id=message,
        text=text,
        as_of=ANCHOR,
        data_dir=settings.data_dir,
        database_path=settings.checkpoint_db_path,
    )


def registry(
    settings,
    *,
    scan_batch=200,
    allowed=("002594", "600104"),
    conversation="conversation-offline",
):
    return EntryToolRegistry(
        settings=settings,
        query_service=TaskQueryService(
            settings, allowed_subject_ids=allowed, scan_batch=scan_batch
        ),
        conversation_id=conversation,
        workspace_ref=workspace_reference(settings),
    )


def permissions(**overrides):
    return EntryPermissions.model_validate(
        {
            "allowed_subject_ids": ["002594", "600104"],
            "logging_healthy": True,
            **overrides,
        }
    )


def execute(reg, tool, args=None, perms=None):
    return reg.execute(
        tool=tool,
        arguments=args or {},
        call_id=f"call-{tool}",
        permissions=perms or permissions(),
    )


def test_latest_checkpoints_deduplicate_and_drafts_merge(settings):
    reg = registry(settings, scan_batch=2)
    draft(settings, task="investigation")
    draft(
        settings,
        task="unstarted",
        message="unstarted-msg",
        text="比较去年和今年的现金流",
    )
    for version in range(1, 7):
        checkpoint(settings, "investigation", version)
    checkpoint(settings, "nested-only", 7, namespace="subgraph:child")
    checkpoint(
        settings, "investigation", 8, namespace="subgraph:child", status="FAKE_NESTED"
    )
    page = reg.query.list()
    assert not page.backfill_complete
    assert {item.task_id for item in page.items} == {"investigation", "unstarted"}
    assert (
        next(item for item in page.items if item.task_id == "investigation").status
        == "RUNNING"
    )
    for _ in range(8):
        page = reg.query.list()
        if page.backfill_complete:
            break
    assert page.backfill_complete and len(page.items) == 2
    pending = next(item for item in page.items if item.task_id == "unstarted")
    assert pending.kind == "DRAFT" and pending.checkpoint_at is None
    assert reg.query.get("investigation").summary.spec_version == 1


def test_new_head_overflow_resumes_incremental_backfill(settings):
    reg = registry(settings, scan_batch=2)
    checkpoint(settings, "old", 1)
    assert reg.query.list().backfill_complete
    for version in range(2, 9):
        checkpoint(settings, f"new-{version}", version)
    assert not reg.query.list().backfill_complete
    for _ in range(10):
        page = reg.query.list()
        if page.backfill_complete:
            break
    assert page.backfill_complete
    assert len(page.items) == 8


def test_hidden_and_unauthorized_tasks_and_references_are_rejected(settings):
    reg = registry(settings, allowed=("002594",))
    checkpoint(settings, "hidden", 1, case="case_normal")
    checkpoint(settings, "saic", 2, case="case_saic_600104")
    checkpoint(
        settings,
        "byd",
        3,
        extra={
            "report_ref": "artifacts/report_v1.json",
            "raw_prompt": "private-config-sentinel",
        },
    )
    assert [item.task_id for item in reg.query.list().items] == ["byd"]
    for task in ("hidden", "saic", "unknown"):
        assert execute(reg, "get_task_status", {"task_id": task}).status == "REJECTED"
    assert (
        execute(
            reg,
            "get_task_result",
            {"task_id": "byd", "reference_id": "artifacts/another_task_v1.json"},
        ).status
        == "REJECTED"
    )
    result = execute(
        reg,
        "get_task_result",
        {"task_id": "byd", "reference_id": "artifacts/report_v1.json"},
    )
    assert (
        result.status == "SUCCESS"
        and "private-config-sentinel" not in result.model_dump_json()
    )


def test_keyset_pagination_is_scoped_and_new_tasks_do_not_duplicate(settings):
    reg = registry(settings)
    for version in range(1, 5):
        checkpoint(settings, f"task-{version}", version)
    first = reg.query.list(limit=2)
    assert first.has_more and len(first.items) == 2
    checkpoint(settings, "newer", 10)
    second = reg.query.list(limit=2, cursor=first.next_cursor)
    assert not {item.task_id for item in first.items} & {
        item.task_id for item in second.items
    }
    with pytest.raises(EntryConflict, match="CURSOR"):
        reg.query.list(cursor=first.next_cursor, subject_id="002594")
    with pytest.raises(EntryConflict, match="CURSOR"):
        reg.query.list(cursor="not-a-cursor")
    with pytest.raises(EntryConflict, match="LIMIT"):
        reg.query.list(limit=51)
    injected = reg.query.list(status="RUNNING' OR 1=1 --")
    assert not injected.items


def test_read_tools_do_not_dispatch_or_wait_for_task_lock(settings):
    reg = registry(settings)
    checkpoint(settings, "busy-task", 1)
    with task_lock(settings.checkpoint_db_path, "busy-task"):
        result = execute(reg, "get_task_status", {"task_id": "busy-task"})
        assert result.status == "SUCCESS" and result.external_requests == 0
        assert execute(reg, "list_tasks").status == "SUCCESS"
    with sqlite3.connect(settings.checkpoint_db_path) as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='credra_agent_budget'"
        ).fetchall()


def test_real_handler_catalog_and_strict_unavailable_tools(settings):
    reg = registry(settings)
    capabilities = {item["name"]: item for item in reg.catalog(permissions())}
    assert len(capabilities) == 8
    assert capabilities["list_tasks"]["available"]
    assert (
        capabilities["list_tasks"]["arguments_schema"]["additionalProperties"] is False
    )
    assert capabilities["select_task"]["effect"] == "WRITE_CONVERSATION"
    for tool in (
        "prepare_investigation",
        "submit_clarification",
        "resume_investigation",
    ):
        assert not capabilities[tool]["available"]
        assert execute(reg, tool).status == "UNAVAILABLE"
    assert execute(reg, "search_evidence").status == "REJECTED"
    assert (
        execute(reg, "list_tasks", {"sql": "SELECT * FROM checkpoints"}).status
        == "REJECTED"
    )
    assert execute(reg, "list_tasks", {"limit": 51}).status == "REJECTED"
    assert not next(
        item
        for item in reg.catalog(permissions(logging_healthy=False))
        if item["name"] == "select_task"
    )["available"]


def test_selection_pending_question_and_messages_survive_reopen(settings):
    reg = registry(settings)
    checkpoint(settings, "existing", 1)
    assert execute(reg, "select_task", {"task_id": "existing"}).status == "SUCCESS"
    question = EntryAskUser(
        decision="ask_user",
        question="哪个年度？",
        reason="期间歧义",
        missing_fields=["year"],
    )
    reg.store.pending(reg.conversation_id, reg.workspace_ref, question)
    kwargs = {
        "conversation_id": reg.conversation_id,
        "workspace_ref": reg.workspace_ref,
        "message_id": "user-1",
        "turn_id": "turn-1",
        "role": "user",
        "payload": {"text": "继续它"},
    }
    assert reg.store.append(**kwargs)
    assert not reg.store.append(**kwargs)
    with pytest.raises(EntryConflict, match="ID_CONFLICT"):
        reg.store.append(**{**kwargs, "payload": {"text": "new"}})
    reopened = registry(settings)
    saved = reopened.store.conversation(reg.conversation_id, reg.workspace_ref)
    assert (
        saved["selected_task_id"] == "existing"
        and saved["pending_question"] == question
    )
    assert (
        reopened.store.history(reg.conversation_id, reg.workspace_ref)[0][0]["payload"][
            "text"
        ]
        == "继续它"
    )
    with pytest.raises(EntryConflict, match="NOT_VISIBLE"):
        reopened.store.ensure_conversation(reg.conversation_id, "different-workspace")


def test_query_context_includes_cases_tools_budget_and_no_fabricated_task(settings):
    reg = registry(settings)
    builder = EntryContextBuilder(reg)
    with service_session("offline_entry", config_from_settings(settings)):
        context, ref = builder.build(
            message_id="m1",
            query="现有任务有哪些",
            anchor_date=ANCHOR,
            permissions=permissions(
                entry_authorized=True,
                conversation_remaining_requests=5,
                task_remaining_requests=20,
            ),
        )
    assert (
        context.query == "现有任务有哪些"
        and context.current_task is None
        and context.selected_task_id is None
    )
    assert not context.task_page.items and len(context.imported_cases) == 2
    assert (
        len(context.tools) == 8
        and context.permissions.conversation_remaining_requests == 5
    )
    assert context.permissions.task_remaining_requests == 20
    assert "private-config-sentinel" not in context.model_dump_json()
    with sqlite3.connect(settings.checkpoint_db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM credra_intent_messages"
            ).fetchone()[0]
            == 0
        )
        saved = connection.execute(
            "SELECT payload_json FROM credra_entry_contexts WHERE context_id=?", (ref,)
        ).fetchone()[0]
    assert json.loads(saved)["query"] == context.query
    assert len(saved) <= 30000


def test_same_enterprise_cases_are_not_silently_deduplicated(settings):
    shutil.copytree(
        settings.data_dir / "case_byd_002594" / "source",
        settings.data_dir / "case_byd_deep" / "source",
    )
    reg = registry(settings)
    result = execute(reg, "list_cases", {"subject_hint": "比亚迪"})
    assert {item["case_id"] for item in result.data["items"]} == {
        "case_byd_002594",
        "case_byd_deep",
    }


def test_history_keeps_whole_turns_and_input_limit_preserves_constraints(settings):
    reg = registry(settings)
    for turn, roles in (
        ("first", ("user", "assistant", "tool")),
        ("second", ("user", "assistant", "tool")),
    ):
        for role in roles:
            reg.store.append(
                conversation_id=reg.conversation_id,
                workspace_ref=reg.workspace_ref,
                message_id=f"{turn}-{role}",
                turn_id=turn,
                role=role,
                payload={"text": "history" * 100},
            )
    history, limited = reg.store.history(
        reg.conversation_id, reg.workspace_ref, max_messages=4
    )
    assert limited and {item["turn_id"] for item in history} == {"second"}
    builder = EntryContextBuilder(reg, max_history_messages=4, max_input_chars=1000)
    with pytest.raises(ContextLimited):
        builder.build(
            message_id="oversize",
            query="仅限交易所公告，不使用转载媒体",
            anchor_date=ANCHOR,
            permissions=permissions(),
        )
    with sqlite3.connect(settings.checkpoint_db_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM credra_entry_contexts").fetchone()[
                0
            ]
            == 0
        )
    builder = EntryContextBuilder(reg, max_input_chars=100000)
    context, _ = builder.build(
        message_id="fits",
        query="仅限交易所公告，不使用转载媒体",
        anchor_date=ANCHOR,
        permissions=permissions(),
    )
    assert context.query == "仅限交易所公告，不使用转载媒体"


def test_executable_investigation_capabilities_and_projection_rebuild(settings):
    reg = registry(settings)
    checkpoint(settings, "existing", 1)
    execute(reg, "select_task", {"task_id": "existing"})
    context, _ = EntryContextBuilder(reg, max_input_chars=100000).build(
        message_id="m",
        query="能做什么",
        anchor_date=ANCHOR,
        permissions=permissions(),
        investigation_executor=ActionExecutor(
            {"search_evidence": HandlerDefinition(lambda args: None)}
        ),
    )
    capabilities = {item["name"]: item for item in context.investigation_capabilities}
    assert capabilities["search_evidence"]["available_in_selected_executor"]
    assert not capabilities["compute_metrics"]["available_in_selected_executor"]
    assert not capabilities["compare_peers"]["available_in_selected_executor"]
    assert reg.query.rebuild()
    assert reg.query.get("existing").summary.task_id == "existing"
    assert (
        reg.store.conversation(reg.conversation_id, reg.workspace_ref)[
            "selected_task_id"
        ]
        == "existing"
    )


@pytest.mark.parametrize(
    "decision",
    [
        {"decision": "reply", "text": "hello", "authorization": "APPROVED"},
        {
            "decision": "call_tool",
            "tool": "list_tasks",
            "arguments": {},
            "reason": "查询",
            "task_id": "invented",
        },
        {"decision": "unknown", "text": "hello"},
    ],
)
def test_entry_decision_discriminator_rejects_extra_or_unknown_fields(decision):
    with pytest.raises(ValidationError):
        EntryDecision.model_validate(decision)


def test_conversation_log_fields_are_hashed_and_text_visible():
    with log_context(
        conversation_id="private-conversation",
        message_id="private-message",
        context_id="private-context",
    ):
        event = event_record(
            "REQUEST_ACCEPTED", service="offline_entry", status="ACCEPTED"
        )
    validate_event(event)
    assert event["conversation_id"] == reference_id("private-conversation")
    assert "private-conversation" not in json.dumps(event)
    assert "会话引用" in render_event(event).decode("utf-8")


def test_case_pages_are_scoped_and_schema_references_are_complete(settings):
    reg = registry(settings)
    first = execute(reg, "list_cases", {"limit": 1})
    assert first.data["has_more"] and first.next_cursor
    second = execute(reg, "list_cases", {"limit": 1, "cursor": first.next_cursor})
    assert first.data["items"][0]["case_id"] != second.data["items"][0]["case_id"]
    assert (
        execute(
            reg, "list_cases", {"cursor": first.next_cursor, "subject_hint": "比亚迪"}
        ).status
        == "REJECTED"
    )
    context, _ = EntryContextBuilder(reg).build(
        message_id="schema-check",
        query="工具有哪些",
        anchor_date=ANCHOR,
        permissions=permissions(),
    )

    def check(value):
        if isinstance(value, dict):
            if "$ref" in value:
                assert value["$ref"].startswith("#/tool_contracts/")
                assert value["$ref"].split("/")[-1] in context.tool_contracts
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(context.tools)
    check(context.tool_contracts)


def test_current_graph_spec_beats_unapplied_newer_draft(settings):
    reg = registry(settings)
    old = draft(settings, task="actual-graph")
    ArtifactStore(
        settings.data_dir / old.task_spec.case_id / "runs" / "123456"
    ).write_json("artifacts/task_spec_v1.json", old.task_spec)
    checkpoint(
        settings,
        "actual-graph",
        1,
        extra={"run_id": "123456", "task_spec_ref": "artifacts/task_spec_v1.json"},
    )
    amended = draft(
        settings, task="actual-graph", message="amended", text="再核查2024年现金质量"
    )
    assert amended.task_spec.version == 2
    assert reg.query.get("actual-graph").task_spec.version == 1


def test_capacity_trimming_retains_selected_spec_and_denial(settings):
    reg = registry(settings)
    draft(
        settings,
        task="selected",
        text="调查比亚迪2025年的监管消息，仅限交易所公告，不使用媒体报道",
    )
    execute(reg, "select_task", {"task_id": "selected"})
    query = "继续核查；仅限交易所公告，不使用媒体报道"
    base, _ = EntryContextBuilder(reg).build(
        message_id="base", query=query, anchor_date=ANCHOR, permissions=permissions()
    )
    reg.store.append(
        conversation_id=reg.conversation_id,
        workspace_ref=reg.workspace_ref,
        message_id="large-history",
        turn_id="history-turn",
        role="user",
        payload={"text": "x" * 20000},
    )
    context, _ = EntryContextBuilder(
        reg, max_input_chars=len(base.model_dump_json()) + 1000
    ).build(
        message_id="trimmed", query=query, anchor_date=ANCHOR, permissions=permissions()
    )
    assert not context.history and context.query == query
    assert (
        context.current_task.task_spec.source_policy
        == base.current_task.task_spec.source_policy
    )
    assert (
        context.current_task.task_spec.questions
        == base.current_task.task_spec.questions
    )
    assert context.limitations
    with pytest.raises(EntryConflict, match="SCOPE_MISMATCH"):
        EntryContextBuilder(reg).build(
            message_id="bad-scope",
            query=query,
            anchor_date=ANCHOR,
            permissions=permissions(allowed_subject_ids=["600104"]),
        )


def test_checkpoint_error_is_a_safe_context_blocker(settings):
    reg = registry(settings)
    checkpoint(settings, "blocked", 1)
    with SqliteSaver.from_conn_string(str(settings.checkpoint_db_path)) as saver:
        current = saver.get_tuple({"configurable": {"thread_id": "blocked"}})
        saver.put_writes(
            current.config,
            [
                (
                    "__error__",
                    "LoggingUnavailable('LOGGING_UNAVAILABLE_BEFORE_NODE'): private-exception",
                )
            ],
            "node-error",
        )
    view = reg.query.get("blocked")
    assert view.execution_blocked == "LOGGING_UNAVAILABLE"
    assert "private-exception" not in view.model_dump_json()


def test_page_revalidates_visibility_and_status_before_return(settings, monkeypatch):
    reg = registry(settings)
    for version in range(1, 4):
        checkpoint(settings, f"done-{version}", version, status="COMPLETED")
    original = reg.query.get

    def changing(task_id):
        if task_id == "done-3":
            checkpoint(settings, task_id, 10, status="RUNNING")
        if task_id == "done-2":
            checkpoint(settings, task_id, 11, case="case_normal", status="COMPLETED")
        return original(task_id)

    monkeypatch.setattr(reg.query, "get", changing)
    page = reg.query.list(status="COMPLETED", limit=2)
    assert not page.items and page.has_more and page.next_cursor and page.limitations
    second = reg.query.list(status="COMPLETED", limit=2, cursor=page.next_cursor)
    assert [item.task_id for item in second.items] == ["done-1"]
