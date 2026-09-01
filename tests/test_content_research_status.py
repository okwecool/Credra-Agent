"""Content-fetch status propagation into the Research aggregate."""

from pathlib import Path

from app.agents.research import research_company_and_industry
from app.agents.risk import analyze_risk
from app.config import Settings
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis, FinancialMetric
from app.models.research import ResearchQueryResult
from app.runtime.fault import ResearchFaultInjector
from app.runtime.tracing import TraceWriter
from app.search.content import SnapshotContentFetcher, build_content_fetcher


def _query_result(
    query_type: str,
    *,
    content_fetch_status: str = "NOT_NEEDED",
    fetched_content_count: int = 0,
    failed_content_count: int = 0,
) -> ResearchQueryResult:
    return ResearchQueryResult(
        query_type=query_type,
        query=f"{query_type} fixture",
        found=query_type == "company",
        facts=[],
        content_fetch_status=content_fetch_status,
        fetched_content_count=fetched_content_count,
        failed_content_count=failed_content_count,
        verification_status="UNVERIFIED" if query_type == "company" else "NOT_FOUND",
        source="fixture",
    )


class FetchFailureClient:
    async def search_company(self, _: str) -> ResearchQueryResult:
        return _query_result(
            "company",
            content_fetch_status="FAILED",
            failed_content_count=1,
        )

    async def search_industry(self, _: str) -> ResearchQueryResult:
        return _query_result("industry")


def test_fetch_failure_is_incomplete_but_not_a_failed_mcp_tool(
    tmp_path: Path,
) -> None:
    result = research_company_and_industry(
        "fetch-failure-001",
        CompanyProfile(
            company_name="比亚迪股份有限公司",
            industry="汽车制造业",
            registered_capital="test",
            established_date="test",
            shareholders=[],
            business_scope="test",
            major_customers=[],
            major_suppliers=[],
        ),
        anomaly_flags=[],
        max_retry=0,
        trace=TraceWriter(tmp_path / "traces"),
        fault=ResearchFaultInjector(tmp_path / "fault", fail_first=False),
        client=FetchFailureClient(),
    )

    assert result.status == "INCOMPLETE"
    assert result.execution_status == "COMPLETE"
    assert result.content_fetch_status == "FAILED"
    assert result.content_fetch_incomplete is True
    assert result.external_research_incomplete is True
    assert result.failed_content_count == 1
    assert result.failed_tools == []


class MixedFetchClient:
    async def search_company(self, _: str) -> ResearchQueryResult:
        return _query_result(
            "company",
            content_fetch_status="COMPLETE",
            fetched_content_count=1,
        )

    async def search_industry(self, _: str) -> ResearchQueryResult:
        return _query_result(
            "industry",
            content_fetch_status="FAILED",
            failed_content_count=1,
        )


def test_mixed_fetch_outcomes_aggregate_as_partial(tmp_path: Path) -> None:
    result = research_company_and_industry(
        "mixed-fetch-001",
        CompanyProfile(
            company_name="比亚迪股份有限公司",
            industry="汽车制造业",
            registered_capital="test",
            established_date="test",
            shareholders=[],
            business_scope="test",
            major_customers=[],
            major_suppliers=[],
        ),
        anomaly_flags=[],
        max_retry=0,
        trace=TraceWriter(tmp_path / "traces"),
        fault=ResearchFaultInjector(tmp_path / "fault", fail_first=False),
        client=MixedFetchClient(),
    )

    assert result.status == "INCOMPLETE"
    assert result.content_fetch_status == "PARTIAL"
    assert result.fetched_content_count == 1
    assert result.failed_content_count == 1

    def metric(name: str, values: list[float]) -> FinancialMetric:
        return FinancialMetric(
            metric=name,
            years=[2024, 2025],
            values=values,
            unit="ratio",
            formula="test fixture",
        )

    financial = FinancialAnalysis(
        case_id="case_byd_002594",
        currency="CNY_1000",
        metrics={
            "debt_ratio": metric("debt_ratio", [0.4, 0.5]),
            "operating_cash_flow_trend": metric(
                "operating_cash_flow_trend", [100.0, 50.0]
            ),
            "current_ratio": metric("current_ratio", [1.2, 0.9]),
        },
    )
    risk = analyze_risk(
        financial,
        settings=Settings(_env_file=None),
        research=result,
    )
    external_flag = next(
        flag for flag in risk.risk_flags if flag.type == "external_research"
    )

    assert external_flag.evidence == [
        "content_fetch_status:PARTIAL",
        "content_fetch_failed:1",
    ]


def test_auto_content_fetch_provider_follows_research_mode(tmp_path: Path) -> None:
    disabled = build_content_fetcher(
        Settings(
            _env_file=None,
            research_provider="mock",
            content_fetch_provider="auto",
            search_content_snapshot_dir=tmp_path,
        )
    )
    snapshot = build_content_fetcher(
        Settings(
            _env_file=None,
            research_provider="snapshot",
            content_fetch_provider="auto",
            search_content_snapshot_dir=tmp_path,
        )
    )

    assert disabled is None
    assert isinstance(snapshot, SnapshotContentFetcher)
