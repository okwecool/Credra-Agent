"""Research MCP Server backed by a deterministic local dataset."""

import json
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP

from app.models.research import ResearchQueryResult

DATASET_PATH = Path(__file__).with_name("mock_research.json")
mcp = FastMCP("Credra Research MCP")


def _load_dataset() -> dict[str, dict[str, list[dict[str, str]]]]:
    with DATASET_PATH.open(encoding="utf-8") as file:
        return json.load(file)


def _search(
    query_type: Literal["company", "industry"], query: str
) -> ResearchQueryResult:
    normalized = query.strip()
    if not normalized:
        raise ValueError("query cannot be empty")
    collection = "companies" if query_type == "company" else "industries"
    facts = _load_dataset()[collection].get(normalized, [])
    return ResearchQueryResult(
        query_type=query_type,
        query=normalized,
        found=bool(facts),
        facts=facts,
    )


@mcp.tool
def search_company(company_name: str) -> dict[str, Any]:
    """Search deterministic supplemental facts for a company."""

    return _search("company", company_name).model_dump(mode="json")


@mcp.tool
def search_industry(industry: str) -> dict[str, Any]:
    """Search deterministic supplemental facts for an industry."""

    return _search("industry", industry).model_dump(mode="json")


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)
