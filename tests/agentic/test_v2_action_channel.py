"""P11 typed action registry and exact MCP search parameter channel."""

import hashlib
from datetime import date

import pytest

from app.config import Settings
from app.mcp.research_client import ResearchMCPClient
from app.mcp.research_server import _search_evidence
from app.mcp.research_server import mcp as research_mcp
from app.models.search import SearchRequest, SearchResponse
from app.search.providers import SnapshotStore
from credra_agent.execution.models import Action, SearchEvidenceArgs
from credra_agent.execution.registry import ToolUnavailableError, default_registry
from credra_agent.intent.models import Period, SourcePolicy


def search_args() -> SearchEvidenceArgs:
    return SearchEvidenceArgs(
        query="比亚迪 经营现金流 回款 澄清",
        subject_id="002594",
        subject_name="比亚迪股份有限公司",
        period=Period(start=date(2025, 1, 1), end=date(2025, 12, 31)),
        source_policy=SourcePolicy(
            preferred=["exchange_disclosure"],
            allowed=["exchange_disclosure", "media"],
            denied=["social_media"],
        ),
        category="cash_quality",
    )


class CaptureProvider:
    name = "capture"

    def __init__(self) -> None:
        self.requests: list[SearchRequest] = []

    def search(self, request: SearchRequest) -> SearchResponse:
        self.requests.append(request)
        return SearchResponse(provider=self.name, request=request, items=[])


def test_registry_uses_tool_specific_schema_and_marks_future_tools_unavailable() -> (
    None
):
    registry = default_registry()
    validated = registry.validate(
        "search_evidence", search_args().model_dump(mode="json")
    )

    assert isinstance(validated, SearchEvidenceArgs)
    assert all(item["read_only"] for item in registry.catalog())
    with pytest.raises(ToolUnavailableError, match="planned for V2-3"):
        registry.validate(
            "compare_peers",
            {"subject_ids": ["a", "b"], "input_refs": ["i"], "metric_ids": ["m"]},
        )
    with pytest.raises(ValueError):
        Action(
            action_id="bad",
            task_spec_version=1,
            plan_version=1,
            tool="search_evidence",
            arguments={"query": "missing all scoped parameters"},
            expected_observation="none",
            reason_summary="invalid fixture",
        )


def test_server_passes_exact_query_subject_period_and_policy_to_provider(
    tmp_path,
) -> None:
    provider = CaptureProvider()
    arguments = search_args()

    result = _search_evidence(
        arguments,
        provider=provider,
        settings=Settings(
            _env_file=None,
            content_fetch_provider="disabled",
            search_content_snapshot_dir=tmp_path / "content",
            verification_snapshot_dir=tmp_path / "verification",
        ),
    )

    assert result.query == arguments.query
    assert len(provider.requests) == 1
    received = provider.requests[0]
    assert received.query == arguments.query
    assert received.subject_id == arguments.subject_id
    assert received.subject == arguments.subject_name
    assert received.period == arguments.period
    assert received.source_policy == arguments.source_policy


@pytest.mark.asyncio
async def test_client_and_mcp_preserve_complete_custom_request(monkeypatch) -> None:
    from app.mcp import research_server
    from app.models.research import ResearchQueryResult

    received: list[SearchEvidenceArgs] = []

    def fake_search(arguments: SearchEvidenceArgs) -> ResearchQueryResult:
        received.append(arguments)
        return ResearchQueryResult(
            query_type="company",
            query=arguments.query,
            found=False,
            facts=[],
            source="capture",
        )

    monkeypatch.setattr(research_server, "_search_evidence", fake_search)
    result = await ResearchMCPClient(research_mcp).search_evidence(search_args())

    assert result.query == search_args().query
    assert received == [search_args()]


def test_existing_search_request_contract_remains_backward_compatible() -> None:
    request = SearchRequest(
        query_type="company",
        subject="比亚迪股份有限公司",
        category="regulatory",
        query='"比亚迪股份有限公司" 监管处罚',
    )

    assert request.subject_id is None
    assert request.period is None
    assert request.source_policy is None
    legacy_canonical = (
        '{"query_type":"company","subject":"比亚迪股份有限公司",'
        '"category":"regulatory","query":"\\"比亚迪股份有限公司\\" 监管处罚"}'
    )
    assert (
        SnapshotStore.key(request)
        == hashlib.sha256(legacy_canonical.encode("utf-8")).hexdigest()
    )
