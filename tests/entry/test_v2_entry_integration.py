"""P26 integration: safe log diagnostics and independent concurrent conversations."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from credra_agent.observability.collector import (
    Collector,
    LogConfig,
    LoggingUnavailable,
    diagnostic_failure,
)
from credra_agent.observability.events import event_record
from credra_agent.observability.runtime import require_logging, service_session
from tests.entry.test_v2_entry_delegation import (
    Runtime,
    decision,
    draft,
)
from tests.entry.test_v2_entry_delegation import (
    settings as delegation_settings,
)


@pytest.fixture
def settings(tmp_path):
    return delegation_settings.__wrapped__(tmp_path)


def test_log_write_failure_exposes_only_safe_stage_and_system_code(
    tmp_path, monkeypatch, capsys
):
    collector = Collector(LogConfig(tmp_path))
    try:

        def fail(record):
            raise PermissionError(13, "secret-sentinel private-provider-payload")

        monkeypatch.setattr(collector.writer, "append", fail)
        with pytest.raises(LoggingUnavailable):
            collector.publish(event_record("NODE_START", service="test"))
        assert collector.failure_code == "WRITE_FAILURE"
    finally:
        collector.close()
    error = capsys.readouterr().err
    assert "stage=WRITE_FAILURE" in error and "errno=13" in error
    assert "secret-sentinel" not in error and "private-provider-payload" not in error


def test_unserializable_event_fails_receipt_without_killing_writer(
    tmp_path, monkeypatch
):
    collector = Collector(LogConfig(tmp_path))
    original = collector.writer.append
    try:

        def fail(record):
            raise TypeError("private-payload")

        monkeypatch.setattr(collector.writer, "append", fail)
        with pytest.raises(LoggingUnavailable):
            collector.publish(event_record("NODE_START", service="test"))
        assert collector.failure_code == "WRITE_FAILURE" and collector.worker.is_alive()
    finally:
        monkeypatch.setattr(collector.writer, "append", original)
        collector.close()


def test_diagnostic_stage_is_whitelisted(capsys):
    diagnostic_failure("secret-sentinel")
    assert "secret-sentinel" not in capsys.readouterr().err


def test_schema_compaction_preserves_title_field_and_constraints():
    from pydantic import BaseModel, Field

    from credra_agent.entry.registry import EntryToolRegistry

    class Document(BaseModel):
        title: str = Field(min_length=2, description="Actual field, not metadata")

    schema, _ = EntryToolRegistry._schema(Document)
    assert "title" not in schema
    assert schema["required"] == ["title"]
    assert schema["properties"]["title"]["minLength"] == 2
    assert schema["properties"]["title"]["description"] == "Actual field, not metadata"


@pytest.mark.parametrize(
    "reply,removed_unsupported_content",
    [("Oct 31 == Dec 25", False), ("已有999个已完成任务", True)],
)
def test_ordinary_digits_do_not_bypass_task_fact_guard(
    settings, reply, removed_unsupported_content
):
    from tests.entry.test_v2_entry_dialogue import Model

    result = Runtime(settings).run(
        Model(
            lambda ctx: {
                "decision": "reply",
                "content_kind": "conversation",
                "text": reply,
            }
        ),
        text="讲个笑话",
    )
    assert result.status == "REPLY"
    assert not result.tools and result.budget["external_spent"] == 1
    assert (
        "ENTRY_UNSUPPORTED_CONVERSATION_CONTENT_REMOVED" in result.limitations
    ) is removed_unsupported_content


def test_rejected_arguments_supply_safe_feedback_and_can_be_corrected(settings):
    from tests.entry.test_v2_entry_dialogue import Model

    runtime = Runtime(settings)

    def choose(context):
        if not context["turn_events"]:
            arguments = {
                "draft": draft(
                    case_id="case_byd_002594", private_unknown="secret-sentinel"
                )
            }
        else:
            rejected = context["turn_events"][-1]["payload"]
            assert rejected["status"] == "REJECTED"
            issues = rejected["data"]["validation_issues"]
            assert any(item["path"] == ["draft", "case_id"] for item in issues)
            assert "secret-sentinel" not in str(
                rejected
            ) and "private_unknown" not in str(rejected)
            arguments = {"draft": draft(), "case_id": "case_byd_002594"}
        return {
            "decision": "call_tool",
            "tool": "prepare_investigation",
            "arguments": arguments,
            "reason": "按 Schema 纠正参数",
        }

    result = runtime.run(Model(choose))
    assert result.status == "REPLY" and [item.status for item in result.tools] == [
        "REJECTED",
        "ACCEPTED",
    ]
    view = runtime.finish(result)
    assert view.task_spec.subject_id == "002594" and len(runtime.actions) == 1
    assert result.budget["external_spent"] == 2


def test_default_capacity_allows_status_feedback_after_task_creation(settings):
    from tests.entry.test_v2_entry_dialogue import Model

    settings = settings.model_copy(update={"analysis_llm_max_input_chars": 30000})
    runtime = Runtime(settings)
    created = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    runtime.finish(created)
    model = Model(
        lambda context: (
            {
                "decision": "call_tool",
                "tool": "get_task_status",
                "arguments": {"task_id": created.selected_task_id},
                "reason": "实际状态",
            }
            if not context["turn_events"]
            else {
                "decision": "reply",
                "content_kind": "task_facts",
                "text": "保存状态",
                "fact_refs": context["turn_events"][-1]["payload"]["fact_refs"],
            }
        )
    )
    result = runtime.run(model, message="status-default", text="现在是什么状态")
    assert result.status == "REPLY", result.limitations
    assert len(model.calls) == 2


def test_selection_cannot_authorize_a_new_investigation_in_same_turn(settings):
    from tests.entry.test_v2_entry_dialogue import Model

    runtime = Runtime(settings)
    created = runtime.run(
        decision("prepare_investigation", {"draft": draft()}),
        conversation="conversation-seed",
    )
    runtime.finish(created)
    calls = len(runtime.model.calls)
    seen = []

    def choose(context):
        if not context["turn_events"]:
            return {
                "decision": "call_tool",
                "tool": "select_task",
                "arguments": {"task_id": created.selected_task_id},
                "reason": "选择已有任务",
            }
        if len(context["turn_events"]) == 2:
            capability = next(
                item
                for item in context["tools"]
                if item["name"] == "prepare_investigation"
            )
            assert not capability["available"]
            return {
                "decision": "call_tool",
                "tool": "prepare_investigation",
                "arguments": {"draft": draft()},
                "reason": "模型错误提出新建",
            }
        seen.append(context["turn_events"][-1]["payload"])
        return {
            "decision": "reply",
            "content_kind": "task_facts",
            "text": "所选任务",
            "fact_refs": context["turn_events"][1]["payload"]["fact_refs"],
        }

    result = runtime.run(
        Model(choose),
        conversation="conversation-selection",
        message="select-existing",
        text="第二个",
    )
    assert [item.status for item in result.tools] == ["SUCCESS", "UNAVAILABLE"]
    assert result.selected_task_id == created.selected_task_id
    assert seen[0]["status"] == "UNAVAILABLE" and len(runtime.model.calls) == calls


def test_logging_failure_still_blocks_dispatch(tmp_path, monkeypatch):
    with service_session("test", LogConfig(tmp_path), inherited=False) as log:

        def fail(record):
            raise OSError("private-payload")

        monkeypatch.setattr(log.collector.writer, "append", fail)
        assert not log.emit("NODE_START")
        with pytest.raises(LoggingUnavailable):
            require_logging()


def test_two_conversations_do_not_share_selection_or_budget(settings):
    runtime = Runtime(settings)
    results = []
    for number, subject in enumerate(("比亚迪", "上汽")):
        result = runtime.run(
            decision("prepare_investigation", {"draft": draft(subject_hint=subject)}),
            conversation=f"conversation-separate-{number}",
            message=f"task-{number}",
            text=f"调查{subject}2025年的监管消息",
        )
        results.append((result, runtime.finish(result)))
    assert results[0][0].selected_task_id != results[1][0].selected_task_id
    assert results[0][1].task_spec.subject_id == "002594"
    assert results[1][1].task_spec.subject_id == "600104"
    assert all(result.budget["external_spent"] == 1 for result, view in results)


def test_concurrent_conversations_can_query_while_investigation_holds_task_lock(
    settings,
):
    from credra_agent.entry.service import execute_entry_message
    from credra_agent.execution.executor import (
        ActionExecutor,
        ExecutionOutcome,
        HandlerDefinition,
    )
    from tests.entry.test_v2_entry_delegation import ANCHOR
    from tests.entry.test_v2_entry_dialogue import Model

    entered, release = threading.Event(), threading.Event()
    runtime = Runtime(settings)

    def executor(settings, spec, model):
        def search(args):
            entered.set()
            assert release.wait(20)
            return ExecutionOutcome(
                status="NO_RESULT", summary="并发来源缺口", actual_external_requests=1
            )

        return ActionExecutor(
            {
                "search_evidence": HandlerDefinition(
                    search, external_request_reservation=1
                )
            }
        )

    runtime.executor = executor
    accepted = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    assert entered.wait(15)

    def query(number):
        model = Model(
            lambda ctx: (
                {
                    "decision": "call_tool",
                    "tool": "get_task_status",
                    "arguments": {"task_id": accepted.selected_task_id},
                    "reason": "读取实际状态",
                }
                if not ctx["turn_events"]
                else {
                    "decision": "reply",
                    "content_kind": "task_facts",
                    "text": "保存的任务状态",
                    "fact_refs": ctx["turn_events"][-1]["payload"]["fact_refs"],
                }
            )
        )
        return execute_entry_message(
            conversation_id=f"conversation-parallel-{number}",
            message_id=f"query-{number}",
            text="查看该任务的状态",
            as_of=ANCHOR,
            settings=settings,
            model_factory=lambda s, p: model,
            investigation_model_factory=lambda s, p: runtime.model,
            executor_factory=executor,
        )

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            answers = list(pool.map(query, range(3)))
        assert all(
            answer.status == "REPLY" and "RUNNING" in answer.text for answer in answers
        ), [(answer.status, answer.limitations) for answer in answers]
        assert all(answer.budget["external_spent"] == 2 for answer in answers)
    finally:
        release.set()
        runtime.finish(accepted)


def test_ambiguous_tasks_second_candidate_and_pronoun_use_saved_context(settings):
    from tests.entry.test_v2_entry_dialogue import Model

    runtime = Runtime(settings)
    for number in range(2):
        result = runtime.run(
            decision("prepare_investigation", {"draft": draft()}),
            conversation=f"conversation-seed-{number}",
            message=f"seed-{number}",
        )
        runtime.finish(result)
    task_calls = len(runtime.model.calls)
    candidates = []

    def choose(context):
        if context["query"] == "继续上次比亚迪调查":
            candidates.extend(item["task_id"] for item in context["task_page"]["items"])
            return {
                "decision": "ask_user",
                "question": "有两项比亚迪调查，请选择",
                "reason": "指代不唯一",
                "options": candidates,
            }
        if context["query"] == "第二个" and not context["turn_events"]:
            pending = context["pending_question"]
            assert pending["options"] == candidates and len(candidates) == 2
            return {
                "decision": "call_tool",
                "tool": "select_task",
                "arguments": {"task_id": pending["options"][1]},
                "reason": "使用原候选第二项",
            }
        if context["query"] == "继续它":
            selected = context["current_task"]["summary"]
            return {
                "decision": "call_tool",
                "tool": "resume_investigation",
                "arguments": {
                    "task_id": selected["task_id"],
                    "expected_state_ref": selected["state_ref"],
                },
                "reason": "继续已选原任务",
            }
        return {
            "decision": "reply",
            "content_kind": "task_facts",
            "text": "已选择原候选",
            "fact_refs": context["turn_events"][-1]["payload"]["fact_refs"],
        }

    model = Model(choose)
    asked = runtime.run(
        model,
        text="继续上次比亚迪调查",
        conversation="conversation-ref",
        message="ambiguous",
    )
    assert asked.status == "ASK_USER" and asked.selected_task_id is None
    selected = runtime.run(
        model, text="第二个", conversation="conversation-ref", message="second"
    )
    assert selected.selected_task_id == candidates[1]
    resumed = runtime.run(
        model, text="继续它", conversation="conversation-ref", message="pronoun"
    )
    assert (
        resumed.selected_task_id == candidates[1] and resumed.tools[-1].data["terminal"]
    )
    assert (
        len(runtime.model.calls) == task_calls and resumed.budget["external_spent"] == 4
    )
