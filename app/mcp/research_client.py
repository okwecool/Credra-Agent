"""Typed FastMCP client for the Research MCP Server."""

import asyncio
import os
import sys
from pathlib import Path
from typing import Any

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from app.models.research import ResearchQueryResult
from credra_agent.observability.events import CONTEXT
from credra_agent.observability.instrumentation import tool_call
from credra_agent.observability.runtime import child_environment, current

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
        child_environment.update(_log_environment())
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
        payload = dict(payload)
        logging_available = payload.pop("_service_log_available", True)
        if logging_available is False and current():
            service = current()
            service.healthy = False
            if service.collector:
                service.collector.fail()
        return ResearchQueryResult.model_validate(payload)

    def _session_transport(self):
        if isinstance(self.transport, StdioTransport) and current():
            return StdioTransport(
                command=self.transport.command,
                args=self.transport.args,
                cwd=self.transport.cwd,
                env={**(self.transport.env or {}), **_log_environment()},
                keep_alive=False,
                log_file=self.transport.log_file,
            )
        return self.transport

    @tool_call
    async def search_company(
        self, company_name: str, categories: list[str] | None = None
    ) -> ResearchQueryResult:
        try:
            async with Client(self._session_transport()) as client:
                result = await client.call_tool(
                    "search_company",
                    {
                        "company_name": company_name,
                        "categories": categories,
                        **_diagnostics(),
                    },
                )
            return self._parse_result(result)
        except ResearchServiceError:
            raise
        except Exception as exc:
            raise ResearchServiceError(f"company research failed: {exc}") from exc

    @tool_call
    async def search_industry(
        self, industry: str, categories: list[str] | None = None
    ) -> ResearchQueryResult:
        try:
            async with Client(self._session_transport()) as client:
                result = await client.call_tool(
                    "search_industry",
                    {"industry": industry, "categories": categories, **_diagnostics()},
                )
            return self._parse_result(result)
        except ResearchServiceError:
            raise
        except Exception as exc:
            raise ResearchServiceError(f"industry research failed: {exc}") from exc

    @tool_call
    async def search_company_and_industry(
        self,
        company_name: str,
        industry: str,
    ) -> tuple[ResearchQueryResult, ResearchQueryResult]:
        """Call both tools through one initialized MCP session."""

        try:
            async with Client(self._session_transport()) as client:
                company_result = await client.call_tool(
                    "search_company", {"company_name": company_name, **_diagnostics()}
                )
                industry_result = await client.call_tool(
                    "search_industry", {"industry": industry, **_diagnostics()}
                )
            return self._parse_result(company_result), self._parse_result(
                industry_result
            )
        except ResearchServiceError:
            raise
        except Exception as exc:
            raise ResearchServiceError(f"combined research failed: {exc}") from exc


def _log_environment():
    return child_environment()


def _diagnostics():
    return {"diagnostic_context": CONTEXT.get() or {}} if current() else {}


def run_async(coroutine: Any) -> Any:
    """Run MCP I/O from the current synchronous LangGraph node."""

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    raise ResearchServiceError(
        "sync research client cannot run inside an active event loop"
    )
