from datetime import date

import pytest

from app.llm.gateway import StructuredModelError, StructuredModelResult
from credra_agent.execution.budget import BudgetLedger, BudgetUnavailable
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.execution.policy import ActionPolicy, PolicyViolation
from credra_agent.execution.registry import default_registry
from credra_agent.intent.models import Period, Question, SourcePolicy, TaskSpec
from credra_agent.planning.coordinator import Coordinator
from credra_agent.planning.models import (
    CoordinatorLimits,
    DecisionDraft,
    RunAuthorization,
)


def task_spec(*, allowed=None) -> TaskSpec:
    return TaskSpec(
        version=1,
        source_message_id="message-1",
        operation="start",
        subject_id="002594.SZ",
        subject_name="比亚迪",
        case_id="case_byd_002594",
        as_of=date(2025, 12, 31),
        periods=[Period(start=date(2024, 1, 1), end=date(2025, 12, 31))],
        source_policy=SourcePolicy(
            preferred=["exchange"], allowed=allowed, denied=["social_media"]
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


def search_decision(query: str, *, category: str = "regulatory") -> DecisionDraft:
    return DecisionDraft(
        decision="ACTION",
        tool="search_evidence",
        arguments={
            "query": query,
            "subject_id": "002594.SZ",
            "subject_name": "比亚迪",
            "period": {"start": "2024-01-01", "end": "2025-12-31"},
            "source_policy": {
                "preferred": ["exchange"],
                "allowed": ["exchange", "regulator"],
                "denied": ["social_media"],
            },
            "category": category,
        },
        hypothesis_ids=["hyp-q-regulatory"],
        expected_observation="检索新增消息并识别缺口",
        reason_summary="用户明确要求调查新增消息。",
    )


def finish_decision(reason="ANSWERED") -> DecisionDraft:
    return DecisionDraft(
        decision="FINISH",
        reason_summary="现有观察已足以进入完成门禁。",
        finish_reason=reason,
    )


class FixedModel:
    model_name = "fixed-offline"

    def __init__(self, outputs, *, accounting_complete=True):
        self.outputs = list(outputs)
        self.payloads = []
        self.accounting_complete = accounting_complete

    def generate(self, **kwargs):
        self.payloads.append(kwargs["payload"])
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            latency_ms=1,
            input_tokens=8,
            output_tokens=12,
            accounting_complete=self.accounting_complete,
        )


def approved_budget(*, external=20, tokens=1000):
    return BudgetLedger(
        approval="APPROVED",
        external_limit=external,
        token_limit=tokens,
        active_seconds_limit=60,
    )


def coordinator(
    tmp_path,
    model,
    handler,
    *,
    budget=None,
    decisions=5,
    no_progress=2,
    model_attempts=2,
    fallback=None,
):
    budget = budget or approved_budget()
    snapshot = budget.snapshot()
    authorization = RunAuthorization(
        authorization_id="offline-authorization",
        authorized_by="OFFLINE_TEST",
        task_spec_version=1,
        approval=snapshot.approval,
        external_request_limit=snapshot.external_limit,
        token_limit=snapshot.token_limit,
        active_seconds_limit=snapshot.active_seconds_limit,
        limits=CoordinatorLimits(
            max_decisions=decisions,
            no_progress_limit=no_progress,
            model_attempt_reservation=model_attempts,
            decision_token_reservation=100,
            decision_max_output_tokens=50,
        ),
    )
    return Coordinator(
        model=model,
        executor=ActionExecutor(
            {
                "search_evidence": HandlerDefinition(
                    handler, external_request_reservation=1
                )
            }
        ),
        authorization=authorization,
        run_dir=tmp_path,
        fallback=fallback,
    )


def test_coordinator_investigates_explicit_message_and_adapts_after_gap(tmp_path):
    calls = []

    def handler(arguments):
        calls.append(arguments)
        if len(calls) == 1:
            return ExecutionOutcome(
                status="SUCCESS",
                summary="交易所渠道未覆盖诉讼事项，需要转向监管渠道。",
                novelty_keys=["gap:legal"],
                gap_question_ids=["q-regulatory"],
                actual_external_requests=1,
            )
        return ExecutionOutcome(
            status="SUCCESS",
            summary="监管渠道核验完成。",
            payload={"evidence": [{"id": "evidence-2"}]},
            novelty_keys=["evidence-2"],
            answered_question_ids=["q-regulatory"],
            actual_external_requests=1,
        )

    model = FixedModel(
        [
            search_decision("比亚迪 新增监管消息"),
            search_decision("比亚迪 诉讼 监管处罚", category="legal"),
            finish_decision(),
        ]
    )
    result = coordinator(tmp_path, model, handler).run(
        task_spec(allowed=["exchange", "regulator"])
    )

    assert result.status == "COMPLETED"
    assert [item.query for item in calls] == [
        "比亚迪 新增监管消息",
        "比亚迪 诉讼 监管处罚",
    ]
    assert model.payloads[1]["observation_index"][0]["status"] == "SUCCESS"
    assert "诉讼事项" in model.payloads[1]["observation_index"][0]["summary"]
    assert result.coverage.complete
    assert any("agent_action" in ref for ref in result.artifact_refs)
    assert any("agent_tool_result" in ref for ref in result.artifact_refs)
    assert any("agent_model_budget" in ref for ref in result.artifact_refs)
    assert result.actions[0]["budget_ref"] in result.artifact_refs


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda args: args.update(subject_id="600104.SH"), "SUBJECT_SCOPE_VIOLATION"),
        (
            lambda args: args.update(
                period={"start": "2023-01-01", "end": "2025-12-31"}
            ),
            "PERIOD_SCOPE_VIOLATION",
        ),
        (
            lambda args: args["source_policy"].update(allowed=None),
            "SOURCE_ALLOWLIST_WIDENED",
        ),
        (
            lambda args: args["source_policy"].update(denied=[]),
            "SOURCE_DENYLIST_WIDENED",
        ),
        (
            lambda args: args["source_policy"].update(preferred=[]),
            "SOURCE_PREFERENCE_DROPPED",
        ),
    ],
)
def test_action_policy_rejects_scope_widening(mutate, code):
    spec = task_spec(allowed=["exchange", "regulator"])
    args = search_decision("query").arguments
    mutate(args)
    with pytest.raises(PolicyViolation) as error:
        ActionPolicy(default_registry()).authorize(
            tool="search_evidence",
            arguments=args,
            task_spec=spec,
            executable_tools={"search_evidence"},
            available_refs=set(),
            prior_signatures=set(),
        )
    assert error.value.code == code


def test_duplicate_action_is_not_dispatched_or_charged_as_tool_call(tmp_path):
    calls = []

    def handler(arguments):
        calls.append(arguments)
        return ExecutionOutcome(
            status="SUCCESS",
            summary="发现一条材料，但尚未覆盖必答问题。",
            novelty_keys=["evidence-1"],
            actual_external_requests=1,
        )

    same = search_decision("比亚迪 新增监管消息")
    model = FixedModel([same, same.model_copy(deep=True), finish_decision()])
    runner = coordinator(tmp_path, model, handler, budget=approved_budget())
    result = runner.run(task_spec(allowed=["exchange", "regulator"]))

    assert result.status == "LIMITED"
    assert result.stop_reason == "FINISH_GATE_REJECTED"
    assert len(calls) == 1
    assert runner.budget.snapshot().external_spent == 4  # 3 model + 1 tool


def test_no_result_cannot_pass_answered_finish_gate(tmp_path):
    model = FixedModel([search_decision("比亚迪 无结果检索"), finish_decision()])

    def handler(arguments):
        return ExecutionOutcome(
            status="NO_RESULT",
            summary="指定范围内未检出结果。",
            actual_external_requests=1,
        )

    result = coordinator(tmp_path, model, handler, no_progress=1).run(
        task_spec(allowed=["exchange", "regulator"])
    )
    assert result.status == "LIMITED"
    assert result.stop_reason == "FINISH_GATE_REJECTED"
    assert result.coverage.gap_question_ids == ["q-regulatory"]
    assert not result.coverage.complete


def test_budget_is_reserved_before_tool_dispatch(tmp_path):
    calls = []
    model = FixedModel([search_decision("比亚迪 新增监管消息")])
    budget = approved_budget(external=1)

    def handler(arguments):
        calls.append(arguments)
        raise AssertionError("must not dispatch")

    result = coordinator(tmp_path, model, handler, budget=budget, model_attempts=1).run(
        task_spec(allowed=["exchange", "regulator"])
    )
    assert result.status == "LIMITED"
    assert result.stop_reason == "EXTERNAL_REQUEST_BUDGET_EXHAUSTED"
    assert calls == []
    assert any("agent_coverage" in ref for ref in result.artifact_refs)
    budget_ref = result.actions[0]["budget_ref"]
    audit = (tmp_path / budget_ref).read_text(encoding="utf-8")
    assert '"status": "REJECTED"' in audit
    assert "EXTERNAL_REQUEST_BUDGET_EXHAUSTED" in audit


def test_unconfirmed_budget_blocks_model_and_model_failure_is_limited(tmp_path):
    model = FixedModel([finish_decision()])
    unconfirmed = BudgetLedger(approval="UNCONFIRMED")
    blocked = coordinator(
        tmp_path / "blocked", model, lambda _: None, budget=unconfirmed
    ).run(task_spec(allowed=["exchange", "regulator"]))
    assert blocked.status == "LIMITED"
    assert blocked.stop_reason == "BUDGET_UNCONFIRMED"
    assert model.payloads == []

    failed_model = FixedModel(
        [StructuredModelError("MODEL_ERROR", "offline failure", attempts=1)]
    )
    failed = coordinator(tmp_path / "failed", failed_model, lambda _: None).run(
        task_spec(allowed=["exchange", "regulator"])
    )
    assert failed.status == "LIMITED"
    assert failed.stop_reason == "MODEL_UNAVAILABLE"
    assert failed.coverage.review_required


def test_model_fallback_runs_through_the_same_action_policy(tmp_path):
    failed_model = FixedModel(
        [StructuredModelError("MODEL_ERROR", "offline failure", attempts=1)]
    )

    def safe_fallback(payload, error):
        assert payload["task_spec"]["subject_id"] == "002594.SZ"
        return DecisionDraft(
            decision="ACTION",
            tool="ask_user",
            arguments={
                "question": "模型不可用，是否在恢复后继续调查？",
                "unresolved_fields": ["coordinator_model"],
            },
            expected_observation="取得用户对恢复后继续的指示",
            reason_summary="模型不可用，转为受限澄清动作。",
        )

    result = coordinator(
        tmp_path,
        failed_model,
        lambda _: None,
        fallback=safe_fallback,
    ).run(task_spec(allowed=["exchange", "regulator"]))
    assert result.status == "WAITING_CLARIFICATION"
    assert result.actions[0]["tool"] == "ask_user"


def test_unknown_usage_is_charged_conservatively():
    ledger = approved_budget(external=5, tokens=100)
    reservation = ledger.reserve(external=2, tokens=40)
    ledger.settle(reservation, actual_external=1, actual_tokens=None)
    snapshot = ledger.snapshot()
    assert snapshot.external_spent == 1
    assert snapshot.token_spent == 40
    assert snapshot.usage_uncertain

    with pytest.raises(BudgetUnavailable):
        BudgetLedger(approval="UNCONFIRMED").reserve(external=1)


def test_run_authorization_has_no_implicit_approved_caps_and_binds_task_version(
    tmp_path,
):
    with pytest.raises(ValueError, match="requires all budget caps"):
        RunAuthorization(
            authorization_id="invalid-authorization",
            authorized_by="RUNTIME_POLICY",
            task_spec_version=1,
            approval="APPROVED",
            limits=CoordinatorLimits(
                max_decisions=1,
                no_progress_limit=1,
                model_attempt_reservation=1,
                decision_token_reservation=100,
                decision_max_output_tokens=50,
            ),
        )

    model = FixedModel([finish_decision()])
    runner = coordinator(tmp_path, model, lambda _: None)
    mismatched = task_spec(allowed=["exchange", "regulator"]).model_copy(
        update={"version": 2}
    )
    result = runner.run(mismatched)
    assert result.status == "FAILED"
    assert result.stop_reason == "AUTHORIZATION_SCOPE_MISMATCH"
    assert result.authorization_id == "offline-authorization"
    assert model.payloads == []
    assert any("agent_run_authorization" in ref for ref in result.artifact_refs)


def test_incomplete_model_accounting_uses_the_full_reservation(tmp_path):
    model = FixedModel([finish_decision()], accounting_complete=False)
    runner = coordinator(tmp_path, model, lambda _: None)
    result = runner.run(task_spec(allowed=["exchange", "regulator"]))
    assert result.budget["external_spent"] == 2
    assert result.budget["token_spent"] == 100
    assert result.budget["usage_uncertain"] is True
    model_audit_ref = next(
        ref for ref in result.artifact_refs if "agent_model_budget" in ref
    )
    audit = (tmp_path / model_audit_ref).read_text(encoding="utf-8")
    assert '"status": "SETTLED_UNCERTAIN"' in audit
    assert '"actual_tokens": null' in audit
