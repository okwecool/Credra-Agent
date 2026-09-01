"""Evidence URLs remain traceable through Research, Risk, and Report."""

from app.agents.risk import analyze_risk
from app.config import Settings
from app.mcp.research_server import _search
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis, FinancialMetric
from app.models.research import ResearchQueryResult, ResearchResult
from app.models.search import SearchItem, SearchRequest, SearchResponse
from app.report import render_credit_report


class VerifiedFixtureProvider:
    name = "mock"

    def search(self, request: SearchRequest) -> SearchResponse:
        return SearchResponse(
            provider=self.name,
            request=request,
            items=[
                SearchItem(
                    title="交易所公开公告",
                    content="比亚迪股份有限公司存在需要审阅的公开监管事项。",
                    url="https://www.szse.cn/disclosure/example.html",
                    score=0.95,
                    category=request.category,
                )
            ],
        )


def _metric(name: str, values: list[float]) -> FinancialMetric:
    return FinancialMetric(
        metric=name,
        years=[2024, 2025],
        values=values,
        unit="ratio",
        formula="test fixture",
    )


def test_supported_url_reaches_risk_artifact_and_report() -> None:
    company_result = _search(
        "company",
        "比亚迪股份有限公司",
        provider=VerifiedFixtureProvider(),
    )
    empty_industry = ResearchQueryResult(
        query_type="industry",
        query='"汽车制造业" 景气度 风险',
        found=False,
        facts=[],
    )
    research = ResearchResult(
        company_name="比亚迪股份有限公司",
        industry="汽车制造业",
        anomaly_flags=[],
        company_result=company_result,
        industry_result=empty_industry,
        status="PARTIAL",
    )
    financial = FinancialAnalysis(
        case_id="case_byd_002594",
        currency="CNY_1000",
        metrics={
            "revenue_growth": _metric("revenue_growth", [0.0, 0.1]),
            "net_profit_margin": _metric("net_profit_margin", [0.05, 0.04]),
            "current_ratio": _metric("current_ratio", [1.2, 1.1]),
            "debt_ratio": _metric("debt_ratio", [0.4, 0.6]),
            "operating_cash_flow_trend": _metric(
                "operating_cash_flow_trend", [100.0, 50.0]
            ),
        },
    )
    risk = analyze_risk(
        financial,
        settings=Settings(_env_file=None),
        research=research,
    )
    source_url = "https://www.szse.cn/disclosure/example.html"

    assert company_result.verification_status == "SUPPORTED"
    assert company_result.facts[0].source_url == source_url
    assert any(source_url in flag.evidence for flag in risk.risk_flags)

    report = render_credit_report(
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
        financial,
        risk,
    )
    assert source_url in report
