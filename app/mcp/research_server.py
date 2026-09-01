"""Research MCP Server backed by a configured pluggable search provider."""

from typing import Any, Literal

from fastmcp import FastMCP

from app.config import Settings, get_settings
from app.models.research import ResearchQueryResult
from app.search.evidence import (
    deduplicate_evidence,
    evidence_from_response,
    facts_from_evidence,
    overall_verification_status,
)
from app.search.providers import SearchProvider, build_search_provider
from app.search.queries import build_search_requests

mcp = FastMCP("Credra Research MCP")


def _search(
    query_type: Literal["company", "industry"],
    query: str,
    *,
    provider: SearchProvider | None = None,
    settings: Settings | None = None,
) -> ResearchQueryResult:
    normalized = query.strip()
    if not normalized:
        raise ValueError("query cannot be empty")

    resolved_settings = settings or get_settings()
    active_provider = provider or build_search_provider(resolved_settings)
    requests = build_search_requests(query_type, normalized)
    responses = [active_provider.search(request) for request in requests]
    evidence = deduplicate_evidence(
        item
        for response in responses
        for item in evidence_from_response(
            response,
            min_relevance_score=resolved_settings.search_min_relevance_score,
        )
    )
    is_mock = active_provider.name == "mock"
    facts = facts_from_evidence(evidence, include_unverified=is_mock)
    return ResearchQueryResult(
        query_type=query_type,
        query=" | ".join(request.query for request in requests),
        found=bool(evidence),
        facts=facts,
        evidence=evidence,
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
