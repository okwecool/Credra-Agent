"""M2 search provider, snapshot, query, and evidence tests."""

from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.models.search import SearchRequest, SearchResponse
from app.search.evidence import classify_source_tier, evidence_from_response
from app.search.providers import (
    SearchAuthenticationError,
    SearchConfigurationError,
    SearchProviderError,
    SearchQuotaError,
    SearchRateLimitError,
    SnapshotSearchProvider,
    SnapshotStore,
    TavilySearchProvider,
    build_search_provider,
    register_search_provider,
)
from app.search.queries import build_search_requests


def _request() -> SearchRequest:
    return SearchRequest(
        query_type="company",
        subject="比亚迪股份有限公司",
        category="regulatory",
        query='"比亚迪股份有限公司" 监管处罚',
    )


def _tavily(
    handler: httpx.MockTransport,
    *,
    snapshot_store: SnapshotStore | None = None,
) -> TavilySearchProvider:
    return TavilySearchProvider(
        api_key="test-key",
        base_url="https://api.tavily.com",
        search_depth="basic",
        max_results=5,
        timeout_seconds=15,
        time_range="year",
        snapshot_store=snapshot_store,
        client=httpx.Client(transport=handler),
    )


def test_deterministic_query_templates_cover_m2_baseline() -> None:
    company = build_search_requests("company", "比亚迪股份有限公司")
    industry = build_search_requests("industry", "汽车制造业")

    assert [item.query for item in company] == [
        '"比亚迪股份有限公司" 经营异常',
        '"比亚迪股份有限公司" 监管处罚',
        '"比亚迪股份有限公司" 诉讼 仲裁',
        '"比亚迪股份有限公司" 债务 逾期',
        '"比亚迪股份有限公司" 财务造假',
        '"比亚迪股份有限公司" 业绩预警',
        '"比亚迪股份有限公司" 实际控制人 风险',
    ]
    assert [item.query for item in industry] == ['"汽车制造业" 景气度 风险']
    assert company[0].subject_aliases == ["比亚迪"]
    detailed_industry = build_search_requests(
        "industry", "汽车制造业（新能源汽车、动力电池及相关业务）"
    )
    assert detailed_industry[0].subject_aliases == ["汽车制造业"]


def test_tavily_maps_real_url_and_never_sends_answer_or_raw_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        payload = __import__("json").loads(request.content)
        assert payload["include_answer"] is False
        assert payload["include_raw_content"] is False
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "监管公告",
                        "url": "https://www.szse.cn/disclosure/example.html",
                        "content": "比亚迪股份有限公司公开监管事实摘要",
                        "score": 0.91,
                        "published_date": "2026-06-30",
                    }
                ]
            },
        )

    response = _tavily(httpx.MockTransport(handler)).search(_request())
    evidence = evidence_from_response(response)

    assert response.items[0].url == "https://www.szse.cn/disclosure/example.html"
    assert evidence[0].source_domain == "szse.cn"
    assert evidence[0].source_tier == "A"
    assert evidence[0].verification_status == "UNVERIFIED"
    assert evidence[0].evidence_stage == "CANDIDATE"
    assert evidence[0].filter_reasons == []
    assert evidence[0].content_hash.startswith("sha256:")
    assert evidence[0].query_match is True


def test_trusted_source_without_query_semantics_stays_unverified() -> None:
    response = SearchResponse(
        provider="tavily",
        request=_request(),
        items=[
            {
                "title": "比亚迪股份有限公司年度报告",
                "content": "比亚迪股份有限公司披露年度财务数据。",
                "url": "https://paper.cnstock.com/annual-report.html",
                "score": 0.95,
                "category": "regulatory",
            }
        ],
    )

    evidence = evidence_from_response(response)

    assert evidence[0].source_tier == "B"
    assert evidence[0].query_match is False
    assert evidence[0].evidence_stage == "REJECTED"
    assert evidence[0].filter_reasons == ["CATEGORY_MISMATCH"]
    assert evidence[0].verification_status == "UNVERIFIED"


@pytest.mark.parametrize(
    ("status_code", "error_type"),
    [
        (401, SearchAuthenticationError),
        (429, SearchRateLimitError),
        (432, SearchQuotaError),
        (500, SearchProviderError),
    ],
)
def test_tavily_exposes_provider_failures(
    status_code: int,
    error_type: type[Exception],
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(status_code, json={"detail": "failed"})
    )
    with pytest.raises(error_type):
        _tavily(transport).search(_request())


def test_tavily_exposes_timeout_without_falling_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(SearchProviderError, match="timed out"):
        _tavily(httpx.MockTransport(handler)).search(_request())


def test_snapshot_round_trip_and_missing_snapshot_is_explicit(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    response = SearchResponse(provider="tavily", request=_request(), items=[])
    store.write(response)

    replayed = SnapshotSearchProvider(store).search(_request())
    assert replayed.provider == "snapshot"
    assert replayed.request == _request()

    missing = _request().model_copy(update={"query": "different query"})
    with pytest.raises(SearchProviderError, match="snapshot not found"):
        SnapshotSearchProvider(store).search(missing)


def test_provider_registry_is_extensible_and_unknown_provider_is_explicit() -> None:
    class CustomProvider:
        name = "custom-test"

        def search(self, request: SearchRequest) -> SearchResponse:
            return SearchResponse(provider=self.name, request=request, items=[])

    register_search_provider("custom-test", lambda _: CustomProvider(), replace=True)
    settings = Settings(_env_file=None, research_provider="custom-test")
    assert build_search_provider(settings).name == "custom-test"

    with pytest.raises(SearchConfigurationError, match="unknown research provider"):
        build_search_provider(
            Settings(_env_file=None, research_provider="missing-provider")
        )


def test_tavily_requires_api_key_and_domain_tiers_are_stable() -> None:
    with pytest.raises(SearchConfigurationError, match="TAVILY_API_KEY"):
        build_search_provider(
            Settings(
                _env_file=None,
                research_provider="tavily",
                tavily_api_key="",
            )
        )
    assert classify_source_tier("https://www.cninfo.com.cn/a") == "A"
    assert classify_source_tier("https://www.reuters.com/a") == "B"
    assert classify_source_tier("https://paper.cnstock.com/a") == "B"
    assert classify_source_tier("http://epaper.zqrb.cn/a") == "B"
    assert classify_source_tier("https://example.com/a") == "C"
