"""P26-2 actual dialogue orchestration with context-dependent offline decisions."""

import asyncio
import copy
import json
import shutil
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm.gateway import StructuredModelError, StructuredModelResult
from credra_agent.entry.budget import EntryBudgetStore
from credra_agent.entry.models import EntryDecision
from credra_agent.entry.service import execute_entry_message
from credra_agent.entry.store import EntryConflict
from credra_agent.intent.service import interpret_message

ROOT = Path(__file__).resolve().parents[2]
ANCHOR = date(2026, 6, 30)


def policy_data(**overrides):
    return {
        "policy_id": "offline-entry",
        "version": 1,
        "approval": "APPROVED",
        "allowed_subject_ids": ["002594", "600104"],
        "external_request_limit": 30,
        "token_limit": 100000,
        "active_seconds_limit": 60,
        "max_model_rounds": 4,
        "max_tool_calls": 3,
        "model_attempt_reservation": 2,
        "model_token_reservation": 5000,
        "max_output_tokens": 500,
        "max_input_chars": 50000,
        **overrides,
    }


@pytest.fixture
def settings(tmp_path):
    for case in ("case_byd_002594", "case_saic_600104"):
        shutil.copytree(
            ROOT / "data" / case / "source", tmp_path / "data" / case / "source"
        )
    path = tmp_path / "entry-policy.json"
    path.write_text(json.dumps(policy_data()), encoding="utf-8")
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        checkpoint_db_path=tmp_path / "entry.db",
        trace_dir=tmp_path / "traces",
        service_log_dir=tmp_path / "logs",
        agent_ui_execution_enabled=True,
        agent_entry_policy_path=path,
        analysis_llm_max_input_chars=50000,
        model_name="offline-entry-placeholder",
        model_api_key="private-test-key",
        research_provider="mock",
    )


def seed(settings, task="old-task", message="old-msg"):
    return interpret_message(
        thread_id=task,
        source_message_id=message,
        text="调查比亚迪2025年的监管消息",
        as_of=ANCHOR,
        data_dir=settings.data_dir,
        database_path=settings.checkpoint_db_path,
    )


class Model:
    model_name = "offline-dialogue"

    def __init__(self, decision=None, *, unknown=False, error=None):
        self.calls = []
        self.decision = decision
        self.unknown = unknown
        self.error = error

    def generate(self, **kwargs):
        self.calls.append(copy.deepcopy(kwargs["payload"]))
        if self.error:
            raise self.error
        payload = kwargs["payload"]
        if self.decision:
            decision = self.decision(payload)
        elif not payload["turn_events"]:
            assert (
                payload["query"]
                and payload["task_page"] is not None
                and payload["tools"]
                and payload["tool_contracts"]
            )
            decision = {
                "decision": "call_tool",
                "tool": "list_tasks",
                "arguments": {"limit": 20},
                "reason": "读取真实历史任务",
            }
        else:
            latest = payload["turn_events"][-1]["payload"]
            assert latest["tool"] == "list_tasks" and latest["fact_refs"]
            decision = {
                "decision": "reply",
                "content_kind": "task_facts",
                "text": "虚构999999个已完成任务",
                "fact_refs": latest["fact_refs"],
            }
        return StructuredModelResult(
            output=EntryDecision.model_validate(decision),
            model_name=self.model_name,
            attempts=1,
            latency_ms=1,
            input_tokens=None if self.unknown else 10,
            output_tokens=None if self.unknown else 20,
            external_requests=1,
            accounting_complete=not self.unknown,
        )


def run(
    settings,
    model,
    *,
    text="之前做过哪些调查",
    message="message-1",
    conversation="conversation-dialogue",
):
    return execute_entry_message(
        conversation_id=conversation,
        message_id=message,
        text=text,
        as_of=ANCHOR,
        settings=settings,
        model_factory=lambda s, p: model,
    )


def test_model_tool_model_gets_real_results_and_does_not_create_tasks(settings):
    seed(settings)
    model = Model()
    result = run(settings, model)
    assert (
        result.status == "REPLY"
        and "old-task" in result.text
        and "999999" not in result.text
    )
    assert len(model.calls) == 2 and len(result.tools) == 1
    assert model.calls[1]["turn_events"][-1]["role"] == "tool"
    assert (
        model.calls[1]["permissions"]["conversation_remaining_requests"]
        < model.calls[0]["permissions"]["conversation_remaining_requests"]
    )
    assert result.budget["external_spent"] == 2 and result.budget["token_spent"] == 60
    with sqlite3.connect(settings.checkpoint_db_path) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM credra_intent_messages"
            ).fetchone()[0]
            == 1
        )
        assert not connection.execute(
            "SELECT name FROM sqlite_master WHERE name='credra_agent_budget'"
        ).fetchall()
    logs = "\n".join(
        path.read_text(encoding="utf-8")
        for path in settings.service_log_dir.glob("*/service.*.log")
    )
    assert (
        "会话引用" in logs
        and "private-test-key" not in logs
        and "之前做过哪些调查" not in logs
    )


def test_same_message_replays_without_new_model_or_tool_calls(settings):
    seed(settings)
    model = Model()
    first = run(settings, model)
    again = run(settings, model)
    assert again.duplicate and again.text == first.text and len(model.calls) == 2
    assert again.budget == first.budget
    with pytest.raises(EntryConflict, match="ID_CONFLICT"):
        run(settings, model, text="不同内容")
    with pytest.raises(EntryConflict, match="ID_CONFLICT"):
        run(settings, model, conversation="conversation-other")


@pytest.mark.parametrize(
    "change",
    [{"approval": "UNCONFIRMED"}, {"approval": "APPROVED", "token_limit": None}],
)
def test_unapproved_or_missing_caps_do_not_call_models(settings, change):
    settings.agent_entry_policy_path.write_text(
        json.dumps(policy_data(**change)), encoding="utf-8"
    )
    model = Model()
    assert run(settings, model).status == "CONFIGURATION_REQUIRED" and not model.calls


def test_disabled_flag_and_missing_path_do_not_call_models(settings):
    model = Model()
    settings.agent_ui_execution_enabled = False
    assert run(settings, model).status == "CONFIGURATION_REQUIRED"
    settings.agent_ui_execution_enabled = True
    settings.agent_entry_policy_path = None
    assert run(settings, model).status == "CONFIGURATION_REQUIRED" and not model.calls


def test_limits_unknown_usage_and_retries_are_charged_conservatively(settings):
    settings.agent_entry_policy_path.write_text(
        json.dumps(policy_data(external_request_limit=2, token_limit=5000)),
        encoding="utf-8",
    )
    model = Model(unknown=True)
    result = run(settings, model)
    assert result.status == "LIMITED" and len(model.calls) == 1
    assert result.budget["token_spent"] == 5000 and result.budget["usage_uncertain"]
    failed = Model(
        error=StructuredModelError(
            "INVALID_OUTPUT", "private raw response", attempts=2, external_requests=2
        )
    )
    settings.agent_entry_policy_path.write_text(
        json.dumps(policy_data()), encoding="utf-8"
    )
    result = run(
        settings, failed, conversation="conversation-retry", message="retry-msg"
    )
    assert result.status == "LIMITED" and result.budget["external_spent"] == 2
    assert "private raw response" not in result.text


def test_frozen_policy_and_profile_do_not_expand_after_configuration_change(settings):
    model = Model(lambda p: {"decision": "reply", "text": "你好"})
    first = run(settings, model)
    settings.agent_entry_policy_path.write_text(
        json.dumps(policy_data(external_request_limit=200)), encoding="utf-8"
    )
    second = run(settings, model, message="message-2", text="你好")
    assert second.budget["external_limit"] == first.budget["external_limit"] == 30
    settings.model_api_key = "rotated-private-key"
    assert run(settings, model, message="message-3", text="你好").status == "REPLY"
    settings.intent_model = "different-model"
    assert run(settings, model, message="message-4").status == "CONFIGURATION_REQUIRED"
    assert len(model.calls) == 3


def test_invalid_and_unavailable_tool_results_are_fed_back_to_model(settings):
    def decisions(p):
        if not p["turn_events"]:
            return {
                "decision": "call_tool",
                "tool": "prepare_investigation",
                "arguments": {},
                "reason": "尝试委派",
            }
        assert p["turn_events"][-1]["payload"]["status"] == "UNAVAILABLE"
        return {
            "decision": "reply",
            "content_kind": "capabilities",
            "text": "已实现所有调查能力",
        }

    model = Model(decisions)
    result = run(settings, model)
    assert result.status == "REPLY" and len(model.calls) == 2
    assert "会话策略未允许" in result.text and "已实现所有调查能力" not in result.text


def test_model_and_tool_round_limits_stop_loop(settings):
    settings.agent_entry_policy_path.write_text(
        json.dumps(policy_data(max_model_rounds=2, max_tool_calls=1)), encoding="utf-8"
    )
    model = Model(
        lambda p: {
            "decision": "call_tool",
            "tool": "list_tasks",
            "arguments": {},
            "reason": "重复取数",
        }
    )
    result = run(settings, model)
    assert (
        result.status == "LIMITED" and len(model.calls) == 2 and len(result.tools) == 1
    )


def test_empty_filtered_tool_result_does_not_render_unrelated_context_tasks(settings):
    seed(settings)

    def decisions(p):
        if not p["turn_events"]:
            return {
                "decision": "call_tool",
                "tool": "list_tasks",
                "arguments": {"subject_id": "600104"},
                "reason": "查询上汽",
            }
        latest = p["turn_events"][-1]["payload"]
        return {
            "decision": "reply",
            "content_kind": "task_facts",
            "text": "所有历史为空",
            "fact_refs": latest["fact_refs"],
        }

    result = run(settings, Model(decisions))
    assert "old-task" not in result.text and "本次结果未返回任务记录" in result.text
    assert "所有历史为空" not in result.text


def test_unsupported_refs_and_unreferenced_claims_fail_closed(settings):
    seed(settings)
    model = Model(
        lambda p: {
            "decision": "reply",
            "content_kind": "task_facts",
            "text": "done",
            "fact_refs": ["invented-reference"],
        }
    )
    assert run(settings, model).status == "LIMITED"
    model = Model(lambda p: {"decision": "reply", "text": "已完成99个任务"})
    assert run(settings, model, message="unsupported-2").status == "LIMITED"


def test_smalltalk_with_unsupported_workspace_claim_returns_safe_reply(settings):
    model = Model(
        lambda p: {
            "decision": "reply",
            "content_kind": "conversation",
            "text": "你好！当前工作区已有比亚迪调查和报告。",
            "fact_refs": [],
        }
    )
    result = run(settings, model, text="你好", message="safe-smalltalk")
    assert result.status == "REPLY" and result.text == (
        "你好！我是 Credra Agent。你可以直接告诉我想讨论的问题。"
    )
    assert result.limitations == ["ENTRY_UNSUPPORTED_CONVERSATION_CONTENT_REMOVED"]
    assert len(model.calls) == 1 and result.budget["external_spent"] == 1


def test_model_result_saved_before_settlement_is_replayed(settings, monkeypatch):
    model = Model(lambda p: {"decision": "reply", "text": "你好"})
    original = EntryBudgetStore.settle

    def stop(self, *args):
        raise SystemExit("after saved result")

    monkeypatch.setattr(EntryBudgetStore, "settle", stop)
    with pytest.raises(SystemExit):
        run(settings, model)
    monkeypatch.setattr(EntryBudgetStore, "settle", original)
    result = run(settings, model)
    assert (
        result.status == "REPLY"
        and len(model.calls) == 1
        and result.budget["external_spent"] == 1
    )


def test_dispatched_without_result_does_not_resend_or_release_budget(
    settings, monkeypatch
):
    model = Model(lambda p: {"decision": "reply", "text": "你好"})

    def stop(*args):
        raise SystemExit("after network before local result")

    monkeypatch.setattr(EntryBudgetStore, "save_result", stop)
    with pytest.raises(SystemExit):
        run(settings, model)
    result = run(settings, model)
    assert result.status == "LIMITED" and len(model.calls) == 1
    assert result.budget["external_spent"] == 2 and result.budget["token_spent"] == 5000
    assert (
        run(settings, model, message="new-msg").status == "LIMITED"
        and len(model.calls) == 1
    )


def test_complete_input_limit_stops_before_model_dispatch(settings):
    settings.agent_entry_policy_path.write_text(
        json.dumps(policy_data(max_input_chars=1000)), encoding="utf-8"
    )
    model = Model()
    result = run(settings, model)
    assert (
        result.status == "LIMITED"
        and not model.calls
        and result.budget["external_spent"] == 0
    )


def test_conversation_lock_rejects_concurrent_entry_but_not_other_conversations(
    settings,
):
    from credra_agent.runtime.ui_store import task_lock

    model = Model(lambda p: {"decision": "reply", "text": "你好"})
    with task_lock(settings.checkpoint_db_path, "conversation-dialogue"):
        assert run(settings, model).status == "LIMITED" and not model.calls
        assert (
            run(settings, model, conversation="conversation-independent").status
            == "REPLY"
        )


def test_actual_chainlit_on_message_uses_llm_without_creating_intent_task(
    settings, monkeypatch
):
    import app.chainlit_app as ui
    from credra_agent.entry import service

    model = Model()
    session = {}
    sent = []

    class Message:
        def __init__(self, content="", **kwargs):
            self.content = content

        async def send(self):
            sent.append(self.content)
            return self

        async def update(self):
            sent.append(self.content)

    monkeypatch.setattr(ui, "get_settings", lambda: settings)
    monkeypatch.setattr(ui.cl, "Message", Message)
    monkeypatch.setattr(
        ui.cl, "user_session", SimpleNamespace(get=session.get, set=session.__setitem__)
    )
    monkeypatch.setattr(service, "build_entry_model", lambda s, p: model)
    asyncio.run(
        ui.on_message(SimpleNamespace(id="on-message", content="现有任务有哪些"))
    )
    assert (
        len(model.calls) == 2
        and "entry_conversation_id" in session
        and "intent_thread_id" not in session
    )
    assert any("本次结果未返回任务记录" in text for text in sent)
    assert any("会话预算" in text for text in sent)


def test_real_gateway_json_retry_is_charged_once_with_all_attempts(settings):
    from app.llm.gateway import OpenAICompatibleStructuredModel
    from tests.analysis.test_m4a_analysis import _FakeClient

    client = _FakeClient(["invalid JSON", '{"decision":"reply","text":"你好"}'])
    model = OpenAICompatibleStructuredModel(
        api_key="offline-placeholder",
        base_url="https://offline.test/v1",
        model_name="offline-entry",
        timeout_seconds=1,
        max_attempts=2,
        max_input_chars=50000,
        max_output_tokens=500,
        enable_thinking=False,
        aggregate_accounting=True,
        client=client,
    )
    result = run(settings, model)
    assert result.status == "REPLY" and result.budget["external_spent"] == 2
    assert result.budget["token_spent"] == 336
    assert len(client.completions.calls) == 2
    assert all(
        c["extra_body"] == {"enable_thinking": False} for c in client.completions.calls
    )
    assert all(
        c["response_format"] == {"type": "json_object"}
        for c in client.completions.calls
    )
    assert run(settings, model).duplicate and len(client.completions.calls) == 2


def test_logging_pause_after_model_result_replays_without_repayment(
    settings, monkeypatch
):
    from credra_agent.entry import service
    from credra_agent.observability.collector import LoggingUnavailable

    original = service.require_logging
    calls = 0

    def gate():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise LoggingUnavailable("offline logging outage")
        original()

    monkeypatch.setattr(service, "require_logging", gate)
    model = Model()
    paused = run(settings, model)
    assert paused.status == "PAUSED_LOGGING" and len(model.calls) == 1
    assert paused.budget["external_spent"] == 1 and not paused.tools
    monkeypatch.setattr(service, "require_logging", original)
    resumed = run(settings, model)
    assert resumed.status == "REPLY" and len(model.calls) == 2
    assert resumed.budget["external_spent"] == 2 and len(resumed.tools) == 1


def test_invalid_usage_does_not_refund_and_saved_results_are_immutable(
    settings, monkeypatch
):
    original = EntryBudgetStore.save_result

    def corrupt(self, conversation_id, operation_id, result):
        original(
            self,
            conversation_id,
            operation_id,
            {**result, "external": -1, "tokens": True},
        )
        with pytest.raises(EntryConflict):
            original(self, conversation_id, operation_id, result)

    monkeypatch.setattr(EntryBudgetStore, "save_result", corrupt)
    result = run(settings, Model(lambda p: {"decision": "reply", "text": "你好"}))
    assert result.status == "REPLY" and result.budget["usage_uncertain"]
    assert result.budget["external_spent"] == 2 and result.budget["token_spent"] == 5000


def test_default_capacity_includes_prompt_schema_and_tool_feedback(settings):
    from credra_agent.entry.models import EntryContext
    from credra_agent.entry.service import input_size

    settings.agent_entry_policy_path.write_text(
        json.dumps(policy_data(max_input_chars=30000)), encoding="utf-8"
    )
    settings.analysis_llm_max_input_chars = 30000
    seed(settings)
    model = Model()
    assert run(settings, model).status == "REPLY"
    assert all(input_size(EntryContext.model_validate(p)) <= 30000 for p in model.calls)


def test_elapsed_budget_blocks_new_tool_and_model_actions(settings, monkeypatch):
    original = EntryBudgetStore.save_result

    def elapsed(self, conversation_id, operation_id, result):
        original(self, conversation_id, operation_id, {**result, "active_seconds": 61})

    monkeypatch.setattr(EntryBudgetStore, "save_result", elapsed)
    model = Model()
    result = run(settings, model)
    assert result.status == "LIMITED" and not result.tools and len(model.calls) == 1
    assert result.budget["active_seconds"] == 61
    assert (
        run(settings, model, message="time-next").status == "LIMITED"
        and len(model.calls) == 1
    )


@pytest.mark.parametrize("reference_index", [0, -1])
def test_task_result_reference_renders_actual_record_once(settings, reference_index):
    seed(settings)

    def decisions(payload):
        if not payload["turn_events"]:
            return {
                "decision": "call_tool",
                "tool": "get_task_result",
                "arguments": {"task_id": "old-task"},
                "reason": "读取保存结果",
            }
        returned = payload["turn_events"][-1]["payload"]
        return {
            "decision": "reply",
            "content_kind": "task_facts",
            "text": "已经完成报告",
            "fact_refs": [returned["fact_refs"][reference_index]],
        }

    result = run(settings, Model(decisions))
    assert result.status == "REPLY" and "old-task" in result.text
    assert result.text.count("保存引用") == 1
    assert "本次结果未返回任务记录" not in result.text
    assert "已经完成报告" not in result.text


def test_invalid_individual_token_usage_remains_reserved(settings):
    class InvalidUsageModel(Model):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            return replace(result, input_tokens=-1, output_tokens=100)

    model = InvalidUsageModel(lambda p: {"decision": "reply", "text": "你好"})
    result = run(settings, model)
    assert result.status == "REPLY" and result.budget["external_spent"] == 1
    assert result.budget["token_spent"] == 5000 and result.budget["usage_uncertain"]
