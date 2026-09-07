"""M2.2 acceptance tests for intent, semantic mapping, isolation, and failure."""

import shutil
from pathlib import Path

from app.agents.risk import analyze_risk
from app.config import Settings
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis, FinancialMetric
from app.models.research import ResearchFact, ResearchQueryResult, ResearchResult
from app.runtime.tasks import get_task_status, start_task
from app.tools.investigation import build_investigation_intent, build_query_plan

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _company() -> CompanyProfile:
    return CompanyProfile.model_validate_json(
        (PROJECT_ROOT / "data/case_risky/source/company_profile.json").read_text(
            encoding="utf-8"
        )
    )


def _metric(name: str, values: list[float]) -> FinancialMetric:
    return FinancialMetric(
        metric=name,
        years=[2024, 2025],
        values=values,
        unit="ratio",
        formula="test",
    )


def _financial() -> FinancialAnalysis:
    return FinancialAnalysis(
        case_id="case_test",
        currency="CNY",
        metrics={
            "revenue_growth": _metric("revenue_growth", [0.0, 0.1]),
            "net_profit_margin": _metric("net_profit_margin", [0.1, 0.1]),
            "current_ratio": _metric("current_ratio", [1.2, 1.1]),
            "debt_ratio": _metric("debt_ratio", [0.3, 0.6]),
            "operating_cash_flow_trend": _metric(
                "operating_cash_flow_trend", [100.0, 80.0]
            ),
        },
    )


def _empty_result(query_type: str) -> ResearchQueryResult:
    return ResearchQueryResult(
        query_type=query_type,
        query="test query",
        found=False,
        facts=[],
    )


def test_query_plan_is_deterministic_and_human_intent_changes_categories() -> None:
    anomaly_intent = build_investigation_intent(
        anomaly_flags=["DEBT_RATIO_RISING"],
        risk_flags=[],
        human_decision=None,
        human_comment=None,
    )
    repeated = build_investigation_intent(
        anomaly_flags=["DEBT_RATIO_RISING"],
        risk_flags=[],
        human_decision=None,
        human_comment=None,
    )
    regulatory_intent = build_investigation_intent(
        anomaly_flags=[],
        risk_flags=[],
        human_decision="research",
        human_comment="请补充核查监管处罚。",
    )

    debt_plan = build_query_plan(_company(), anomaly_intent)
    repeated_plan = build_query_plan(_company(), repeated)
    regulatory_plan = build_query_plan(_company(), regulatory_intent, debt_plan)

    assert debt_plan.plan_id == repeated_plan.plan_id
    assert [item.category for item in debt_plan.queries] == ["debt"]
    assert [item.category for item in regulatory_plan.queries] == ["regulatory"]
    assert regulatory_plan.added_queries and regulatory_plan.removed_queries
    assert regulatory_plan.intent.human_comment == "请补充核查监管处罚。"


def test_verified_facts_map_only_to_matching_external_risks() -> None:
    company_result = ResearchQueryResult(
        query_type="company",
        query="test query",
        found=True,
        facts=[
            ResearchFact(
                fact_id="fact:" + "1" * 64,
                subject="测试企业",
                category="regulatory",
                statement="测试企业受到监管处罚。",
                source_id="reg-1",
                source_url="https://example.com/regulatory",
                verification_status="SUPPORTED",
                relation="SUPPORTS",
            ),
            ResearchFact(
                fact_id="fact:" + "2" * 64,
                subject="测试企业",
                category="legal",
                statement="测试企业涉及重大诉讼。",
                source_id="legal-1",
                source_url="https://example.com/legal",
                verification_status="CORROBORATED",
                relation="SUPPORTS",
            ),
        ],
    )
    research = ResearchResult(
        company_name="测试企业",
        industry="测试行业",
        anomaly_flags=[],
        company_result=company_result,
        industry_result=_empty_result("industry"),
        status="PARTIAL",
        verification_status="CORROBORATED",
    )
    risk = analyze_risk(_financial(), Settings(_env_file=None), research)
    by_type = {flag.type: flag for flag in risk.risk_flags}

    assert by_type["leverage"].evidence == ["metric:debt_ratio"]
    assert by_type["cashflow"].evidence == ["metric:operating_cash_flow_trend"]
    assert "fact:" + "1" * 64 in by_type["external_regulatory"].evidence
    assert "fact:" + "2" * 64 not in by_type["external_regulatory"].evidence
    assert "fact:" + "2" * 64 in by_type["external_legal"].evidence


def test_same_case_threads_have_isolated_runs_and_reports(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(
        PROJECT_ROOT / "data/case_normal/source",
        data_dir / "case_normal/source",
    )
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        checkpoint_db_path=tmp_path / "credra.db",
        trace_dir=tmp_path / "traces",
    )
    first = start_task(thread_id="isolated-a", case_id="case_normal", settings=settings)
    second = start_task(
        thread_id="isolated-b", case_id="case_normal", settings=settings
    )

    assert first["state"]["run_id"] != second["state"]["run_id"]
    for payload in (first, second):
        run_dir = data_dir / "case_normal/runs" / payload["state"]["run_id"]
        assert (run_dir / "artifacts/risk_analysis_v1.json").is_file()
        assert (run_dir / "output/credit_report.md").is_file()
    assert not (data_dir / "case_normal/output/credit_report.md").exists()


def test_unrecoverable_node_failure_is_persisted_and_queryable(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    (data_dir / "case_broken/source").mkdir(parents=True)
    settings = Settings(
        _env_file=None,
        data_dir=data_dir,
        checkpoint_db_path=tmp_path / "credra.db",
        trace_dir=tmp_path / "traces",
    )
    started = start_task(
        thread_id="broken-001", case_id="case_broken", settings=settings
    )
    persisted = get_task_status(thread_id="broken-001", settings=settings)

    assert started["state"]["status"] == "FAILED"
    assert started["state"]["failed_node"] == "document"
    assert "FileNotFoundError" in started["state"]["error_summary"]
    assert persisted["state"]["status"] == "FAILED"
    assert persisted["state"]["failed_node"] == "document"
