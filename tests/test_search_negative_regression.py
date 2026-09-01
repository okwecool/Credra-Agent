"""Negative regression for high-scoring evidence about the wrong company."""

from pathlib import Path

from app.agents.risk import analyze_risk
from app.config import Settings
from app.mcp.research_server import _search
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis, FinancialMetric
from app.models.research import ResearchQueryResult, ResearchResult
from app.models.search import SearchRequest, SearchResponse
from app.report import render_credit_report
from app.search.evidence import deduplicate_evidence, evidence_from_response
from app.search.providers import SnapshotSearchProvider, SnapshotStore

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "search_snapshots"
    / "byd_debt_huayi_irrelevant.json"
)
IRRELEVANT_URL = (
    "https://www.21jingji.com/article/20260419/herald/"
    "f5699fc0901ca19aeb0b63ddc6fdccd5.html"
)


def load_fixture() -> SearchResponse:
    return SearchResponse.model_validate_json(FIXTURE_PATH.read_text(encoding="utf-8"))


class NegativeSnapshotProvider:
    name = "snapshot"

    def __init__(self, response: SearchResponse) -> None:
        self.response = response

    def search(self, request: SearchRequest) -> SearchResponse:
        items = self.response.items if request.category == "debt" else []
        return SearchResponse(provider=self.name, request=request, items=items)


def _metric(name: str, values: list[float]) -> FinancialMetric:
    return FinancialMetric(
        metric=name,
        years=[2024, 2025],
        values=values,
        unit="ratio",
        formula="negative regression fixture",
    )


def _financial() -> FinancialAnalysis:
    return FinancialAnalysis(
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


def _company() -> CompanyProfile:
    return CompanyProfile(
        company_name="比亚迪股份有限公司",
        industry="汽车制造业",
        registered_capital="test",
        established_date="test",
        shareholders=[],
        business_scope="test",
        major_customers=[],
        major_suppliers=[],
    )


def test_wrong_company_is_rejected_by_deterministic_filter() -> None:
    evidence = deduplicate_evidence(evidence_from_response(load_fixture()))

    assert len(evidence) == 1
    assert evidence[0].source_tier == "B"
    assert evidence[0].relevance_score > 0.9
    assert evidence[0].subject_match == "NONE"
    assert evidence[0].category_match is True
    assert evidence[0].query_match is False
    assert evidence[0].filter_reasons == ["SUBJECT_MISMATCH"]
    assert evidence[0].evidence_stage == "REJECTED"
    assert evidence[0].verification_status == "UNVERIFIED"


def test_wrong_company_snapshot_replays_with_same_rejection(tmp_path: Path) -> None:
    response = load_fixture()
    store = SnapshotStore(tmp_path)
    store.write(response)

    replayed = SnapshotSearchProvider(store).search(response.request)
    evidence = evidence_from_response(replayed)

    assert replayed.items[0].url == IRRELEVANT_URL
    assert evidence[0].filter_reasons == ["SUBJECT_MISMATCH"]
    assert evidence[0].evidence_stage == "REJECTED"


def test_wrong_company_never_reaches_facts_risk_or_report() -> None:
    company_result = _search(
        "company",
        "比亚迪股份有限公司",
        subject_aliases=["002594"],
        provider=NegativeSnapshotProvider(load_fixture()),
        settings=Settings(_env_file=None),
    )
    industry_result = ResearchQueryResult(
        query_type="industry",
        query='"汽车制造业" 景气度 风险',
        found=False,
        facts=[],
    )

    assert company_result.found is True
    assert company_result.raw_result_count == 1
    assert company_result.candidate_found is False
    assert company_result.candidate_evidence == []
    assert company_result.rejected_result_count == 1
    assert company_result.verification_status == "NOT_FOUND"
    assert company_result.facts == []

    research = ResearchResult(
        company_name="比亚迪股份有限公司",
        industry="汽车制造业",
        anomaly_flags=[],
        company_result=company_result,
        industry_result=industry_result,
        status="PARTIAL",
        verification_status="NOT_FOUND",
        rejected_result_count=1,
    )
    risk = analyze_risk(
        _financial(),
        settings=Settings(_env_file=None),
        research=research,
    )
    assert all(IRRELEVANT_URL not in flag.evidence for flag in risk.risk_flags)

    report = render_credit_report(_company(), _financial(), risk)
    assert IRRELEVANT_URL not in report
    assert "华谊兄弟" not in report
