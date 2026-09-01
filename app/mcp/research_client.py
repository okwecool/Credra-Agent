"""Typed FastMCP client for the Research MCP Server."""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from app.models.research import ResearchQueryResult

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ResearchServiceError(RuntimeError):
    """A recognizable temporary boundary failure eligible for later retry."""


class ResearchMCPClient:
    def __init__(
        self,
        transport: Any | None = None,
        environment: dict[str, str] | None = None,
    ) -> None:
        child_environment = dict(os.environ)
        child_environment.update(environment or {})
        self.transport = transport or StdioTransport(
            command=sys.executable,
            args=["-m", "app.mcp.research_server"],
            cwd=str(PROJECT_ROOT),
            env=child_environment,
        )

    @staticmethod
    def _parse_result(result: Any) -> ResearchQueryResult:
        payload = result.structured_content
        if payload is None:
            raise ResearchServiceError("MCP tool returned no structured content")
        if "result" in payload and len(payload) == 1:
            payload = payload["result"]
        return ResearchQueryResult.model_validate(payload)

    async def search_company(self, company_name: str) -> ResearchQueryResult:
        try:
            async with Client(self.transport) as client:
                result = await client.call_tool(
                    "search_company", {"company_name": company_name}
                )
            return self._parse_result(result)
        except ResearchServiceError:
            raise
        except Exception as exc:
            raise ResearchServiceError(f"company research failed: {exc}") from exc

    async def search_industry(self, industry: str) -> ResearchQueryResult:
        try:
            async with Client(self.transport) as client:
                result = await client.call_tool(
                    "search_industry", {"industry": industry}
                )
            return self._parse_result(result)
        except ResearchServiceError:
            raise
        except Exception as exc:
            raise ResearchServiceError(f"industry research failed: {exc}") from exc

    async def search_company_and_industry(
        self,
        company_name: str,
        industry: str,
    ) -> tuple[ResearchQueryResult, ResearchQueryResult]:
        """Call both tools through one initialized MCP session."""

        try:
            async with Client(self.transport) as client:
                company_result = await client.call_tool(
                    "search_company", {"company_name": company_name}
                )
                industry_result = await client.call_tool(
                    "search_industry", {"industry": industry}
                )
            return self._parse_result(company_result), self._parse_result(
                industry_result
            )
        except ResearchServiceError:
            raise
        except Exception as exc:
            raise ResearchServiceError(f"combined research failed: {exc}") from exc


def run_async(coroutine: Any) -> Any:
    """Run MCP I/O from the current synchronous LangGraph node."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise ResearchServiceError(
        "sync research client cannot run inside an active event loop"
    )
