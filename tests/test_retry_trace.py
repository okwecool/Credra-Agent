"""Retry, deterministic fault injection, and JSONL trace tests."""

import shutil
from pathlib import Path

from app.agents.research import research_company_and_industry
from app.config import Settings
from app.graph.runner import run_workflow
from app.mcp.research_client import ResearchMCPClient, ResearchServiceError
from app.mcp.research_server import mcp as research_mcp
from app.models.company import CompanyProfile
from app.models.trace import TraceEvent
from app.runtime.fault import ResearchFaultInjector
from app.runtime.tasks import get_task_status, resume_task, start_task
from app.runtime.tracing import TraceWriter, read_trace
from app.tools.artifacts import ArtifactStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class AlwaysFailResearchClient:
    async def search_company(self, _: str):
        raise ResearchServiceError("simulated company timeout")

    async def search_industry(self, _: str):
        raise ResearchServiceError("simulated industry timeout")


def risky_company() -> CompanyProfile:
    return CompanyProfile.model_validate_json(
        (PROJECT_ROOT / "data/case_risky/source/company_profile.json").read_text(
            encoding="utf-8"
        )
    )


def copy_risky_case(tmp_path: Path) -> Path:
    case_dir = tmp_path / "data" / "case_risky"
    shutil.copytree(
        PROJECT_ROOT / "data/case_risky/source",
        case_dir / "source",
    )
    return case_dir


def test_fail_first_retries_each_tool_once_then_succeeds(tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "traces")
    fault = ResearchFaultInjector(tmp_path / "fault", fail_first=True)

    result = research_company_and_industry(
        "retry-success-001",
        risky_company(),
        ["REVENUE_CASHFLOW_DIVERGENCE"],
        max_retry=2,
        trace=trace,
        fault=fault,
        client=ResearchMCPClient(research_mcp),
    )

    assert result.status == "COMPLETE"
    assert result.external_research_incomplete is False
    events = read_trace(tmp_path / "traces/retry-success-001.jsonl")
    retries = [event for event in events if event["status"] == "RETRY"]
    successes = [
        event
        for event in events
        if event["event_type"] == "TOOL_CALL" and event["status"] == "SUCCESS"
    ]
    assert len(retries) == 2
    assert len(successes) == 2
    assert {event["input_summary"].split(";")[0] for event in retries} == {
        "tool=search_company",
        "tool=search_industry",
    }


def test_retry_exhaustion_returns_incomplete_research(tmp_path: Path) -> None:
    trace = TraceWriter(tmp_path / "traces")
    result = research_company_and_industry(
        "retry-failed-001",
        risky_company(),
        ["DEBT_RATIO_RISING"],
        max_retry=2,
        trace=trace,
        fault=ResearchFaultInjector(tmp_path / "fault", fail_first=False),
        client=AlwaysFailResearchClient(),
    )

    assert result.status == "INCOMPLETE"
    assert result.external_research_incomplete is True
    assert result.failed_tools == ["search_company", "search_industry"]
    events = read_trace(tmp_path / "traces/retry-failed-001.jsonl")
    assert len([event for event in events if event["status"] == "RETRY"]) == 4
    assert len([event for event in events if event["status"] == "FAILED"]) == 2


def test_incomplete_research_reaches_state_risk_and_report(tmp_path: Path) -> None:
    case_dir = copy_risky_case(tmp_path)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        trace_dir=tmp_path / "traces",
        checkpoint_db_path=tmp_path / "checkpoints/credra.db",
        max_retry=1,
    )
    thread_id = "incomplete-flow-001"

    started = start_task(
        thread_id=thread_id,
        case_id="case_risky",
        settings=settings,
        research_client=AlwaysFailResearchClient(),
    )

    assert started["state"]["external_research_incomplete"] is True
    run_dir = case_dir / "runs" / started["state"]["run_id"]
    store = ArtifactStore(run_dir)
    research = store.read_json(started["state"]["research_artifact"])
    risk = store.read_json(started["state"]["risk_artifact"])
    assert research["status"] == "INCOMPLETE"
    assert any(flag["type"] == "external_research" for flag in risk["risk_flags"])

    completed = resume_task(
        thread_id=thread_id,
        decision="approve",
        comment="已知调查证据不完整，人工继续。",
        settings=settings,
        research_client=AlwaysFailResearchClient(),
    )
    assert completed["state"]["status"] == "COMPLETED"
    report = (run_dir / "output/credit_report.md").read_text(encoding="utf-8")
    assert "外部调查未全部完成" in report
    assert "证据缺口" in report


def test_trace_is_valid_and_reconstructs_interrupt_resume_path(tmp_path: Path) -> None:
    copy_risky_case(tmp_path)
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        trace_dir=tmp_path / "traces",
        checkpoint_db_path=tmp_path / "credra.db",
    )
    thread_id = "trace-lifecycle-001"
    client = ResearchMCPClient(research_mcp)

    start_task(
        thread_id=thread_id,
        case_id="case_risky",
        settings=settings,
        research_client=client,
    )
    get_task_status(
        thread_id=thread_id,
        settings=settings,
        research_client=client,
    )
    resume_task(
        thread_id=thread_id,
        decision="approve",
        comment="trace approval",
        settings=settings,
        research_client=client,
    )

    events = read_trace(tmp_path / f"traces/{thread_id}.jsonl")
    validated = [TraceEvent.model_validate(event) for event in events]
    event_types = [event.event_type for event in validated]
    successful_nodes = [
        event.node
        for event in validated
        if event.event_type == "NODE_END" and event.status == "SUCCESS"
    ]
    assert "TASK_START" in event_types
    assert "TOOL_CALL" in event_types
    assert "INTERRUPT" in event_types
    assert "STATUS_QUERY" in event_types
    assert "RESUME" in event_types
    assert successful_nodes == [
        "document",
        "financial",
        "research",
        "risk",
        "approval",
        "report",
    ]
    serialized = (tmp_path / f"traces/{thread_id}.jsonl").read_text(encoding="utf-8")
    assert "MODEL_API_KEY" not in serialized
    assert "company_profile.json" not in serialized


def test_workflow_runner_still_supports_injected_trace_services(tmp_path: Path) -> None:
    case_dir = copy_risky_case(tmp_path)
    settings = Settings(_env_file=None, trace_dir=tmp_path / "traces")

    result = run_workflow(
        case_dir,
        task_id="runner-trace-001",
        settings=settings,
        research_client=ResearchMCPClient(research_mcp),
    )

    assert result.state["status"] == "WAITING_APPROVAL"
    assert (tmp_path / "traces/runner-trace-001.jsonl").is_file()
