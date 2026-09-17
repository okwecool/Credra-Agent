"""M2.1-A entity matching, deduplication, and status-separation tests."""

from pathlib import Path

from app.agents.research import research_company_and_industry
from app.models.company import CompanyProfile
from app.models.research import ResearchQueryResult
from app.models.search import SearchItem, SearchRequest, SearchResponse
from app.runtime.fault import ResearchFaultInjector
from app.runtime.tracing import TraceWriter
from app.search.evidence import (
    deduplicate_evidence,
    evidence_from_response,
    overall_verification_status,
)
from app.search.providers import SnapshotSearchProvider, SnapshotStore
from app.search.queries import build_search_requests


def test_company_alias_can_create_candidate_but_not_verified_fact() -> None:
    request = build_search_requests("company", "比亚迪股份有限公司")[3]
    response = SearchResponse(
        provider="tavily",
        request=request,
        items=[
            SearchItem(
                title="比亚迪债务风险观察",
                content="比亚迪披露债务安排，未出现逾期结论。",
                url="https://www.21jingji.com/example-byd.html",
                score=0.88,
                category="debt",
            )
        ],
    )

    evidence = evidence_from_response(response)

    assert request.subject_aliases == ["比亚迪"]
    assert evidence[0].subject_match == "ALIAS"
    assert evidence[0].category_match is True
    assert evidence[0].filter_reasons == []
    assert evidence[0].evidence_stage == "CANDIDATE"
    assert evidence[0].verification_status == "UNVERIFIED"


def test_duplicate_results_are_preserved_with_explicit_rejection_reason() -> None:
    request = SearchRequest(
        query_type="company",
        subject="比亚迪股份有限公司",
        subject_aliases=["比亚迪"],
        category="debt",
        query='"比亚迪股份有限公司" 债务 逾期',
    )
    shared = {
        "title": "比亚迪债务风险观察",
        "content": "比亚迪披露债务安排，需要关注逾期风险。",
        "url": "https://www.21jingji.com/example-duplicate.html",
        "category": "debt",
    }
    response = SearchResponse(
        provider="tavily",
        request=request,
        items=[
            SearchItem(**shared, score=0.91),
            SearchItem(**shared, score=0.81),
        ],
    )

    evidence = deduplicate_evidence(evidence_from_response(response))

    assert len(evidence) == 2
    assert [item.evidence_stage for item in evidence] == [
        "CANDIDATE",
        "REJECTED",
    ]
    assert evidence[1].filter_reasons == ["DUPLICATE_CONTENT"]


def _candidate_result(
    query_type: str,
    subject: str,
    category: str,
    content: str,
) -> ResearchQueryResult:
    request = SearchRequest(
        query_type=query_type,
        subject=subject,
        category=category,
        query=f'"{subject}" candidate test',
    )
    response = SearchResponse(
        provider="tavily",
        request=request,
        items=[
            SearchItem(
                title=content,
                content=content,
                url="https://www.21jingji.com/candidate.html",
                score=0.9,
                category=category,
            )
        ],
    )
    evidence = evidence_from_response(response)
    return ResearchQueryResult(
        query_type=query_type,
        query=request.query,
        found=True,
        facts=[],
        evidence=evidence,
        candidate_evidence=evidence,
        raw_result_count=1,
        candidate_found=True,
        verification_status=overall_verification_status(evidence),
        source="tavily",
    )


class CandidateOnlyClient:
    async def search_company(self, _: str) -> ResearchQueryResult:
        return _candidate_result(
            "company",
            "比亚迪股份有限公司",
            "debt",
            "比亚迪股份有限公司披露债务安排。",
        )

    async def search_industry(self, _: str) -> ResearchQueryResult:
        return _candidate_result(
            "industry",
            "汽车制造业",
            "industry_risk",
            "汽车制造业景气度存在下行风险。",
        )


def test_execution_complete_is_distinct_from_fact_verification(
    tmp_path: Path,
) -> None:
    result = research_company_and_industry(
        "candidate-only-001",
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
        client=CandidateOnlyClient(),
    )

    assert result.status == "COMPLETE"
    assert result.execution_status == "COMPLETE"
    assert result.verification_status == "UNVERIFIED"
    assert result.candidate_count == 2
    assert result.verified_fact_count == 0
    assert result.rejected_result_count == 0


def test_snapshot_key_ignores_filter_only_aliases() -> None:
    without_aliases = SearchRequest(
        query_type="company",
        subject="比亚迪股份有限公司",
        category="debt",
        query='"比亚迪股份有限公司" 债务 逾期',
    )
    with_aliases = without_aliases.model_copy(
        update={"subject_aliases": ["比亚迪", "002594"]}
    )

    assert SnapshotStore.key(without_aliases) == SnapshotStore.key(with_aliases)


def test_snapshot_replay_applies_current_filter_aliases(tmp_path: Path) -> None:
    stored_request = SearchRequest(
        query_type="company",
        subject="比亚迪股份有限公司",
        category="debt",
        query='"比亚迪股份有限公司" 债务 逾期',
    )
    requested = stored_request.model_copy(update={"subject_aliases": ["比亚迪"]})
    store = SnapshotStore(tmp_path)
    store.write(SearchResponse(provider="tavily", request=stored_request, items=[]))

    replayed = SnapshotSearchProvider(store).search(requested)

    assert replayed.request.subject_aliases == ["比亚迪"]
