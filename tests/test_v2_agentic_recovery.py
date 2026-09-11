from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.llm.gateway import StructuredModelError, StructuredModelResult
from app.models.research import ResearchFact, ResearchQueryResult
from app.runtime.tasks import (
    amend_agentic_task,
    get_task_status,
    resume_agentic_task,
    start_agentic_task,
)
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.execution.ledger import (
    ActionLedger,
    DurableBudgetExhausted,
    InvalidLedgerTransition,
)
from credra_agent.graph.versioning import resolve_graph_identity
from credra_agent.intent.models import Period, Question, SourcePolicy, TaskSpec
from credra_agent.planning.models import (
    CoordinatorLimits,
    DecisionDraft,
    RunAuthorization,
)
from credra_agent.runtime.executors import build_agentic_executor
from credra_agent.runtime.service import interpret_and_execute

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FixedModel:
    model_name = "fixed-offline"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        return StructuredModelResult(
            output=self.outputs.pop(0),
            model_name=self.model_name,
            attempts=1,
            latency_ms=1,
            input_tokens=8,
            output_tokens=12,
            accounting_complete=True,
        )


class FailOnce:
    def __init__(self, boundary: str):
        self.boundary = boundary
        self.failed = False

    def __call__(self, boundary, action):
        if boundary == self.boundary and not self.failed:
            self.failed = True
            raise RuntimeError(f"simulated crash at {boundary}")


def task_spec() -> TaskSpec:
    return TaskSpec(
        version=1,
        source_message_id="message-agentic-1",
        operation="start",
        subject_id="002594.SZ",
        subject_name="比亚迪",
        case_id="case_byd_002594",
        as_of=date(2025, 12, 31),
        periods=[Period(start=date(2024, 1, 1), end=date(2025, 12, 31))],
        source_policy=SourcePolicy(
            preferred=["exchange"],
            allowed=["exchange", "regulator"],
            denied=["social_media"],
        ),
        questions=[
            Question(
                question_id="q-regulatory",
                text="调查近期新增监管和负面消息",
                completion_criteria="形成带来源的结论或明确缺口",
                focus="regulatory",
            )
        ],
        readiness="READY",
    )


def authorization() -> RunAuthorization:
    return RunAuthorization(
        authorization_id="offline-agentic-authorization",
        authorized_by="OFFLINE_TEST",
        task_spec_version=1,
        approval="APPROVED",
        external_request_limit=20,
        token_limit=1000,
        active_seconds_limit=60,
        limits=CoordinatorLimits(
            max_decisions=4,
            no_progress_limit=2,
            model_attempt_reservation=2,
            decision_token_reservation=100,
            decision_max_output_tokens=50,
        ),
    )


def search_decision() -> DecisionDraft:
    return DecisionDraft(
        decision="ACTION",
        tool="search_evidence",
        arguments={
            "query": "比亚迪 新增监管消息",
            "subject_id": "002594.SZ",
            "subject_name": "比亚迪",
            "period": {"start": "2024-01-01", "end": "2025-12-31"},
            "source_policy": {
                "preferred": ["exchange"],
                "allowed": ["exchange", "regulator"],
                "denied": ["social_media"],
            },
            "category": "regulatory",
        },
        hypothesis_ids=["hyp-q-regulatory"],
        expected_observation="找到可追溯监管材料",
        reason_summary="先核验监管来源。",
    )


def finish_decision() -> DecisionDraft:
    return DecisionDraft(
        decision="FINISH",
        finish_reason="ANSWERED",
        reason_summary="必答问题已经由观察覆盖。",
    )


def ask_decision() -> DecisionDraft:
    return DecisionDraft(
        decision="ACTION",
        tool="ask_user",
        arguments={
            "question": "请补充是否继续核验监管材料。",
            "unresolved_fields": ["investigation_confirmation"],
        },
        expected_observation="取得用户补充指令",
        reason_summary="继续前需要用户澄清。",
    )


def settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    (data_dir / "case_byd_002594" / "source").mkdir(parents=True)
    return Settings(
        data_dir=data_dir,
        checkpoint_db_path=tmp_path / "checkpoints" / "tasks.db",
        trace_dir=tmp_path / "traces",
        service_log_dir=tmp_path / "logs",
    )


def executor(calls: list) -> ActionExecutor:
    def search(arguments):
        calls.append(arguments.query)
        return ExecutionOutcome(
            status="SUCCESS",
            summary="已从监管来源核验到材料。",
            payload={"evidence": [{"id": "evidence-1"}]},
            novelty_keys=["evidence-1"],
            answered_question_ids=["q-regulatory"],
            actual_external_requests=1,
        )

    return ActionExecutor(
        {"search_evidence": HandlerDefinition(search, external_request_reservation=1)}
    )


def test_missing_graph_version_is_legacy_and_unknown_version_fails_closed():
    legacy = resolve_graph_identity({"status": "WAITING_APPROVAL"})
    assert legacy.graph_version == "legacy_v1"
    assert legacy.execution_mode == "baseline"
    with pytest.raises(ValueError):
        resolve_graph_identity(
            {"graph_version": "future_v3", "execution_mode": "agentic"}
        )


def test_crash_after_decision_reuses_persisted_action(tmp_path):
    config = settings(tmp_path)
    model = FixedModel([search_decision(), finish_decision()])
    calls: list[str] = []
    tools = executor(calls)
    with pytest.raises(RuntimeError, match="after_action_reserved"):
        start_agentic_task(
            thread_id="agentic-after-decision",
            task_spec=task_spec(),
            authorization=authorization(),
            settings=config,
            model=model,
            executor=tools,
            fault_hook=FailOnce("after_action_reserved"),
        )

    result = resume_agentic_task(
        thread_id="agentic-after-decision",
        settings=config,
        model=model,
        executor=tools,
    )
    assert result["state"]["status"] == "COMPLETED"
    assert calls == ["比亚迪 新增监管消息"]
    assert model.calls == 2


def test_crash_after_request_does_not_redispatch_and_charges_uncertain(tmp_path):
    config = settings(tmp_path)
    model = FixedModel([search_decision(), finish_decision()])
    calls: list[str] = []
    tools = executor(calls)
    with pytest.raises(RuntimeError, match="after_request"):
        start_agentic_task(
            thread_id="agentic-after-request",
            task_spec=task_spec(),
            authorization=authorization(),
            settings=config,
            model=model,
            executor=tools,
            fault_hook=FailOnce("after_request"),
        )

    result = resume_agentic_task(
        thread_id="agentic-after-request",
        settings=config,
        model=model,
        executor=tools,
    )
    assert result["state"]["status"] == "LIMITED"
    assert result["state"]["stop_reason"] == "REQUEST_RESULT_UNCERTAIN"
    assert calls == ["比亚迪 新增监管消息"]
    budget = ActionLedger(config.checkpoint_db_path).budget_snapshot(
        "agentic-after-request", authorization()
    )
    assert budget["usage_uncertain"] is True
    assert budget["external_spent"] == 2  # one model + reserved one tool request


def test_crash_after_result_persistence_replays_without_redispatch(tmp_path):
    config = settings(tmp_path)
    model = FixedModel([search_decision(), finish_decision()])
    calls: list[str] = []
    tools = executor(calls)
    with pytest.raises(RuntimeError, match="after_result_stored"):
        start_agentic_task(
            thread_id="agentic-after-result",
            task_spec=task_spec(),
            authorization=authorization(),
            settings=config,
            model=model,
            executor=tools,
            fault_hook=FailOnce("after_result_stored"),
        )

    result = resume_agentic_task(
        thread_id="agentic-after-result",
        settings=config,
        model=model,
        executor=tools,
    )
    assert result["state"]["status"] == "COMPLETED"
    assert calls == ["比亚迪 新增监管消息"]
    records = ActionLedger(config.checkpoint_db_path).dump_actions(
        "agentic-after-result"
    )
    assert records[0]["status"] == "RESULT_STORED"

    persisted = get_task_status(thread_id="agentic-after-result", settings=config)
    assert persisted["state"]["graph_version"] == "agentic_v2"
    assert persisted["state"]["status"] == "COMPLETED"


def test_action_ledger_rejects_result_transition_before_dispatch(tmp_path):
    ledger = ActionLedger(tmp_path / "ledger.db")
    action = search_decision()
    # Use a real Action through an interrupted graph, then assert a terminal row
    # cannot be dispatched a second time.
    config = settings(tmp_path / "runtime")
    model = FixedModel([action, finish_decision()])
    calls: list[str] = []
    result = start_agentic_task(
        thread_id="agentic-terminal-transition",
        task_spec=task_spec(),
        authorization=authorization(),
        settings=config,
        model=model,
        executor=executor(calls),
    )
    assert result["state"]["status"] == "COMPLETED"
    durable = ActionLedger(config.checkpoint_db_path)
    record = durable.dump_actions("agentic-terminal-transition")[0]
    with pytest.raises(InvalidLedgerTransition):
        durable.mark_dispatched(
            "agentic-terminal-transition", record["action"]["action_id"]
        )
    assert ledger.dump_actions("unused") == []


def test_durable_budget_enforces_accumulated_active_time(tmp_path):
    ledger = ActionLedger(tmp_path / "budget.db")
    approved = authorization().model_copy(update={"active_seconds_limit": 1.0})
    ledger.reserve_budget(
        task_id="active-time-task",
        operation_id="decision:1",
        phase="MODEL",
        external=1,
        tokens=10,
        authorization=approved,
    )
    ledger.mark_budget_dispatched("active-time-task", "decision:1")
    ledger.settle_budget(
        "active-time-task",
        "decision:1",
        actual_external=1,
        actual_tokens=10,
        active_seconds=1.1,
    )
    with pytest.raises(DurableBudgetExhausted, match="ACTIVE_TIME_EXHAUSTED"):
        ledger.reserve_budget(
            task_id="active-time-task",
            operation_id="decision:2",
            phase="MODEL",
            external=1,
            tokens=10,
            authorization=approved,
        )


def test_agentic_model_failure_uses_gated_clarification_fallback(tmp_path):
    class FailedModel:
        model_name = "failed-offline"

        def generate(self, **kwargs):
            raise StructuredModelError("MODEL_ERROR", "offline failure", attempts=1)

    def fallback(payload, error):
        assert payload["task_spec"]["subject_id"] == "002594.SZ"
        return DecisionDraft(
            decision="ACTION",
            tool="ask_user",
            arguments={
                "question": "模型不可用，是否稍后继续？",
                "unresolved_fields": ["coordinator_model"],
            },
            expected_observation="取得继续调查指令",
            reason_summary="模型不可用，进入受限澄清。",
        )

    config = settings(tmp_path)
    result = start_agentic_task(
        thread_id="agentic-fallback",
        task_spec=task_spec(),
        authorization=authorization(),
        settings=config,
        model=FailedModel(),
        executor=ActionExecutor(),
        fallback=fallback,
    )
    assert result["state"]["status"] == "WAITING_CLARIFICATION"
    assert result["state"]["stop_reason"] == "USER_INPUT_REQUIRED"


def test_shadow_records_legal_plan_without_executing_selected_tool(tmp_path):
    config = settings(tmp_path)
    calls: list[str] = []
    result = start_agentic_task(
        thread_id="shadow-plan",
        task_spec=task_spec(),
        authorization=authorization(),
        settings=config,
        model=FixedModel([search_decision()]),
        executor=executor(calls),
        execution_mode="shadow",
    )
    assert result["state"]["status"] == "COMPLETED"
    assert result["state"]["stop_reason"] == "SHADOW_PLAN_RECORDED"
    assert calls == []
    records = ActionLedger(config.checkpoint_db_path).dump_actions("shadow-plan")
    assert records[0]["status"] == "RESULT_STORED"
    observation = Path(config.data_dir / "case_byd_002594" / "runs")
    observation_path = next(observation.rglob("agent_observation_v1.json"))
    assert "SHADOW_ACTION_NOT_EXECUTED" in observation_path.read_text(encoding="utf-8")


def test_waiting_agentic_task_accepts_next_task_spec_version_and_continues(tmp_path):
    config = settings(tmp_path)
    model = FixedModel([ask_decision(), search_decision(), finish_decision()])
    calls: list[str] = []
    tools = executor(calls)
    waiting = start_agentic_task(
        thread_id="agentic-clarification",
        task_spec=task_spec(),
        authorization=authorization(),
        settings=config,
        model=model,
        executor=tools,
    )
    assert waiting["state"]["status"] == "WAITING_CLARIFICATION"

    amended_spec = task_spec().model_copy(
        update={
            "version": 2,
            "operation": "amend",
            "source_message_id": "message-agentic-2",
        }
    )
    amended_authorization = authorization().model_copy(
        update={
            "authorization_id": "offline-agentic-authorization-v2",
            "task_spec_version": 2,
        }
    )
    completed = amend_agentic_task(
        thread_id="agentic-clarification",
        task_spec=amended_spec,
        authorization=amended_authorization,
        settings=config,
        model=model,
        executor=tools,
    )
    assert completed["state"]["status"] == "COMPLETED"
    assert completed["state"]["pending_instruction_version"] is None
    assert calls == ["比亚迪 新增监管消息"]


def test_natural_language_requires_authorization_before_agentic_execution(
    tmp_path, monkeypatch
):
    config = settings(tmp_path)
    parsed_spec = task_spec()

    def fake_interpret(**kwargs):
        from credra_agent.intent.models import IntentResult

        return IntentResult(
            source_message_id=kwargs["source_message_id"],
            thread_id=kwargs["thread_id"],
            operation="start",
            task_spec=parsed_spec,
            bound_task_spec_version=1,
            parser_mode="RULE_FALLBACK",
        )

    monkeypatch.setattr(
        "credra_agent.runtime.service.interpret_message", fake_interpret
    )
    model = FixedModel([search_decision()])
    result = interpret_and_execute(
        thread_id="natural-authorization",
        source_message_id="natural-message-1",
        text="调查比亚迪监管消息",
        as_of=date(2025, 12, 31),
        settings=config,
        coordinator_model=model,
        executor_factory=lambda spec: executor([]),
    )
    assert result.outcome == "AUTHORIZATION_REQUIRED"
    assert result.task is None
    assert model.calls == 0


def test_authorized_natural_language_runs_the_agentic_vertical_slice(
    tmp_path, monkeypatch
):
    config = settings(tmp_path)
    parsed_spec = task_spec()
    calls: list[str] = []

    def fake_interpret(**kwargs):
        from credra_agent.intent.models import IntentResult

        return IntentResult(
            source_message_id=kwargs["source_message_id"],
            thread_id=kwargs["thread_id"],
            operation="start",
            task_spec=parsed_spec,
            bound_task_spec_version=1,
            parser_mode="RULE_FALLBACK",
        )

    monkeypatch.setattr(
        "credra_agent.runtime.service.interpret_message", fake_interpret
    )
    model = FixedModel([search_decision(), finish_decision()])
    result = interpret_and_execute(
        thread_id="natural-agentic",
        source_message_id="natural-agentic-message",
        text="调查比亚迪近期新增监管消息",
        as_of=date(2025, 12, 31),
        settings=config,
        execution_mode="agentic",
        authorization=authorization(),
        coordinator_model=model,
        executor_factory=lambda spec: executor(calls),
    )

    assert result.outcome == "TASK_STATE"
    assert result.task["state"]["status"] == "COMPLETED"
    assert result.task["state"]["graph_version"] == "agentic_v2"
    assert calls == ["比亚迪 新增监管消息"]
    assert model.calls == 2


def test_research_executor_maps_verified_service_result_to_question_coverage():
    captured = []

    class FakeResearchClient:
        async def search_evidence(self, arguments):
            captured.append(arguments)
            return ResearchQueryResult(
                query_type="company",
                query=arguments.query,
                found=True,
                facts=[
                    ResearchFact(
                        fact_id=f"fact:{'a' * 64}",
                        category="regulatory",
                        statement="监管材料已经核验。",
                        source_id="exchange-notice-1",
                        verification_status="SUPPORTED",
                    )
                ],
                verification_status="SUPPORTED",
                source="fake-research-service",
            )

    action = search_decision()
    arguments = action.arguments
    runtime_executor = build_agentic_executor(
        task_spec(), research_client=FakeResearchClient()
    )
    from credra_agent.execution.models import SearchEvidenceArgs

    outcome = runtime_executor.execute(
        "search_evidence", SearchEvidenceArgs.model_validate(arguments)
    )

    assert captured[0].model_dump(mode="json") == arguments
    assert outcome.status == "SUCCESS"
    assert outcome.answered_question_ids == ["q-regulatory"]
    assert outcome.gap_question_ids == []
    assert outcome.novelty_keys == [f"fact:{'a' * 64}"]


def test_natural_language_shadow_runs_baseline_and_never_dispatches_plan(
    tmp_path, monkeypatch
):
    config = settings(tmp_path)
    parsed_spec = task_spec()
    baseline_calls: list[tuple[str, str]] = []
    tool_calls: list[str] = []

    def fake_interpret(**kwargs):
        from credra_agent.intent.models import IntentResult

        return IntentResult(
            source_message_id=kwargs["source_message_id"],
            thread_id=kwargs["thread_id"],
            operation="start",
            task_spec=parsed_spec,
            bound_task_spec_version=1,
            parser_mode="RULE_FALLBACK",
        )

    def fake_start_task(*, thread_id, case_id, settings):
        baseline_calls.append((thread_id, case_id))
        return {"thread_id": thread_id, "state": {"status": "COMPLETED"}}

    monkeypatch.setattr(
        "credra_agent.runtime.service.interpret_message", fake_interpret
    )
    monkeypatch.setattr("credra_agent.runtime.service.start_task", fake_start_task)
    result = interpret_and_execute(
        thread_id="natural-shadow",
        source_message_id="natural-shadow-message",
        text="以影子模式调查比亚迪监管消息",
        as_of=date(2025, 12, 31),
        settings=config,
        execution_mode="shadow",
        authorization=authorization(),
        coordinator_model=FixedModel([search_decision()]),
        executor_factory=lambda spec: executor(tool_calls),
    )
    assert result.task["state"]["stop_reason"] == "SHADOW_PLAN_RECORDED"
    assert result.baseline_task["state"]["status"] == "COMPLETED"
    assert result.baseline_thread_id == "natural-shadow-baseline"
    assert baseline_calls == [("natural-shadow-baseline", "case_byd_002594")]
    assert tool_calls == []


def test_agentic_nodes_actions_tools_budgets_and_states_are_logged(tmp_path):
    config = settings(tmp_path)
    result = start_agentic_task(
        thread_id="agentic-service-log",
        task_spec=task_spec(),
        authorization=authorization(),
        settings=config,
        model=FixedModel([search_decision(), finish_decision()]),
        executor=executor([]),
    )
    assert result["state"]["status"] == "COMPLETED"
    records = [
        json.loads(line)
        for path in config.service_log_dir.rglob("service.*.jsonl")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    event_types = {item["event_type"] for item in records}
    assert {"NODE_START", "NODE_END", "TASK_STATE", "ACTION_STATE"} <= event_types
    assert {"TOOL_START", "TOOL_END", "BUDGET_STATE"} <= event_types
    assert {item.get("node") for item in records} >= {
        "agentic_decide",
        "agentic_execute",
    }


def run_recovery_cli(root: Path, command: str, thread_id: str, crash_at=None):
    arguments = [
        sys.executable,
        "-m",
        "tests.agentic_recovery_cli",
        command,
        "--root",
        str(root),
        "--thread-id",
        thread_id,
    ]
    if crash_at:
        arguments.extend(["--crash-at", crash_at])
    return subprocess.run(
        arguments,
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )


@pytest.mark.parametrize(
    ("boundary", "expected_status"),
    [
        ("after_action_reserved", "COMPLETED"),
        ("after_request", "LIMITED"),
        ("after_result_stored", "COMPLETED"),
    ],
)
def test_agentic_crash_windows_across_real_processes(
    tmp_path, boundary, expected_status
):
    thread_id = f"p14-process-{boundary}"
    crashed = run_recovery_cli(tmp_path, "start", thread_id, boundary)
    assert crashed.returncode == 91, crashed.stderr or crashed.stdout

    resumed = run_recovery_cli(tmp_path, "resume", thread_id)
    assert resumed.returncode == 0, resumed.stderr or resumed.stdout
    payload = json.loads(resumed.stdout.strip())
    assert payload["state"]["status"] == expected_status
    assert payload["state"]["graph_version"] == "agentic_v2"
    calls = (tmp_path / "external_calls.txt").read_text(encoding="utf-8").splitlines()
    assert calls == ["比亚迪 新增监管消息"]
    if boundary == "after_request":
        assert payload["state"]["stop_reason"] == "REQUEST_RESULT_UNCERTAIN"
