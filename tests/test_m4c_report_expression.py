"""M4-C1 validated model-expression projection into the fixed report."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.config import Settings
from app.llm.research_analysis import build_research_analysis
from app.mcp.research_client import ResearchMCPClient
from app.mcp.research_server import mcp as research_mcp
from app.models.analysis import RiskNarrativeArtifact, RiskNarrativeExplanation
from app.models.risk import RiskAnalysis, RiskFlag, RiskLevel
from app.report import render_credit_report
from app.report_expression import build_report_expression
from app.runtime.tasks import resume_task, start_task
from tests.test_m4b_research_analysis import (
    _MainChainModel,
    _plan,
    _research,
    _ResearchAnalysisModel,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _risk() -> RiskAnalysis:
    return RiskAnalysis(
        risk_level=RiskLevel.HIGH,
        risk_flags=[
            RiskFlag(
                type="liquidity",
                severity=RiskLevel.HIGH,
                description="期末流动比率低于 1。",
                evidence=["metric:current_ratio"],
            )
        ],
        requires_human_review=True,
        summary="短期偿债能力承压。",
    )


def _narrative(*, unsafe: bool = False) -> RiskNarrativeArtifact:
    return RiskNarrativeArtifact(
        risk_level="HIGH",
        mode="llm",
        execution_status="COMPLETE",
        model_name="mock-qwen",
        prompt_version="risk-narrative-v1",
        overall_summary=(
            "详见 https://evil.example，违约概率为 99%。"
            if unsafe
            else "流动比率低于 1，短期偿债能力承压。"
        ),
        summary_evidence_ids=["metric:current_ratio"],
        explanations=[
            RiskNarrativeExplanation(
                risk_id="risk:1:liquidity",
                explanation="期末流动比率低于 1。",
                evidence_ids=["metric:current_ratio"],
            )
        ],
    )


def test_validated_projection_accepts_cited_expression_and_unexecuted_query() -> None:
    plan = _plan()
    research = _research()
    analysis = build_research_analysis(
        research,
        plan,
        analysis_mode="llm",
        model=_ResearchAnalysisModel(
            proposal_factory=lambda payload: [
                {
                    "query_type": "company",
                    "category": "debt",
                    "query": '"测试企业" 债务 展期',
                    "reference_ids": [payload["evidence_records"][0]["evidence_id"]],
                    "rationale": "围绕已核验债务证据补充人工复核线索。",
                }
            ]
        ),
        model_name="mock-qwen",
    )

    expression = build_report_expression(
        _risk(),
        analysis_mode="llm",
        research=research,
        plan=plan,
        risk_narrative=_narrative(),
        evidence_summary=analysis.evidence_summary,
        query_proposal=analysis.query_proposal,
    )

    assert expression.execution_status == "COMPLETE"
    assert expression.risk_explanations
    assert expression.evidence_items
    assert expression.evidence_gaps
    assert expression.query_suggestions[0].execution_status == "NOT_EXECUTED"
    assert expression.unsupported_claims == []


def test_unsupported_url_and_number_are_removed_from_report_projection() -> None:
    expression = build_report_expression(
        _risk(),
        analysis_mode="llm",
        risk_narrative=_narrative(unsafe=True),
    )

    assert expression.execution_status == "PARTIAL"
    assert expression.risk_overall_summary is None
    assert expression.risk_explanations
    assert {claim.reason for claim in expression.unsupported_claims} == {
        "UNSUPPORTED_NUMBER",
        "UNSUPPORTED_URL",
    }

    source = PROJECT_ROOT / "data/case_risky/source"
    from app.agents.document import normalize_company_documents
    from app.agents.financial import analyze_financials
    from app.models.financial import FinancialStatement

    company = normalize_company_documents(source)
    financial = analyze_financials(
        "case_risky",
        FinancialStatement.model_validate_json(
            (source / "financial_statement.json").read_text(encoding="utf-8")
        ),
    )
    report = render_credit_report(
        company, financial, _risk(), report_expression=expression
    )

    assert "https://evil.example" not in report
    assert "99%" not in report
    assert "UNSUPPORTED_URL=1" in report
    assert "期末流动比率低于 1。" in report


def test_unknown_fact_is_rejected_by_the_second_pass() -> None:
    plan = _plan()
    research = _research()
    analysis = build_research_analysis(
        research,
        plan,
        analysis_mode="llm",
        model=_ResearchAnalysisModel(),
        model_name="mock-qwen",
    )
    summary = analysis.evidence_summary.model_copy(deep=True)
    summary.entries[0].fact_id = "fact:" + "f" * 64

    expression = build_report_expression(
        _risk(),
        analysis_mode="llm",
        research=research,
        plan=plan,
        evidence_summary=summary,
    )

    assert expression.execution_status == "PARTIAL"
    assert expression.evidence_items == []
    assert any(
        claim.component == "evidence_summary" and claim.reason == "UNSUPPORTED_FACT"
        for claim in expression.unsupported_claims
    )


def test_invalid_optional_source_falls_back_without_raw_content() -> None:
    expression = build_report_expression(
        _risk(),
        analysis_mode="llm",
        invalid_sources=("risk_narrative", "evidence_summary"),
    )

    assert expression.execution_status == "FALLBACK"
    assert expression.risk_overall_summary is None
    assert expression.evidence_items == []
    assert {
        (item.component, item.reason) for item in expression.unsupported_claims
    } == {
        ("risk_narrative", "SOURCE_INVALID"),
        ("evidence_summary", "SOURCE_INVALID"),
    }


def test_main_chain_builds_report_expression_with_bounded_report_draft_call(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(
        PROJECT_ROOT / "data/case_risky/source",
        data_dir / "case_risky/source",
    )
    settings = Settings(
        _env_file=None,
        analysis_mode="llm",
        model_name="mock-qwen",
        model_api_key="test-key",
        data_dir=data_dir,
        checkpoint_db_path=tmp_path / "credra.db",
        trace_dir=tmp_path / "traces",
    )
    model = _MainChainModel()
    thread_id = "m4c1-main-chain"

    started = start_task(
        thread_id=thread_id,
        case_id="case_risky",
        settings=settings,
        research_client=ResearchMCPClient(research_mcp),
        analysis_model=model,
    )
    completed = resume_task(
        thread_id=thread_id,
        decision="approve",
        comment="已人工复核，生成最终报告。",
        settings=settings,
        research_client=ResearchMCPClient(research_mcp),
        analysis_model=model,
    )

    assert started["state"]["status"] == "WAITING_APPROVAL"
    assert completed["state"]["status"] == "COMPLETED"
    assert completed["state"]["report_expression_artifact"] == (
        "artifacts/report_expression_v1.json"
    )
    assert model.calls == [
        "evidence_summary_query_proposal",
        "risk_narrative",
        "report_draft",
    ]
    assert completed["state"]["report_draft_artifact"] == (
        "artifacts/report_draft_v1.json"
    )
    run_dir = data_dir / "case_risky/runs" / completed["state"]["run_id"]
    expression = json.loads(
        (run_dir / "artifacts/report_expression_v1.json").read_text(encoding="utf-8")
    )
    report = (run_dir / "output/credit_report.md").read_text(encoding="utf-8")
    assert expression["execution_status"] in {"COMPLETE", "PARTIAL"}
    assert "受约束 LLM 风险解释" in report
    assert "受约束 Evidence Summary" in report
    assert "模型表达审计" in report
    assert "Query Proposal 仅供人工审核" in report
