"""P25 UI dispatch, trusted authorization, paid intent accounting and replay."""

import asyncio
import json
import shutil
import subprocess
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.llm.gateway import StructuredModelError, StructuredModelResult
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.intent.models import IntentDraft
from credra_agent.intent.parser import parse_draft
from credra_agent.intent.store import IntentStore
from credra_agent.planning.models import DecisionDraft
from credra_agent.runtime.ui_policy import UIPolicy
from credra_agent.runtime.ui_service import execute_ui_message, ui_execution_description
from credra_agent.runtime.ui_store import UITaskStore, task_lock

ROOT = Path(__file__).resolve().parents[1]
ANCHOR = date(2026, 6, 30)


def policy_dict(**overrides):
    return {
        "policy_id": "offline-ui",
        "version": 1,
        "approval": "APPROVED",
        "allowed_subject_ids": ["002594", "600104"],
        "external_request_limit": 30,
        "token_limit": 10_000,
        "active_seconds_limit": 60,
        "limits": {
            "max_decisions": 4,
            "no_progress_limit": 2,
            "model_attempt_reservation": 1,
            "decision_token_reservation": 500,
            "decision_max_output_tokens": 200,
        },
        "intent_attempt_reservation": 1,
        "intent_token_reservation": 500,
        "intent_max_output_tokens": 200,
        **overrides,
    }


@pytest.fixture
def settings(tmp_path):
    data = tmp_path / "data"
    for case in ("case_byd_002594", "case_saic_600104"):
        shutil.copytree(ROOT / "data" / case / "source", data / case / "source")
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(policy_dict()), encoding="utf-8")
    return Settings(
        _env_file=None,
        data_dir=data,
        checkpoint_db_path=tmp_path / "tasks.db",
        trace_dir=tmp_path / "traces",
        service_log_dir=tmp_path / "logs",
        agent_ui_execution_enabled=True,
        agent_ui_policy_path=path,
        analysis_mode="llm",
        intent_mode="llm",
        model_name="offline-placeholder",
        model_api_key="test-placeholder",
        research_provider="mock",
        content_fetch_provider="disabled",
    )


class Models:
    model_name = "offline-ui"

    def __init__(self, *, unknown_usage=False, fail_intent=False, decisions=None):
        self.calls = []
        self.unknown_usage = unknown_usage
        self.fail_intent = fail_intent
        self.decisions = list(decisions or [])
        self.decision_count = 0

    def generate(self, **kwargs):
        purpose = kwargs["purpose"]
        self.calls.append(purpose)
        if kwargs["output_schema"] is IntentDraft:
            if self.fail_intent:
                raise StructuredModelError(
                    "MODEL_ERROR", "offline failure", attempts=1, external_requests=1
                )
            output = parse_draft(
                kwargs["payload"]["message"],
                catalog=[],
                anchor_date=ANCHOR,
                current=object() if kwargs["payload"]["has_current_task"] else None,
            )[0]
        elif self.decisions:
            output = self.decisions.pop(0)
        else:
            self.decision_count += 1
            task = kwargs["payload"]["task_spec"]
            if not kwargs["payload"]["observation_index"]:
                output = DecisionDraft(
                    decision="ACTION",
                    tool="search_evidence",
                    arguments={
                        "query": "比亚迪 监管公告",
                        "subject_id": task["subject_id"],
                        "subject_name": task["subject_name"],
                        "period": task["periods"][0],
                        "source_policy": task["source_policy"],
                        "category": "regulatory",
                    },
                    expected_observation="核对原始监管材料",
                    reason_summary="查询监管原始出处",
                )
            else:
                output = DecisionDraft(
                    decision="FINISH",
                    finish_reason="NEEDS_REVIEW",
                    reason_summary="缺少原始材料，披露缺口并结束",
                    review_required=True,
                )
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            external_requests=1,
            latency_ms=1,
            input_tokens=None if self.unknown_usage else 10,
            output_tokens=None if self.unknown_usage else 20,
            accounting_complete=not self.unknown_usage,
        )


def run(
    settings,
    model,
    *,
    thread="ui-task",
    message="ui-1",
    text="调查比亚迪2025年的监管消息",
    actions=None,
):
    actions = [] if actions is None else actions

    def search(arguments):
        actions.append(arguments.query)
        return ExecutionOutcome(
            status="NO_RESULT",
            summary="离线来源无原始公告，保留缺口",
            actual_external_requests=1,
        )

    return execute_ui_message(
        thread_id=thread,
        message_id=message,
        text=text,
        as_of=ANCHOR,
        settings=settings,
        models_factory=lambda s, p: (model, model if s.intent_mode == "llm" else None),
        executor_factory=lambda s, spec, m: ActionExecutor(
            {
                "search_evidence": HandlerDefinition(
                    search, external_request_reservation=1
                )
            }
        ),
    )


def test_ui_runs_actual_graph_and_tool_with_bound_policy(settings):
    model, actions = Models(), []
    result = run(settings, model, actions=actions)
    assert result.outcome == "TASK_STATE"
    assert result.task["state"]["status"] == "LIMITED"
    assert len(actions) == 1 and len(model.calls) == 3
    assert result.budget["external_spent"] == 4
    assert result.budget["token_spent"] == 90
    assert result.observations[0]["status"] == "NO_RESULT"
    state = result.task["state"]
    auth_path = (
        settings.data_dir
        / state["case_id"]
        / "runs"
        / state["run_id"]
        / state["authorization_ref"]
    )
    auth = json.loads(auth_path.read_text(encoding="utf-8"))
    assert auth["task_id"] == "ui-task"
    assert auth["authorized_by"] == "RUNTIME_POLICY"
    assert auth["policy_ref"].startswith("offline-ui:v1:sha256:")


@pytest.mark.parametrize(
    "invalid", ["disabled", "missing", "invalid", "unconfirmed", "caps"]
)
def test_ui_configuration_rejects_before_any_model(settings, invalid):
    if invalid == "disabled":
        settings.agent_ui_execution_enabled = False
    elif invalid == "missing":
        settings.agent_ui_policy_path = None
    elif invalid == "invalid":
        settings.agent_ui_policy_path.write_text(
            '{"private": "test-secret-never-echo"}', encoding="utf-8"
        )
    else:
        settings.agent_ui_policy_path.write_text(
            json.dumps(
                policy_dict(
                    **(
                        {"approval": "UNCONFIRMED"}
                        if invalid == "unconfirmed"
                        else {"token_limit": None}
                    )
                )
            ),
            encoding="utf-8",
        )
    model = Models()
    result = run(settings, model)
    assert result.outcome == "CONFIGURATION_REQUIRED"
    assert not model.calls and result.task is None
    assert "test-secret-never-echo" not in result.model_dump_json()


def test_default_policy_example_cannot_authorize_paid_calls():
    policy = UIPolicy.model_validate_json(
        (ROOT / "config/agent_ui_policy.example.json").read_text(encoding="utf-8")
    )
    assert policy.approval == "UNCONFIRMED"
    assert policy.authorization("example", 1).external_request_limit is None


def test_ui_clarifies_then_starts_same_thread_and_keeps_parse_spending(settings):
    model = Models()
    first = run(settings, model, text="比较去年和今年的现金流")
    assert first.outcome == "WAITING_CLARIFICATION" and first.task is None
    assert len(model.calls) == 1
    second = run(
        settings,
        model,
        message="ui-2",
        text="主体为比亚迪，期间为2024年和2025年，调查监管消息",
    )
    assert second.outcome == "TASK_STATE"
    assert second.intent.task_spec.version == 2
    assert second.budget["external_spent"] == 5
    assert second.budget["token_spent"] == 120


def test_ui_duplicate_message_and_start_do_not_reexecute(settings):
    model, actions = Models(), []
    first = run(settings, model, actions=actions)
    duplicate = run(settings, model, actions=actions)
    another = run(settings, model, message="ui-2", actions=actions)
    assert duplicate.duplicate and duplicate.task == first.task
    assert another.outcome == "CONTROL_UNAVAILABLE"
    assert len(actions) == 1 and len(model.calls) == 3


def test_message_id_conflict_cannot_rebind_task_or_content(settings):
    model = Models()
    run(settings, model)
    assert (
        "SOURCE_MESSAGE_ID_CONFLICT"
        in run(settings, model, text="调查上汽2025年").limitations[0]
    )
    assert (
        "SOURCE_MESSAGE_ID_CONFLICT"
        in run(settings, model, thread="other").limitations[0]
    )
    assert len(model.calls) == 3


def test_ui_status_and_finished_resume_are_free(settings):
    model = Models()
    result = run(settings, model)
    assert run(settings, model, message="status", text="查看状态").task == result.task
    assert run(settings, model, message="resume", text="继续").task == result.task
    assert len(model.calls) == 3


def test_frozen_policy_prevents_changed_file_from_granting_more_budget(settings):
    model = Models()
    first = run(settings, model, text="比较去年和今年的现金流")
    settings.agent_ui_policy_path.write_text(
        json.dumps(policy_dict(token_limit=999999, version=2)), encoding="utf-8"
    )
    second = run(settings, model, message="ui-2", text="调查比亚迪2025年监管消息")
    assert first.budget["token_limit"] == second.budget["token_limit"] == 10000
    assert UITaskStore(settings.checkpoint_db_path).policy("ui-task")[0].version == 1


def test_changed_model_profile_blocks_resume_before_dispatch(settings):
    model = Models()
    run(settings, model)
    settings.analysis_llm_enable_thinking = True
    result = run(settings, model, message="resume", text="resume")
    assert result.outcome == "CONFIGURATION_REQUIRED"
    assert "UI_REQUEST_CONFIGURATION_CHANGED" in result.limitations[0]
    assert len(model.calls) == 3


def test_subject_outside_policy_is_rejected_before_spec_persistence(settings):
    settings.agent_ui_policy_path.write_text(
        json.dumps(policy_dict(allowed_subject_ids=["600104"])), encoding="utf-8"
    )
    model = Models()
    result = run(settings, model)
    assert result.outcome == "CONTROL_UNAVAILABLE"
    assert "SUBJECT_NOT_AUTHORIZED" in result.limitations[0]
    assert len(model.calls) == 1
    assert IntentStore(settings.checkpoint_db_path).latest_task_spec("ui-task") is None


def test_unknown_intent_usage_settles_reserved_tokens(settings):
    model = Models(unknown_usage=True)
    result = run(settings, model)
    assert result.budget["usage_uncertain"]
    assert result.budget["external_spent"] == 4
    assert result.budget["token_spent"] == 1500


def test_failed_intent_is_counted_before_rule_fallback(settings):
    model = Models(fail_intent=True)
    result = run(settings, model)
    assert "LLM_INTENT_FALLBACK" in result.intent.warnings
    assert result.budget["token_spent"] == 560
    assert result.budget["external_spent"] == 4


def test_budget_cannot_be_reset_between_clarification_and_investigation(settings):
    settings.agent_ui_policy_path.write_text(
        json.dumps(policy_dict(external_request_limit=2)), encoding="utf-8"
    )
    model, actions = Models(), []
    run(settings, model, text="比较去年和今年的现金流")
    result = run(
        settings,
        model,
        message="ui-2",
        text="调查比亚迪2025年的监管消息",
        actions=actions,
    )
    assert result.task["state"]["stop_reason"] == "EXTERNAL_REQUEST_BUDGET_EXHAUSTED"
    assert result.budget["external_spent"] == 2
    assert not actions and len(model.calls) == 2


def test_intent_budget_is_checked_before_parse(settings):
    settings.agent_ui_policy_path.write_text(
        json.dumps(policy_dict(token_limit=100)), encoding="utf-8"
    )
    model = Models()
    result = run(settings, model)
    assert result.outcome == "LIMITED" and not model.calls
    assert result.limitations == ["TOKEN_BUDGET_EXHAUSTED"]


def test_concurrent_process_owns_task_lock_and_exit_releases_it(settings):
    with task_lock(settings.checkpoint_db_path, "ui-task"):
        script = "from pathlib import Path; from credra_agent.runtime.ui_store import task_lock, UIRequestBlocked; import sys\ntry:\n with task_lock(Path(sys.argv[1]), 'ui-task'): sys.exit(3)\nexcept UIRequestBlocked: sys.exit(0)"
        process = subprocess.run(
            [sys.executable, "-c", script, str(settings.checkpoint_db_path)],
            cwd=ROOT,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert process.returncode == 0, process.stderr.decode()
        assert run(settings, Models()).limitations[0].startswith("UI_TASK_BUSY")
    with task_lock(settings.checkpoint_db_path, "ui-task"):
        pass


def test_control_queries_do_not_hide_latest_task_spec(settings):
    model = Models()
    run(settings, model, text="比较去年和今年的现金流")
    from credra_agent.intent.service import interpret_message

    interpret_message(
        thread_id="ui-task",
        source_message_id="control",
        text="查看状态",
        as_of=ANCHOR,
        data_dir=settings.data_dir,
        database_path=settings.checkpoint_db_path,
    )
    assert (
        IntentStore(settings.checkpoint_db_path).latest_task_spec("ui-task") is not None
    )


def test_chainlit_natural_language_handler_reaches_actual_runtime(
    settings, monkeypatch
):
    import app.chainlit_app as ui
    import credra_agent.runtime.ui_service as service

    model, actions, sent = Models(), [], []
    session = {}

    class Message:
        def __init__(self, content="", **kwargs):
            self.content = content

        async def send(self):
            sent.append(self.content)
            return self

        async def update(self):
            sent.append(self.content)

    async def send_payload(payload):
        sent.append(payload["state"]["status"])

    def factory(s, spec, m):
        def search(args):
            actions.append(args.query)
            return ExecutionOutcome(
                status="NO_RESULT", summary="来源缺口", actual_external_requests=1
            )

        return ActionExecutor(
            {
                "search_evidence": HandlerDefinition(
                    search, external_request_reservation=1
                )
            }
        )

    monkeypatch.setattr(ui.cl, "Message", Message)
    monkeypatch.setattr(
        ui.cl, "user_session", SimpleNamespace(get=session.get, set=session.__setitem__)
    )
    monkeypatch.setattr(ui, "_send_payload", send_payload)
    monkeypatch.setattr(service, "build_ui_models", lambda s, p: (model, model))
    monkeypatch.setattr(service, "build_ui_executor", factory)
    asyncio.run(
        ui._interpret_natural_language(
            SimpleNamespace(id="ui-source", content="调查比亚迪2025年的监管消息"),
            settings,
        )
    )
    assert len(actions) == 1 and len(model.calls) == 3
    assert any("Agent 调查状态" in item for item in sent)
    assert not any("不会自行创建运行授权" in item for item in sent)
    assert session["intent_thread_id"].startswith("agentic-ui-")


def test_ui_startup_message_discloses_configuration(settings):
    assert "自动执行已启用" in ui_execution_description(settings)
    settings.agent_ui_execution_enabled = False
    assert "解析模式" in ui_execution_description(settings)


@pytest.mark.parametrize(
    "boundary",
    [
        "after_action_reserved",
        "after_request",
        "after_result_stored",
        "intent_dispatched",
        "intent_result_stored",
        "before_graph_start",
    ],
)
def test_ui_true_process_exit_and_resume_do_not_repeat_dispatched_requests(
    settings, boundary
):
    directory = settings.checkpoint_db_path.parent

    def process(command, boundary=""):
        args = [sys.executable, "-m", "tests.ui_execution_cli", str(directory), command]
        if boundary:
            args += ["--boundary", boundary]
        return subprocess.run(
            args, cwd=ROOT, capture_output=True, timeout=45, check=False
        )

    failed = process("start", boundary)
    assert failed.returncode == 73, failed.stderr.decode()
    recovered = process("replay" if boundary.startswith("intent_") else "resume")
    assert recovered.returncode == 0, recovered.stderr.decode()
    result = json.loads(recovered.stdout.decode().strip().splitlines()[-1])
    calls = [
        json.loads(line)
        for line in (directory / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len([item for item in calls if item.get("purpose") == "intent_parse"]) == 1
    if boundary == "intent_dispatched":
        assert result["outcome"] == "CONTROL_UNAVAILABLE"
        assert "INTENT_REQUEST_RESULT_UNCERTAIN" in result["limitations"][0]
        assert not any(item["type"] == "tool" for item in calls)
    else:
        assert len([item for item in calls if item["type"] == "tool"]) == 1
        assert result["task"]["state"]["status"] == "LIMITED"
        if boundary == "after_request":
            assert result["task"]["state"]["stop_reason"] == "REQUEST_RESULT_UNCERTAIN"


def test_ui_ask_user_question_is_visible_and_answer_can_continue(settings):
    ask = DecisionDraft(
        decision="ACTION",
        tool="ask_user",
        arguments={
            "question": "是否继续核对原始监管公告？",
            "unresolved_fields": ["confirmation"],
        },
        expected_observation="取得用户回答",
        reason_summary="继续调查前澄清",
    )
    model = Models(decisions=[ask])
    first = run(settings, model)
    assert first.task["state"]["status"] == "WAITING_CLARIFICATION"
    from app.chainlit_app import _agent_run_markdown

    assert "是否继续核对原始监管公告" in _agent_run_markdown(first)
    before = len(model.calls)
    assert (
        run(settings, model, message="resume", text="继续").outcome
        == "WAITING_CLARIFICATION"
    )
    assert len(model.calls) == before
    second = run(settings, model, message="answer", text="继续核对监管公告")
    assert second.task["state"]["status"] == "LIMITED"
    assert second.intent.task_spec.version == 2
    assert second.budget["token_spent"] > first.budget["token_spent"]


def test_ui_logging_failure_blocks_intent_request_then_keeps_same_budget(
    settings, monkeypatch
):
    import credra_agent.runtime.ui_budget as budget
    from credra_agent.observability.collector import LoggingUnavailable

    model = Models()

    def unavailable():
        raise LoggingUnavailable("LOGGING_UNAVAILABLE_BEFORE_NODE")

    monkeypatch.setattr(budget, "require_logging", unavailable)
    first = run(settings, model)
    assert first.outcome == "PAUSED_LOGGING" and not model.calls
    monkeypatch.undo()
    second = run(settings, model, message="after-logging")
    assert second.budget["token_limit"] == first.budget["token_limit"]
    assert second.outcome == "TASK_STATE"


def test_ui_cross_task_authorization_is_rejected_before_call_or_artifact(settings):
    model = Models()
    result = run(settings, model)
    from app.runtime.tasks import start_agentic_task

    _policy, auth, _ = UITaskStore(settings.checkpoint_db_path).policy("ui-task")
    with pytest.raises(ValueError, match="AUTHORIZATION_SCOPE_MISMATCH"):
        start_agentic_task(
            thread_id="other",
            task_spec=result.intent.task_spec,
            authorization=auth,
            settings=settings,
            model=model,
            executor=ActionExecutor(),
        )
    assert len(model.calls) == 3


def test_production_ui_models_validate_missing_dependencies_without_requests(settings):
    from credra_agent.runtime.ui_service import build_ui_models

    settings.research_provider = "tavily"
    settings.tavily_api_key = type(settings.model_api_key)("")
    with pytest.raises(ValueError, match="UI_TAVILY_CONFIGURATION_REQUIRED"):
        build_ui_models(settings, UIPolicy.model_validate(policy_dict()))


def test_disabled_ui_does_not_use_unbudgeted_intent_llm(settings, monkeypatch):
    import app.chainlit_app as ui

    settings.agent_ui_execution_enabled = False
    session, sent = {}, []

    class Message:
        def __init__(self, content="", **kwargs):
            self.content = content

        async def send(self):
            sent.append(self.content)

    monkeypatch.setattr(ui.cl, "Message", Message)
    monkeypatch.setattr(
        ui.cl, "user_session", SimpleNamespace(get=session.get, set=session.__setitem__)
    )
    asyncio.run(
        ui._interpret_natural_language(
            SimpleNamespace(id="disabled", content="调查比亚迪2025年的监管消息"),
            settings,
        )
    )
    assert "RULE_FALLBACK" in sent[0] and "不会自行创建运行授权" in sent[0]


def test_ui_source_policy_rejects_model_widening_before_handler(settings):
    class WideningModel(Models):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if (
                isinstance(result.output, DecisionDraft)
                and result.output.decision == "ACTION"
            ):
                result.output.arguments["source_policy"]["allowed"] = None
            return result

    model, actions = WideningModel(), []
    result = run(
        settings,
        model,
        text="调查比亚迪2025年的监管消息，仅限交易所公告",
        actions=actions,
    )
    assert not actions
    assert any(
        item["code"] == "SOURCE_ALLOWLIST_WIDENED"
        for item in result.task["state"]["policy_rejections"]
    )


def test_ui_missing_result_usage_is_conservatively_charged_before_any_retry(settings):
    class FailingModel(Models):
        def generate(self, **kwargs):
            if kwargs["output_schema"] is not IntentDraft:
                self.calls.append(kwargs["purpose"])
                raise StructuredModelError(
                    "MODEL_ERROR", "offline", attempts=1, external_requests=1
                )
            return super().generate(**kwargs)

    model = FailingModel()
    result = run(settings, model)
    assert result.task["state"]["stop_reason"] == "MODEL_UNAVAILABLE"
    assert result.budget["token_spent"] == 530
    assert result.budget["external_spent"] == 2
    assert len(model.calls) == 2


def test_tampered_ui_authorization_cannot_expand_budget(settings):
    model = Models()
    run(settings, model, text="比较去年和今年的现金流")
    store = UITaskStore(settings.checkpoint_db_path)
    with store.connect() as connection:
        row = connection.execute(
            "SELECT authorization_json FROM credra_ui_policies WHERE thread_id='ui-task'"
        ).fetchone()
        authorization = json.loads(row[0])
        authorization["token_limit"] = 999999
        connection.execute(
            "UPDATE credra_ui_policies SET authorization_json=? WHERE thread_id='ui-task'",
            (json.dumps(authorization),),
        )
    result = run(settings, model, message="second", text="调查比亚迪2025年的监管消息")
    assert result.outcome == "CONTROL_UNAVAILABLE"
    assert "UI_STORED_AUTHORIZATION_MISMATCH" in result.limitations[0]
    assert len(model.calls) == 1


def test_ui_gap_changes_next_executed_query(settings):
    class ReplanningModel(Models):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if (
                kwargs["output_schema"] is not IntentDraft
                and len(kwargs["payload"]["observation_index"]) == 1
            ):
                task = kwargs["payload"]["task_spec"]
                decision = DecisionDraft(
                    decision="ACTION",
                    tool="search_evidence",
                    arguments={
                        "query": "比亚迪监管消息 原始出处与独立来源",
                        "subject_id": task["subject_id"],
                        "subject_name": task["subject_name"],
                        "period": task["periods"][0],
                        "source_policy": task["source_policy"],
                        "category": "regulatory",
                    },
                    expected_observation="首次检索无结果，转向原始出处与独立来源",
                    reason_summary="依据缺口调整检索",
                )
                return replace(result, output=decision)
            return result

    model, actions = ReplanningModel(), []
    result = run(settings, model, actions=actions)
    assert len(actions) == 2 and actions[0] != actions[1]
    assert len(result.observations) == 2
    assert result.observations[1]["query"] == actions[1]
    assert result.observations[1]["tool"] == "search_evidence"
    assert result.task["state"]["status"] == "LIMITED"
