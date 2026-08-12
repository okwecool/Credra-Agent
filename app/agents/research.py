"""Research Agent that orchestrates typed MCP tool calls."""

from app.mcp.research_client import ResearchMCPClient, run_async
from app.models.company import CompanyProfile
from app.models.research import ResearchResult


async def research_company_and_industry_async(
    company: CompanyProfile,
    anomaly_flags: list[str],
    client: ResearchMCPClient,
) -> ResearchResult:
    company_result, industry_result = await client.search_company_and_industry(
        company.company_name,
        company.industry,
    )
    found_count = int(company_result.found) + int(industry_result.found)
    status = (
        "COMPLETE" if found_count == 2 else "PARTIAL" if found_count == 1 else "EMPTY"
    )
    return ResearchResult(
        company_name=company.company_name,
        industry=company.industry,
        anomaly_flags=anomaly_flags,
        company_result=company_result,
        industry_result=industry_result,
        status=status,
    )


def research_company_and_industry(
    company: CompanyProfile,
    anomaly_flags: list[str],
    client: ResearchMCPClient | None = None,
) -> ResearchResult:
    client = client or ResearchMCPClient()
    return run_async(
        research_company_and_industry_async(company, anomaly_flags, client)
    )
