"""Research MCP Server backed by a configured pluggable search provider."""

from collections.abc import Iterable
from typing import Any, Literal

from fastmcp import FastMCP

from app.config import Settings, get_settings
from app.models.research import ResearchQueryResult
from app.search.content import (
    ContentFetcher,
    ContentSnapshotStore,
    build_content_fetcher,
    fetch_candidate_content,
)
from app.search.evidence import (
    deduplicate_evidence,
    evidence_from_response,
    facts_from_evidence,
    overall_verification_status,
)
from app.search.providers import SearchProvider, build_search_provider
from app.search.queries import build_search_requests
from app.search.verifier import (
    FactVerifier,
    VerificationCacheStore,
    build_fact_verifier,
    verification_counts,
    verify_candidate_evidence,
)

mcp = FastMCP("Credra Research MCP")


def _search(
    query_type: Literal["company", "industry"],
    query: str,
    *,
    subject_aliases: Iterable[str] | None = None,
    provider: SearchProvider | None = None,
    content_fetcher: ContentFetcher | None = None,
    fact_verifier: FactVerifier | None = None,
    settings: Settings | None = None,
) -> ResearchQueryResult:
    normalized = query.strip()
    if not normalized:
        raise ValueError("query cannot be empty")

    resolved_settings = settings or get_settings()
    active_provider = provider or build_search_provider(resolved_settings)
    requests = build_search_requests(query_type, normalized, subject_aliases)
    responses = [active_provider.search(request) for request in requests]
    is_mock = active_provider.name == "mock"
    evidence = deduplicate_evidence(
        [
            item
            for response in responses
            for item in evidence_from_response(
                response,
                min_relevance_score=resolved_settings.search_min_relevance_score,
                trusted_fixture=is_mock,
            )
        ]
    )
    active_content_fetcher = content_fetcher
    if active_content_fetcher is None and provider is None:
        active_content_fetcher = build_content_fetcher(resolved_settings)
    content_store = ContentSnapshotStore(resolved_settings.search_content_snapshot_dir)
    evidence, content_fetch_status = fetch_candidate_content(
        evidence,
        fetcher=active_content_fetcher,
        snapshot_store=content_store,
        max_candidates=resolved_settings.search_fetch_max_candidates,
        max_concurrency=resolved_settings.search_fetch_max_concurrency,
    )
    active_fact_verifier = fact_verifier
    if (
        active_fact_verifier is None
        and provider is None
        and any(item.evidence_stage == "CANDIDATE" for item in evidence)
    ):
        active_fact_verifier = build_fact_verifier(resolved_settings)
    evidence, verification_execution_status = verify_candidate_evidence(
        evidence,
        verifier=active_fact_verifier,
        content_store=content_store,
        cache_store=VerificationCacheStore(resolved_settings.verification_snapshot_dir),
        min_confidence=resolved_settings.fact_verifier_min_confidence,
        max_candidates=resolved_settings.fact_verifier_max_candidates,
        max_input_chars=resolved_settings.fact_verifier_max_input_chars,
    )
    candidate_evidence = [
        item for item in evidence if item.evidence_stage in {"CANDIDATE", "VERIFIED"}
    ]
    facts = facts_from_evidence(evidence)
    fetched_content_count = sum(
        item.fetched_content is not None and item.fetched_content.status == "SUCCESS"
        for item in evidence
    )
    failed_content_count = sum(
        item.fetched_content is not None
        and item.fetched_content.status in {"FAILED", "SKIPPED"}
        for item in evidence
    )
    completed_verification_count, failed_verification_count = verification_counts(
        evidence
    )
    return ResearchQueryResult(
        query_type=query_type,
        query=" | ".join(request.query for request in requests),
        found=bool(evidence),
        facts=facts,
        evidence=evidence,
        candidate_evidence=candidate_evidence,
        raw_result_count=sum(len(response.items) for response in responses),
        rejected_result_count=sum(
            item.evidence_stage == "REJECTED" for item in evidence
        ),
        candidate_found=bool(candidate_evidence),
        content_fetch_status=content_fetch_status,
        fetched_content_count=fetched_content_count,
        failed_content_count=failed_content_count,
        verification_execution_status=verification_execution_status,
        completed_verification_count=completed_verification_count,
        failed_verification_count=failed_verification_count,
        verification_status=overall_verification_status(evidence),
        source=active_provider.name,
    )


@mcp.tool
def search_company(company_name: str) -> dict[str, Any]:
    """Search auditable external evidence for a company."""

    return _search("company", company_name).model_dump(mode="json")


@mcp.tool
def search_industry(industry: str) -> dict[str, Any]:
    """Search auditable external evidence for an industry."""

    return _search("industry", industry).model_dump(mode="json")


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
