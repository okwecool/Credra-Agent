"""P26-3 model decisions reach real Graph/ledger/handlers without a second parse."""

import asyncio
import json
import shutil
import threading
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from credra_agent.entry.budget import EntryBudgetStore
from credra_agent.entry.context import workspace_reference
from credra_agent.entry.delegation import InvestigationDelegation, wait_for_command
from credra_agent.entry.query import TaskQueryService
from credra_agent.entry.service import execute_entry_message
from credra_agent.entry.store import EntryStore
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.intent.store import IntentStore
from credra_agent.observability.collector import LoggingUnavailable
from credra_agent.observability.runtime import config_from_settings, service_session
from credra_agent.planning.models import DecisionDraft
from credra_agent.runtime.ui_store import UITaskStore
from tests.test_v2_entry_dialogue import Model, policy_data
from tests.test_v2_ui_execution import Models, policy_dict

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = date(2026, 6, 30)
CONTROLS = ["prepare_investigation", "submit_clarification", "resume_investigation"]


@pytest.fixture
def settings(tmp_path):
    for case in ("case_byd_002594", "case_saic_600104"):
        shutil.copytree(
            ROOT / "data" / case / "source", tmp_path / "data" / case / "source"
        )
    entry = tmp_path / "entry.json"
    entry.write_text(
        json.dumps(policy_data(allowed_control_tools=CONTROLS)), encoding="utf-8"
    )
    task = tmp_path / "task.json"
    task.write_text(json.dumps(policy_dict()), encoding="utf-8")
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        checkpoint_db_path=tmp_path / "tasks.db",
        trace_dir=tmp_path / "traces",
        service_log_dir=tmp_path / "logs",
        agent_ui_execution_enabled=True,
        agent_entry_policy_path=entry,
        agent_ui_policy_path=task,
        analysis_mode="llm",
        intent_mode="llm",
        model_name="offline-model",
        model_api_key="offline-secret-placeholder",
        research_provider="mock",
        content_fetch_provider="disabled",
        analysis_llm_max_input_chars=50000,
    )


def decision(tool, arguments):
    def choose(context):
        assert context["query"] and context["tools"] and context["permissions"]
        if not context["turn_events"]:
            return {
                "decision": "call_tool",
                "tool": tool,
                "arguments": arguments,
                "reason": "响应明确调查要求",
            }
        assert context["turn_events"][-1]["role"] == "tool"
        return {
            "decision": "reply",
            "content_kind": "capabilities",
            "text": "说明实际可用能力",
        }

    return Model(choose)


def draft(**overrides):
    return {
        "operation": "start",
        "subject_hint": "比亚迪",
        "years": [2025],
        "focus": ["regulatory"],
        "allowed_sources": ["exchange_disclosure"],
        **overrides,
    }


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        self.model = Models()
        self.actions = []

    def executor(self, settings, spec, model):
        def search(arguments):
            self.actions.append(arguments)
            return ExecutionOutcome(
                status="NO_RESULT",
                summary="离线原始来源缺口",
                actual_external_requests=1,
            )

        return ActionExecutor(
            {
                "search_evidence": HandlerDefinition(
                    search, external_request_reservation=1
                )
            }
        )

    def run(
        self,
        model,
        *,
        message="message-1",
        text="调查比亚迪2025年的监管消息，仅限交易所公告",
        conversation="conversation-controls",
    ):
        return execute_entry_message(
            conversation_id=conversation,
            message_id=message,
            text=text,
            as_of=ANCHOR,
            settings=self.settings,
            model_factory=lambda s, p: model,
            investigation_model_factory=lambda s, p: self.model,
            executor_factory=self.executor,
        )

    def finish(self, result):
        command = result.tools[-1].data["command"]["command_id"]
        row = wait_for_command(self.settings.checkpoint_db_path, command)
        assert row["status"] == "ACKED", row
        return TaskQueryService(
            self.settings, allowed_subject_ids=["002594", "600104"]
        ).get(row["task_id"])


def test_actual_runtime_and_tool_keep_entry_and_task_budgets_separate(settings):
    runtime = Runtime(settings)
    model = decision("prepare_investigation", {"draft": draft()})
    accepted = runtime.run(model)
    assert accepted.status == "REPLY" and accepted.tools[-1].status == "ACCEPTED"
    assert "调查命令已接受" in accepted.text and len(model.calls) == 1
    view = runtime.finish(accepted)
    assert (
        view.summary.graph_version == "agentic_v2" and view.summary.status == "LIMITED"
    )
    assert (
        accepted.budget["external_spent"] == 1 and accepted.budget["token_spent"] == 30
    )
    assert (
        view.budget["scope"] == "TASK"
        and view.budget["external_spent"] == 3
        and view.budget["token_spent"] == 60
    )
    assert len(runtime.model.calls) == 2 and "intent_parse" not in runtime.model.calls
    assert len(runtime.actions) == 1 and runtime.actions[0].subject_id == "002594"
    assert runtime.actions[0].source_policy.allowed == ["exchange_disclosure"]
    assert (
        view.task_spec.questions[0].text == "调查比亚迪2025年的监管消息，仅限交易所公告"
    )
    assert view.command["budget_version"] == "entry_separate_v1"
    assert (
        len(
            IntentStore(settings.checkpoint_db_path).export_thread(view.summary.task_id)
        )
        == 1
    )
    again = runtime.run(model)
    assert again.duplicate and len(model.calls) == 1 and len(runtime.actions) == 1
    logs = "\n".join(
        p.read_text(encoding="utf-8")
        for p in settings.service_log_dir.glob("*/service.*.log")
    )
    assert "offline-secret-placeholder" not in logs and "仅限交易所公告" not in logs


def test_entry_llm_draft_reaches_real_financial_report_without_intent_reparse(settings):
    from credra_agent.financial.models import FinancialCitation
    from credra_agent.runtime.executors import build_agentic_executor
    from tests.test_v2_coordinator import finish_decision

    settings = settings.model_copy(update={"analysis_llm_max_input_chars": 30000})
    case = "case_byd_cash_quality_v2"
    shutil.copytree(
        ROOT / "data" / case / "source", settings.data_dir / case / "source"
    )
    runtime = Runtime(settings)

    class ReportModel(Models):
        def generate(self, **kwargs):
            task = kwargs["payload"]["task_spec"]
            assert [p["start"] for p in task["periods"]] == ["2025-01-01"]
            assert [p["start"] for p in task["comparison_periods"]] == ["2024-01-01"]
            if not kwargs["payload"]["observation_index"]:
                output = DecisionDraft(
                    decision="ACTION",
                    tool="compute_metrics",
                    arguments={
                        "metric_ids": ["cash_profit_ratio"],
                        "input_refs": ["artifacts/agent_financial_input_v1.json"],
                        "accounting_basis": "CONSOLIDATED",
                    },
                    expected_observation="计算合并现金利润比",
                    reason_summary="读取冻结输入",
                )
            else:
                output = finish_decision("NEEDS_REVIEW").model_copy(
                    update={
                        "financial_citations": [
                            FinancialCitation(
                                question_id=task["questions"][0]["question_id"],
                                result_ref="artifacts/agent_tool_result_v1.json",
                                result_index=0,
                            )
                        ]
                    }
                )
            self.decisions.append(output)
            return super().generate(**kwargs)

    runtime.model = ReportModel()
    runtime.executor = lambda settings, spec, model: build_agentic_executor(spec)
    model = decision(
        "prepare_investigation",
        {
            "case_id": case,
            "draft": draft(
                years=[2024, 2025, 2026],
                comparison_years=[2024],
                focus=["cash_quality"],
                allowed_sources=None,
                as_of="2026-04-02",
            ),
        },
    )
    accepted = runtime.run(
        model, text="调查比亚迪2025年现金质量，以2024年作比较，资料截止2026-04-02"
    )
    view = runtime.finish(accepted)
    assert view.result_refs["report_ref"] == "artifacts/agent_report_v2.md"
    assert runtime.model.calls == ["coordinate_investigation"] * 2
    assert len(model.calls) == 1 and accepted.budget["external_spent"] == 1
    assert view.budget["external_spent"] == 2
    contracts = json.dumps(model.calls[0]["tool_contracts"])
    assert "comparison_years" in contracts and "periods" in contracts
    followup = Model(
        lambda context: (
            {
                "decision": "call_tool",
                "tool": "get_task_status",
                "arguments": {"task_id": view.summary.task_id},
                "reason": "读取实际状态",
            }
            if not context["turn_events"]
            else {
                "decision": "reply",
                "content_kind": "task_facts",
                "text": "实际状态",
                "fact_refs": context["turn_events"][-1]["payload"]["fact_refs"],
            }
        )
    )
    status = runtime.run(
        followup, message="financial-status-default", text="现在是什么状态"
    )
    assert status.status == "REPLY" and len(followup.calls) == 2
    assert followup.calls[1]["current_task"] is None
    assert (
        followup.calls[1]["turn_events"][-1]["payload"]["data"]["summary"]["task_id"]
        == view.summary.task_id
    )


def test_missing_subject_clarifies_same_draft_and_authorization(settings):
    runtime = Runtime(settings)
    first = runtime.run(
        decision(
            "prepare_investigation", {"draft": draft(subject_hint=None, years=[])}
        ),
        text="帮我调查监管消息",
    )
    assert first.tools[-1].status == "WAITING_CLARIFICATION" and not runtime.model.calls
    task_id = first.selected_task_id
    original = UITaskStore(settings.checkpoint_db_path).policy(task_id)[1]
    second = runtime.run(
        decision(
            "submit_clarification",
            {
                "task_id": task_id,
                "expected_spec_version": 1,
                "draft": draft(operation="amend", allowed_sources=None),
            },
        ),
        message="message-2",
        text="比亚迪，2025年度",
    )
    view = runtime.finish(second)
    assert view.summary.task_id == task_id and view.summary.spec_version == 2
    assert view.task_spec.source_policy.allowed == ["exchange_disclosure"]
    assert (
        UITaskStore(settings.checkpoint_db_path).policy(task_id)[1].authorization_id
        == original.authorization_id
    )
    assert second.budget["external_spent"] == 2 and view.budget["external_spent"] == 3


@pytest.mark.parametrize(
    "change", [{"allowed_control_tools": []}, {"task_unapproved": True}]
)
def test_control_permissions_and_task_policy_block_without_task_or_paid_investigation(
    settings, change
):
    if change.get("task_unapproved"):
        settings.agent_ui_policy_path.write_text(
            json.dumps(policy_dict(approval="UNCONFIRMED")), encoding="utf-8"
        )
    else:
        settings.agent_entry_policy_path.write_text(
            json.dumps(policy_data(**change)), encoding="utf-8"
        )
    runtime = Runtime(settings)
    result = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    assert (
        result.tools[-1].status == "UNAVAILABLE"
        and not runtime.model.calls
        and not runtime.actions
    )
    assert (
        not TaskQueryService(settings, allowed_subject_ids=["002594", "600104"])
        .list()
        .items
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"draft": draft(), "case_id": "case_saic_600104"},
        {"draft": draft(subject_hint="上汽"), "case_id": "case_byd_002594"},
        {"draft": draft(operation="resume")},
    ],
)
def test_invalid_case_and_operation_never_reach_runtime(settings, arguments):
    runtime = Runtime(settings)
    result = runtime.run(decision("prepare_investigation", arguments))
    assert (
        result.tools[-1].status == "REJECTED"
        and not runtime.actions
        and not runtime.model.calls
    )
    assert (
        not TaskQueryService(settings, allowed_subject_ids=["002594", "600104"])
        .list()
        .items
    )


def test_clarification_cannot_widen_sources_even_before_subject_is_resolved(settings):
    runtime = Runtime(settings)
    first = runtime.run(
        decision("prepare_investigation", {"draft": draft(subject_hint=None)}),
        text="调查监管，仅限交易所公告",
    )
    task_id = first.selected_task_id
    result = runtime.run(
        decision(
            "submit_clarification",
            {
                "task_id": task_id,
                "expected_spec_version": 1,
                "draft": draft(
                    operation="amend", allowed_sources=["exchange_disclosure", "media"]
                ),
            },
        ),
        message="widen",
    )
    assert result.tools[-1].status == "REJECTED" and not runtime.actions
    assert (
        IntentStore(settings.checkpoint_db_path).latest_task_spec(task_id).version == 1
    )


def test_stale_clarification_version_is_rejected(settings):
    runtime = Runtime(settings)
    first = runtime.run(
        decision("prepare_investigation", {"draft": draft(subject_hint=None)}),
        text="调查监管",
    )
    result = runtime.run(
        decision(
            "submit_clarification",
            {
                "task_id": first.selected_task_id,
                "expected_spec_version": 9,
                "draft": draft(operation="amend"),
            },
        ),
        message="stale",
    )
    assert result.tools[-1].status == "REJECTED" and not runtime.actions


def test_task_budget_exhaustion_does_not_block_ordinary_conversation(settings):
    settings.agent_ui_policy_path.write_text(
        json.dumps(policy_dict(token_limit=100)), encoding="utf-8"
    )
    runtime = Runtime(settings)
    failed = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    assert (
        failed.tools[-1].status == "LIMITED"
        and not runtime.model.calls
        and not runtime.actions
    )
    reply = runtime.run(
        Model(lambda p: {"decision": "reply", "text": "你好"}),
        message="ordinary",
        text="你好",
    )
    assert reply.status == "REPLY" and reply.budget["external_spent"] == 3


def test_terminal_resume_returns_saved_state_without_new_investigation_calls(settings):
    runtime = Runtime(settings)
    first = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    view = runtime.finish(first)
    before = len(runtime.model.calls)
    result = runtime.run(
        decision(
            "resume_investigation",
            {
                "task_id": view.summary.task_id,
                "expected_state_ref": view.summary.state_ref,
            },
        ),
        message="resume",
    )
    assert result.tools[-1].status == "SUCCESS" and "原保存状态" in result.text
    assert len(runtime.model.calls) == before and len(runtime.actions) == 1


def test_queue_is_durable_before_worker_schedule_and_recovered_without_entry_charge(
    settings, monkeypatch
):
    runtime = Runtime(settings)
    original = InvestigationDelegation.schedule
    monkeypatch.setattr(InvestigationDelegation, "schedule", lambda *a: None)
    model = decision("prepare_investigation", {"draft": draft()})
    accepted = runtime.run(model)
    row = EntryStore(settings.checkpoint_db_path).command(
        accepted.tools[-1].data["command"]["command_id"]
    )
    assert row["status"] == "QUEUED" and not runtime.model.calls
    monkeypatch.setattr(InvestigationDelegation, "schedule", original)
    controller = InvestigationDelegation(
        settings=settings,
        query=TaskQueryService(settings, allowed_subject_ids=[]),
        model_factory=lambda s, p: runtime.model,
        executor_factory=runtime.executor,
    )
    controller.recover()
    view = runtime.finish(accepted)
    assert view.budget["external_spent"] == 3 and len(model.calls) == 1


def test_running_graph_holds_task_lock_but_dialogue_reads_actual_state(settings):
    runtime = Runtime(settings)
    arrived, release = threading.Event(), threading.Event()

    def executor(s, spec, model):

        def search(arguments):
            runtime.actions.append(arguments)
            arrived.set()
            if not release.wait(20):
                raise TimeoutError("offline test gate")
            return ExecutionOutcome(
                status="NO_RESULT", summary="已保存来源缺口", actual_external_requests=1
            )

        return ActionExecutor(
            {
                "search_evidence": HandlerDefinition(
                    search, external_request_reservation=1
                )
            }
        )

    runtime.executor = executor
    with service_session(
        "offline-process", config_from_settings(settings), inherited=False
    ):
        first = runtime.run(decision("prepare_investigation", {"draft": draft()}))
        try:
            assert arrived.wait(10)
            task_id = first.selected_task_id

            def query(p):
                if not p["turn_events"]:
                    assert (
                        p["selected_task_id"] == task_id
                        and p["permissions"]["task_policy_ref"]
                    )
                    return {
                        "decision": "call_tool",
                        "tool": "get_task_status",
                        "arguments": {"task_id": task_id},
                        "reason": "读取运行状态",
                    }
                actual = p["turn_events"][-1]["payload"]
                assert actual["data"]["summary"]["status"] == "RUNNING"
                return {
                    "decision": "reply",
                    "content_kind": "task_facts",
                    "text": "已全部完成",
                    "fact_refs": actual["fact_refs"],
                }

            status = runtime.run(
                Model(query), message="query-running", text="现在查到哪了"
            )
            assert "RUNNING" in status.text and "已全部完成" not in status.text
            assert len(runtime.model.calls) == 1 and len(runtime.actions) == 1
        finally:
            release.set()
            runtime.finish(first)
    assert len(list(settings.service_log_dir.iterdir())) == 1


def test_exhausted_conversation_cannot_stop_authorized_background_investigation(
    settings,
):
    settings.agent_entry_policy_path.write_text(
        json.dumps(
            policy_data(
                allowed_control_tools=CONTROLS,
                external_request_limit=1,
                model_attempt_reservation=1,
            )
        ),
        encoding="utf-8",
    )
    runtime = Runtime(settings)
    first = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    rejected = Model(lambda p: {"decision": "reply", "text": "你好"})
    assert (
        runtime.run(rejected, message="over-conversation", text="你好").status
        == "LIMITED"
    )
    assert not rejected.calls
    view = runtime.finish(first)
    assert view.budget["external_spent"] == 3 and len(runtime.actions) == 1


def test_real_graph_resume_after_safe_logging_guard_keeps_run_and_usage(
    settings, monkeypatch
):
    from credra_agent.graph import workflow

    runtime = Runtime(settings)
    original = workflow.require_logging

    def guard():
        if runtime.actions:
            raise LoggingUnavailable("LOGGING_UNAVAILABLE_BEFORE_NODE")
        original()

    monkeypatch.setattr(workflow, "require_logging", guard)
    first = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    paused = runtime.finish(first)
    assert (
        paused.execution_blocked == "LOGGING_UNAVAILABLE"
        and paused.budget["external_spent"] == 2
    )
    monkeypatch.setattr(workflow, "require_logging", original)
    resumed = runtime.run(
        decision(
            "resume_investigation",
            {
                "task_id": paused.summary.task_id,
                "expected_state_ref": paused.summary.state_ref,
            },
        ),
        message="resume-paused",
        text="日志恢复后继续",
    )
    final = runtime.finish(resumed)
    assert (
        final.summary.run_id == paused.summary.run_id
        and final.summary.status == "LIMITED"
    )
    assert final.budget["external_spent"] == 3 and len(runtime.actions) == 1


def test_command_ack_replays_when_entry_tool_result_was_not_saved(
    settings, monkeypatch
):
    runtime = Runtime(settings)
    model = decision("prepare_investigation", {"draft": draft()})
    original = EntryBudgetStore.save_tool

    def crash(*args):
        raise SystemExit("after command before entry tool receipt")

    monkeypatch.setattr(EntryBudgetStore, "save_tool", crash)
    with pytest.raises(SystemExit):
        runtime.run(model)
    row = EntryStore(settings.checkpoint_db_path).latest_command(
        EntryStore(settings.checkpoint_db_path).conversation(
            "conversation-controls",
            workspace_reference(settings),
        )["selected_task_id"]
    )
    wait_for_command(settings.checkpoint_db_path, row["command_id"])
    monkeypatch.setattr(EntryBudgetStore, "save_tool", original)
    result = runtime.run(model)
    assert (
        result.status == "REPLY" and len(model.calls) == 1 and len(runtime.actions) == 1
    )
    assert result.budget["external_spent"] == 1 and "命令已处理" in result.text


def test_uncertain_worker_result_is_exposed_on_repeat_without_resending(
    settings, monkeypatch
):
    from credra_agent.entry import delegation

    runtime = Runtime(settings)
    original = delegation.execute_intent

    def lose_ack(**kwargs):
        original(**kwargs)
        raise RuntimeError("offline ack failure")

    monkeypatch.setattr(delegation, "execute_intent", lose_ack)
    model = decision("prepare_investigation", {"draft": draft()})
    first = runtime.run(model)
    command = first.tools[-1].data["command"]["command_id"]
    assert (
        wait_for_command(settings.checkpoint_db_path, command)["status"] == "UNCERTAIN"
    )
    repeated = runtime.run(model)
    assert repeated.status == "LIMITED" and "结果不确定" in repeated.text
    assert (
        len(model.calls) == 1
        and len(runtime.model.calls) == 2
        and len(runtime.actions) == 1
    )


def test_same_subject_multiple_cases_require_explicit_case_then_clarify(settings):
    shutil.copytree(
        settings.data_dir / "case_byd_002594", settings.data_dir / "case_byd_second"
    )
    manifest = settings.data_dir / "case_byd_second" / "source" / "source_manifest.json"
    contents = json.loads(manifest.read_text(encoding="utf-8"))
    contents["case_id"] = "case_byd_second"
    manifest.write_text(json.dumps(contents, ensure_ascii=False), encoding="utf-8")
    runtime = Runtime(settings)
    first = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    assert first.tools[-1].status == "WAITING_CLARIFICATION" and not runtime.model.calls
    assert "case_id" in first.tools[-1].data["unresolved_fields"]
    second = runtime.run(
        decision(
            "submit_clarification",
            {
                "task_id": first.selected_task_id,
                "expected_spec_version": 1,
                "case_id": "case_byd_second",
                "draft": draft(operation="amend"),
            },
        ),
        message="choose-case",
        text="使用第二个比亚迪资料包，年度仍为2025",
    )
    assert runtime.finish(second).summary.case_id == "case_byd_second"


def test_actual_chainlit_message_delegates_to_graph_without_intent_parse(
    settings, monkeypatch
):
    import app.chainlit_app as ui
    from credra_agent.entry import delegation, service

    runtime = Runtime(settings)
    model = decision("prepare_investigation", {"draft": draft()})
    session, sent = {}, []

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
    monkeypatch.setattr(
        delegation, "build_investigation_model", lambda s, p: runtime.model
    )
    monkeypatch.setattr(delegation, "build_ui_executor", runtime.executor)
    asyncio.run(
        ui.on_message(
            SimpleNamespace(
                id="actual-ui-delegation",
                content="调查比亚迪2025年的监管，仅限交易所公告",
            )
        )
    )
    assert "entry_conversation_id" in session and "intent_thread_id" not in session
    store = EntryStore(settings.checkpoint_db_path)
    selected = store.conversation(
        session["entry_conversation_id"], workspace_reference(settings)
    )["selected_task_id"]
    row = store.latest_command(selected)
    assert (
        wait_for_command(settings.checkpoint_db_path, row["command_id"])["status"]
        == "ACKED"
    )
    assert (
        len(model.calls) == 1
        and len(runtime.actions) == 1
        and "intent_parse" not in runtime.model.calls
    )
    assert any("调查命令已接受" in text and "会话预算" in text for text in sent)


def test_legacy_p25_task_keeps_its_historical_intent_charge_when_entry_resumes(
    settings, monkeypatch
):
    from credra_agent.graph import workflow
    from tests.test_v2_ui_execution import run as run_old_ui

    runtime = Runtime(settings)
    old_actions = []
    original = workflow.require_logging

    def guard():
        if old_actions:
            raise LoggingUnavailable("LOGGING_UNAVAILABLE_BEFORE_NODE")
        original()

    monkeypatch.setattr(workflow, "require_logging", guard)
    old = run_old_ui(settings, runtime.model, actions=old_actions)
    assert (
        old.budget["external_spent"] == 3
        and runtime.model.calls.count("intent_parse") == 1
    )
    monkeypatch.setattr(workflow, "require_logging", original)
    view = TaskQueryService(settings, allowed_subject_ids=["002594"]).get(old.thread_id)
    resumed = runtime.run(
        decision(
            "resume_investigation",
            {"task_id": old.thread_id, "expected_state_ref": view.summary.state_ref},
        ),
        message="legacy-resume",
        text="恢复之前的任务",
    )
    final = runtime.finish(resumed)
    assert final.budget["external_spent"] == 4 and resumed.budget["external_spent"] == 1
    assert (
        runtime.model.calls.count("intent_parse") == 1
        and not runtime.actions
        and len(old_actions) == 1
    )


def test_worker_checks_frozen_profile_before_dispatch(settings, monkeypatch):
    original = InvestigationDelegation.schedule
    monkeypatch.setattr(InvestigationDelegation, "schedule", lambda *a: None)
    runtime = Runtime(settings)
    accepted = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    command = accepted.tools[-1].data["command"]["command_id"]
    monkeypatch.setattr(InvestigationDelegation, "schedule", original)
    settings.model_name = "changed-profile"
    controller = InvestigationDelegation(
        settings=settings,
        query=TaskQueryService(settings, allowed_subject_ids=["002594"]),
        model_factory=lambda s, p: runtime.model,
        executor_factory=runtime.executor,
    )
    controller.schedule(command)
    row = wait_for_command(settings.checkpoint_db_path, command)
    assert (
        row["status"] == "ACKED"
        and json.loads(row["result_json"])["outcome"] == "REJECTED"
    )
    assert not runtime.model.calls and not runtime.actions


def test_clarification_reaches_real_waiting_graph_and_updates_authorization_version(
    settings,
):
    runtime = Runtime(settings)
    runtime.model = Models(
        decisions=[
            DecisionDraft(
                decision="ACTION",
                tool="ask_user",
                arguments={
                    "question": "是否继续核对原始监管公告？",
                    "unresolved_fields": ["confirmation"],
                },
                expected_observation="取得用户回答",
                reason_summary="先澄清",
            )
        ]
    )
    first = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    waiting = runtime.finish(first)
    assert waiting.summary.status == "WAITING_CLARIFICATION"
    assert waiting.pending_clarification["question"] == "是否继续核对原始监管公告？"
    assert waiting.unresolved_fields == ["confirmation"]
    repeated = runtime.run(decision("prepare_investigation", {"draft": draft()}))
    assert repeated.duplicate and "是否继续核对原始监管公告" in repeated.text
    answer = runtime.run(
        decision(
            "submit_clarification",
            {
                "task_id": waiting.summary.task_id,
                "expected_spec_version": 1,
                "draft": draft(operation="amend"),
            },
        ),
        message="answer-existing",
        text="继续核对原始监管公告",
    )
    final = runtime.finish(answer)
    assert (
        final.summary.run_id == waiting.summary.run_id
        and final.summary.spec_version == 2
    )
    assert final.summary.status == "LIMITED" and final.budget["external_spent"] == 2
