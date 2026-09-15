"""Complex-case report grounding, source authority and offline replay."""

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.evals.complex_case_replay import (
    FixedTrialCoordinator,
    PooledTrialModel,
    check_report,
    load_package,
    offline_authorization,
    run_complex_case_replay,
)
from app.report import write_agent_report
from credra_agent.intent.models import SourcePolicy
from credra_agent.planning.models import (
    ClaimProposal,
    Coverage,
    DecisionDraft,
    QuestionAssessment,
)
from tests.test_v2_financial import (
    FinancialCitation,
    dataset,
    financial_task,
    report_fixture,
)

ROOT = Path(__file__).resolve().parents[1]


def test_coordinator_prompt_bounds_finish_projection():
    prompt = (ROOT / "credra_agent/prompts/coordinator.md").read_text(encoding="utf-8")
    assert "conclusion 控制在 100 个汉字以内" in prompt
    assert "limitations 最多两项" in prompt
    assert "不重复抄写原文或计算值" in prompt
    assert "FINISH` 时只输出" in prompt
    assert "值为默认 `null`、`{}` 或 `[]` 的可选字段可以省略" in prompt
    assert "应先选择 verify_claim，不要直接 FINISH" in prompt
    assert "growth_gap 不替代 revenue_growth" in prompt
    assert "外层 bundle 的 reference" in prompt
    assert "supporting_evidence_ids" in prompt
    assert "任一必答问题为 UNRESOLVED" in prompt
    assert "不得仅改用新版本 bundle reference 重复核验" in prompt
    assert "相关原文已经出现在 document_reads 后才能提出" in prompt
    assert "指向产生该断言的原文来源" in prompt
    assert "财务结果和普通工具结果也不能填入 evidence_refs" in prompt
    assert "已经为某问题读取相关原文" in prompt
    assert "INVALID_QUESTION_ASSESSMENT" in prompt
    assert "evidence_refs 与 claim_ids 必须同时为空或同时非空" in prompt
    assert "read_document_ids 是本 Run 已读取文档的完整小索引" in prompt


def test_new_claim_proposal_is_bound_to_matching_verify_action():
    proposal = ClaimProposal(
        proposal_id="proposal:statement",
        question_id="q-settlement",
        statement="来源文档中的单一待核验断言。",
        kind="REPORTED_FACT",
        source_document_ids=["source-doc"],
    )
    accepted = DecisionDraft(
        decision="ACTION",
        tool="verify_claim",
        arguments={
            "claim_id": proposal.proposal_id,
            "source_refs": ["artifacts/agent_evidence_v1.json"],
            "document_ids": ["source-doc"],
        },
        expected_observation="返回核验结果。",
        reason_summary="核验已读取原文中的主张。",
        claim_proposals=[proposal],
    )
    assert accepted.claim_proposals == [proposal]

    with pytest.raises(ValueError, match="matching verify_claim target"):
        DecisionDraft(
            decision="ACTION",
            tool="read_document",
            arguments={"reference_id": "artifact", "document_id": "source-doc"},
            expected_observation="读取原文。",
            reason_summary="读取后再形成主张。",
            claim_proposals=[proposal],
        )


def test_verify_signature_ignores_only_rotating_bundle_reference():
    from credra_agent.execution.policy import canonical_action_signature

    first = canonical_action_signature(
        "verify_claim",
        {
            "claim_id": "proposal:statement",
            "source_refs": ["artifacts/agent_evidence_v1.json"],
            "document_ids": ["source-b", "source-a"],
        },
    )
    rotated = canonical_action_signature(
        "verify_claim",
        {
            "claim_id": "proposal:statement",
            "source_refs": ["artifacts/agent_evidence_v2.json"],
            "document_ids": ["source-a", "source-b"],
        },
    )
    new_evidence = canonical_action_signature(
        "verify_claim",
        {
            "claim_id": "proposal:statement",
            "source_refs": ["artifacts/agent_evidence_v2.json"],
            "document_ids": ["source-a", "source-c"],
        },
    )
    assert first == rotated and first != new_evidence


@pytest.mark.parametrize("tag", ["exchange", "exchange_disclosure"])
def test_exchange_synonym_preserves_allow_and_deny_authority(tag):
    other = "exchange_disclosure" if tag == "exchange" else "exchange"
    assert SourcePolicy(allowed=[tag]).permits([other, "financial_report"])
    assert not SourcePolicy(denied=[tag]).permits([other, "financial_report"])
    assert not SourcePolicy(allowed=[tag]).permits(["media"])
    assert not SourcePolicy(denied=[tag]).permits([])
    with pytest.raises(ValidationError):
        SourcePolicy(allowed=[tag], denied=[other])


def test_unresolved_assessment_can_disclose_missing_evidence():
    unresolved = QuestionAssessment(
        question_id="q-gap",
        status="UNRESOLVED",
        conclusion="当前材料不足。",
        limitations=["缺少独立履约证据。"],
    )
    assert unresolved.evidence_refs == [] and unresolved.claim_ids == []

    answered = QuestionAssessment(
        question_id="q-gap",
        status="ANSWERED",
        conclusion="仍需由任务感知的语义门禁验证。",
    )
    assert answered.evidence_refs == [] and answered.claim_ids == []


def test_semantic_gate_rejects_bundle_reference_without_bound_claim(tmp_path):
    from credra_agent.planning.evidence_context import assess_questions

    store, task, refs, _ = report_fixture(tmp_path)
    evidence_ref = next(ref for ref in refs if "agent_evidence" in ref)
    with pytest.raises(ValueError, match="must be referenced together"):
        assess_questions(
            store,
            [
                QuestionAssessment(
                    question_id="q-cash",
                    status="UNRESOLVED",
                    conclusion="错误地复述来源事实但不绑定主张。",
                    evidence_refs=[evidence_ref],
                )
            ],
            references=refs,
            task=task,
        )


def test_nonfinancial_answer_still_requires_verified_claim(tmp_path):
    from credra_agent.planning.evidence_context import assess_questions

    store, task, refs, _ = report_fixture(tmp_path)
    nonfinancial = task.model_copy(
        update={
            "questions": [task.questions[0].model_copy(update={"focus": "regulatory"})]
        }
    )
    with pytest.raises(ValueError, match="verified claim or financial citation"):
        assess_questions(
            store,
            [
                QuestionAssessment(
                    question_id="q-cash",
                    status="ANSWERED",
                    conclusion="错误地声称非财务问题已回答。",
                )
            ],
            references=refs,
            task=nonfinancial,
        )


def test_financial_answer_can_use_computed_citation_without_claim(tmp_path):
    from credra_agent.planning.evidence_context import assess_questions

    store, task, refs, result_ref = report_fixture(tmp_path)
    answered, unresolved = assess_questions(
        store,
        [
            QuestionAssessment(
                question_id="q-cash",
                status="ANSWERED",
                conclusion="现金利润比已由保存的合并口径计算回答。",
            )
        ],
        references=refs,
        task=task,
        financial_citations=[
            FinancialCitation(
                question_id="q-cash", result_ref=result_ref, result_index=3
            )
        ],
    )
    assert answered == ["q-cash"] and unresolved == []


def test_completion_progress_exposes_structured_metric_and_finding_gaps(tmp_path):
    from credra_agent.planning.evidence_context import coordinator_evidence_context

    store, task, refs, _ = report_fixture(tmp_path)
    question = task.questions[0].model_copy(
        update={
            "required_metric_ids": [
                "revenue_growth",
                "receivables_growth",
                "growth_gap",
                "cash_profit_ratio",
            ],
            "minimum_verified_findings": 1,
        }
    )
    task = task.model_copy(update={"questions": [question]})

    progress = coordinator_evidence_context(store, refs, task)["completion_progress"][0]

    assert progress["missing_metric_ids"] == []
    assert progress["minimum_verified_findings"] == 1
    assert progress["verified_finding_claim_ids"] == []


def grounded_report(tmp_path, *, denied=False, invalid_assessment=False):
    from credra_agent.evidence.artifacts import write_evidence_artifacts
    from credra_agent.evidence.models import Entity
    from credra_agent.evidence.service import assemble_bundle
    from tests.test_v2_evidence import claim, document, verified

    store, task, refs, result_ref = report_fixture(
        tmp_path, data=dataset(), task=financial_task()
    )
    asserted = claim(
        subject_id=task.subject_id,
        kind="PARTY_STATEMENT",
        attributed_to="合成企业",
        statement="合成企业表示现金质量尚待调查。",
    )
    doc = document(
        text=asserted.statement, source_tags=["media"] if denied else ["exchange"]
    )
    bundle = assemble_bundle(
        as_of=task.as_of,
        entities=[Entity(entity_id=task.subject_id, legal_name=task.subject_name)],
        documents=[doc],
        evidence=[verified(doc, asserted=asserted)],
        claims=[asserted],
    )
    receipts = write_evidence_artifacts(
        store, bundle, 1, as_of=task.as_of, subject_id=task.subject_id
    )
    refs.update(receipts)
    task = task.model_copy(
        update={"source_policy": SourcePolicy(allowed=["exchange_disclosure"])}
    )
    assessment = QuestionAssessment(
        question_id="q-cash",
        status="ANSWERED",
        conclusion="模型认为可以继续研究，不能替代独立核验。",
        evidence_refs=[receipts[0]],
        claim_ids=["unknown-claim"] if invalid_assessment else [asserted.claim_id],
    )
    refs.add(
        store.write_json(
            "artifacts/agent_question_assessment_v2.json",
            {
                "schema_version": "question_assessment_v2_p22",
                "assessments": [assessment.model_dump(mode="json")],
            },
        )
    )
    report_refs = write_agent_report(
        store,
        task=task,
        references=refs,
        citations=[
            FinancialCitation(
                question_id="q-cash", result_ref=result_ref, result_index=3
            )
        ],
        coverage=Coverage(
            required_question_ids=["q-cash"],
            gap_question_ids=["q-cash"],
            review_required=True,
        ),
        status="LIMITED",
        stop_reason="NEEDS_REVIEW",
        limitations=[],
        version=2,
    )
    return store.read_json(report_refs[0]), (store.run_dir / report_refs[1]).read_text(
        encoding="utf-8"
    )


def test_report_separates_statement_receipt_inference_and_review(tmp_path):
    report, markdown = grounded_report(tmp_path)
    assert report["schema_version"] == "agent_investigation_report_v1"
    finding = report["findings"][0]
    assert (
        finding["status"] == "SUPPORTED"
        and finding["assertion_scope"] == "STATEMENT_WAS_MADE"
    )
    assert finding["receipts"][0]["fragment_ref"]
    assert report["question_answers"][0]["expression_kind"] == "MODEL_INFERENCE"
    assert report["question_answers"][0]["acceptance"] == "ACCEPTED"
    assert report["approval_status"] == "NOT_REVIEWED"
    assert "模型研判" in markdown
    assert "声明核验仅证明声明曾作出" in markdown


def test_agentic_quality_check_requires_verified_settlement_finding(tmp_path):
    report, _ = grounded_report(tmp_path)
    package = load_package(ROOT)

    checks = check_report(
        report,
        package,
        required_finding_questions={"q-settlement"},
    )

    assert {
        "check_id": "evidence.required_question:q-settlement",
        "status": "FAIL",
        "reason": "MISSING_VERIFIED_FINDING",
    } in checks


@pytest.mark.parametrize("denied,invalid", [(True, False), (False, True)])
def test_report_rejects_obsolete_or_unpermitted_assessment(tmp_path, denied, invalid):
    report, markdown = grounded_report(
        tmp_path, denied=denied, invalid_assessment=invalid
    )
    assert report["question_answers"][0]["acceptance"] == "REJECTED"
    if denied:
        assert report["findings"][0]["status"] == "UNRESOLVED"
        assert report["findings"][0]["receipts"] == []
    assert "REJECTED/ANSWERED" in markdown


def test_frozen_human_pack_does_not_auto_score_or_invent_real_counterexamples():
    package = load_package(ROOT)
    assert len(package["human_answers"]) == 3
    assert (
        package["rubric"]["scores"] is None
        and package["human_review_status"] == "NOT_REVIEWED"
    )
    assert len(package["rubric"]["dimensions"]) == 5
    assert len(package["synthetic_counterexamples"]) == 3
    assert all(
        item["fixture"].startswith("tests/fixtures/")
        for item in package["synthetic_counterexamples"]
    )
    assert "比亚迪供应商全部逾期" in package["prohibited_claims"]


def test_offline_case_replay_uses_actual_run_and_keeps_quality_unscored(tmp_path):
    project = tmp_path / "project"
    suite = project / "evals/suites"
    suite.mkdir(parents=True)
    shutil.copy2(ROOT / "evals/suites/byd_cash_quality_v2.json", suite)
    shutil.copytree(
        ROOT / "data/case_byd_cash_quality_v2/source",
        project / "data/case_byd_cash_quality_v2/source",
    )
    summary, path = run_complex_case_replay(
        project, project / "results", include_baseline=True
    )
    assert summary["technical_status"] == "PASS"
    assert summary["actual_provider_requests"] == 0
    assert (
        summary["real_model_quality"] == "NOT_RUN"
        and summary["human_review_status"] == "NOT_REVIEWED"
    )
    run = summary["runs"]["agentic_replay"]
    baseline = summary["runs"]["fixed_single_pass"]
    assert run["status"] == "LIMITED" and run["stop_reason"] == "NEEDS_REVIEW"
    assert set(run["claim_ids"]) == {
        item["proposal_id"] for item in load_package(ROOT)["offline_verifier_snapshots"]
    }
    assert set(baseline["claim_ids"]) == {
        "proposal:cash",
        "proposal:explanation",
    }
    assert set(run["claim_ids"]) - set(baseline["claim_ids"]) == {
        "proposal:pledge",
        "proposal:shift",
        "proposal:response",
    }
    assert run["material_fingerprint"] == baseline["material_fingerprint"]
    assert "search_evidence" not in {
        item["action"]["tool"] for item in [*run["actions"], *baseline["actions"]]
    }
    report = json.loads(
        Path(run["report_path"]).with_suffix(".json").read_text(encoding="utf-8")
    )
    assert check_report(report, load_package(ROOT)) == run["checks"]
    report["financial_citations"][0]["metric"]["value"] = "999"
    assert check_report(report, load_package(ROOT))[0]["status"] == "FAIL"
    assert path.is_file()


def test_real_trial_pool_reserves_unknown_usage_and_stops_before_next_call(tmp_path):
    from app.llm.gateway import StructuredModelError
    from tests.test_v2_coordinator import FixedModel, finish_decision

    model = FixedModel(
        [
            finish_decision(),
            StructuredModelError(
                "INVALID_OUTPUT", "offline failure", external_requests=1
            ),
        ]
    )
    model.request_reservation_multiplier = model.token_reservation_multiplier = 1
    authorization = offline_authorization().model_copy(update={"token_limit": 550})
    pool = PooledTrialModel(model, tmp_path, authorization)
    kwargs = {"purpose": "offline-pool-check", "payload": {}}
    pool.generate(**kwargs)
    with pytest.raises(StructuredModelError, match="offline failure"):
        pool.generate(**kwargs)
    with pytest.raises(StructuredModelError, match="shared budget exhausted"):
        pool.generate(**kwargs)
    budget = pool.ledger.budget_snapshot("complex-case-shared", authorization)
    assert budget["token_spent"] == 520 and budget["usage_uncertain"]
    assert len(model.payloads) == 2


def test_real_trial_pool_stops_if_model_result_cannot_be_archived(
    tmp_path, monkeypatch
):
    from app.llm.gateway import StructuredModelError
    from tests.test_v2_coordinator import FixedModel, finish_decision

    model = FixedModel([finish_decision()])
    model.request_reservation_multiplier = model.token_reservation_multiplier = 1
    authorization = offline_authorization()
    pool = PooledTrialModel(model, tmp_path, authorization)
    monkeypatch.setattr(
        pool.store,
        "write_json",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline disk failure")),
    )
    with pytest.raises(OSError, match="offline disk failure"):
        pool.generate(purpose="offline-persist-check", payload={})
    with pytest.raises(
        StructuredModelError, match="result is uncertain; no new dispatch"
    ):
        pool.generate(purpose="offline-persist-check", payload={})
    record = pool.ledger.budget_record("complex-case-shared", "call-1")
    assert record.status == "UNCERTAIN" and len(model.payloads) == 1


def test_fixed_real_trial_route_finishes_after_each_preselected_verification():
    from tests.test_v2_coordinator import FixedModel, finish_decision

    real = FixedModel([finish_decision()])
    real.request_reservation_multiplier = real.token_reservation_multiplier = 1
    model = FixedTrialCoordinator(load_package(ROOT), real)
    context = {
        "financial_inputs": [{"reference": "artifacts/agent_financial_input_v1.json"}],
        "financial_results": [],
        "bundles": [
            {
                "reference": "artifacts/agent_evidence_v0.json",
                "documents": [{"document_id": "byd-2025-annual"}],
                "claims": [],
            }
        ],
    }

    def call():
        return model.generate(
            payload={"evidence_context": context}, system_prompt="coordinator"
        ).output

    assert call().tool == "compute_metrics"
    context["financial_results"] = [{"reference": "result", "results": []}]
    assert [call().tool, call().tool, call().tool] == [
        "read_document",
        "verify_claim",
        "verify_claim",
    ]
    assert call().decision == "FINISH" and len(real.payloads) == 1


def test_actual_ui_message_reaches_same_task_evidence_calculations_and_report(
    tmp_path, monkeypatch
):
    import app.chainlit_app as ui
    from app.evals.complex_case_replay import SnapshotCoordinator, SnapshotVerifier
    from credra_agent.entry import delegation, service
    from credra_agent.entry.context import workspace_reference
    from credra_agent.entry.delegation import wait_for_command
    from credra_agent.entry.query import TaskQueryService
    from credra_agent.entry.store import EntryStore
    from credra_agent.runtime.executors import build_agentic_executor
    from tests.test_v2_entry_delegation import decision, draft
    from tests.test_v2_entry_delegation import settings as old_settings

    settings = old_settings.__wrapped__(tmp_path)
    settings.analysis_llm_max_input_chars = 30000
    policy = json.loads(settings.agent_ui_policy_path.read_text(encoding="utf-8"))
    policy.update(external_request_limit=60, token_limit=20000)
    policy["limits"] = offline_authorization().limits.model_dump(mode="json")
    settings.agent_ui_policy_path.write_text(json.dumps(policy), encoding="utf-8")
    case = "case_byd_cash_quality_v2"
    shutil.copytree(
        ROOT / "data" / case / "source", settings.data_dir / case / "source"
    )
    package = load_package(ROOT)
    verifier = SnapshotVerifier(package)

    class BoundModel(SnapshotCoordinator):
        def generate(self, **kwargs):
            if not self.calls:
                questions = kwargs["payload"]["task_spec"]["questions"]
                by_focus = {item["focus"]: item["question_id"] for item in questions}
                mapping = {
                    item["question_id"]: by_focus[item["focus"]]
                    for item in self.package["questions"]
                }
                for row in self.rows:
                    row["question_id"] = mapping[row["question_id"]]
                self.package["questions"] = questions
            return super().generate(**kwargs)

    model = BoundModel(package)
    entry = decision(
        "prepare_investigation",
        {
            "case_id": case,
            "draft": draft(
                years=[2025],
                comparison_years=[2024],
                focus=["cash_quality", "general", "regulatory"],
                allowed_sources=None,
                as_of="2026-04-02",
            ),
        },
    )
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
    monkeypatch.setattr(service, "build_entry_model", lambda s, p: entry)
    monkeypatch.setattr(delegation, "build_investigation_model", lambda s, p: model)
    monkeypatch.setattr(
        delegation,
        "build_ui_executor",
        lambda s, task, m: build_agentic_executor(
            task, verifier_model=verifier, search_enabled=False
        ),
    )
    asyncio.run(
        ui.on_message(
            SimpleNamespace(
                id="complex-case-ui",
                content="使用case_byd_cash_quality_v2调查比亚迪2025年现金质量，以2024年作比较，资料截止2026-04-02；解释现金流变化并区分供应商结算报道和公司回应。",
            )
        )
    )
    store = EntryStore(settings.checkpoint_db_path)
    selected = store.conversation(
        session["entry_conversation_id"], workspace_reference(settings)
    )["selected_task_id"]
    command = store.latest_command(selected)
    assert (
        wait_for_command(settings.checkpoint_db_path, command["command_id"])["status"]
        == "ACKED"
    )
    view = TaskQueryService(settings, allowed_subject_ids=["002594"]).get(selected)
    assert view.summary.case_id == case and view.summary.status == "LIMITED"
    from app.runtime.tasks import run_id_for_thread
    from app.tools.artifacts import ArtifactStore

    artifacts = ArtifactStore(
        settings.data_dir / case / "runs" / run_id_for_thread(selected)
    )
    report = artifacts.read_json(view.result_refs["report_ref"].replace(".md", ".json"))
    assert len(report["findings"]) == 5 and len(report["financial_citations"]) == 4
    assert report["task_spec_version"] == view.summary.spec_version
    assert len(entry.calls) == 1 and verifier.calls == 5
    assert any("调查命令已接受" in value for value in sent)
