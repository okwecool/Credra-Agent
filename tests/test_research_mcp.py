"""Protocol-level tests for the Research MCP Server and typed client."""

import sys

import pytest
from fastmcp.client.transports import StdioTransport

from app.mcp.research_client import ResearchMCPClient, ResearchServiceError
from app.mcp.research_server import mcp as research_mcp


@pytest.mark.asyncio
async def test_company_and_industry_tools_return_typed_facts() -> None:
    client = ResearchMCPClient(research_mcp)

    company = await client.search_company("迅驰供应链科技有限公司")
    industry = await client.search_industry("供应链服务")

    assert company.found is True
    assert {fact.source_id for fact in company.facts} == {
        "mock-company-xunchi-001",
        "mock-company-xunchi-002",
    }
    assert industry.found is True
    assert industry.facts[0].source_id == "mock-industry-supplychain-001"


@pytest.mark.asyncio
async def test_unknown_queries_return_explicit_empty_results() -> None:
    client = ResearchMCPClient(research_mcp)

    company = await client.search_company("不存在的企业")
    industry = await client.search_industry("不存在的行业")

    assert company.found is False
    assert company.facts == []
    assert industry.found is False
    assert industry.facts == []


@pytest.mark.asyncio
async def test_unavailable_server_raises_recognizable_boundary_error() -> None:
    transport = StdioTransport(
        command=sys.executable,
        args=["-c", "raise SystemExit(1)"],
    )
    client = ResearchMCPClient(transport)

    with pytest.raises(ResearchServiceError, match="company research failed"):
        await client.search_company("迅驰供应链科技有限公司")
