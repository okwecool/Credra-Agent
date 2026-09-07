"""Anomaly and state-driven routing tests for the durable workflow."""

import json
import shutil
from pathlib import Path

from app.agents.financial import analyze_financials
from app.config import Settings
from app.graph.routing import route_after_financial
from app.graph.runner import run_workflow
from app.graph.state import initial_state
from app.mcp.research_client import ResearchMCPClient
from app.mcp.research_server import mcp as research_mcp
from app.models.financial import FinancialStatement
from app.tools.anomalies import (
    DEBT_RATIO_RISING,
    REVENUE_CASHFLOW_DIVERGENCE,
    detect_anomalies,
)
from app.tools.artifacts import ArtifactStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def settings() -> Settings:
    return Settings(
        _env_file=None,
        data_dir=PROJECT_ROOT / "data",
        revenue_threshold=0.20,
        cashflow_threshold=0.0,
        debt_ratio_threshold=0.15,
    )


def load_analysis(case_id: str):
    path = PROJECT_ROOT / "data" / case_id / "source" / "financial_statement.json"
    with path.open(encoding="utf-8") as file:
        statement = FinancialStatement.model_validate(json.load(file))
    return analyze_financials(case_id, statement)


def copy_case(tmp_path: Path, case_id: str) -> Path:
    destination = tmp_path / case_id
    shutil.copytree(PROJECT_ROOT / "data" / case_id / "source", destination / "source")
    return destination


def test_normal_case_has_no_anomalies_and_routes_to_risk() -> None:
    flags = detect_anomalies(load_analysis("case_normal"), settings())
    state = initial_state("task-normal", "case_normal")
    state["anomaly_flags"] = flags

    assert flags == []
    assert route_after_financial(state) == "risk"


def test_risky_case_has_expected_anomalies_and_routes_to_research() -> None:
    flags = detect_anomalies(load_analysis("case_risky"), settings())
    state = initial_state("task-risky", "case_risky")
    state["anomaly_flags"] = flags

    assert flags == [REVENUE_CASHFLOW_DIVERGENCE, DEBT_RATIO_RISING]
    assert route_after_financial(state) == "research"


def test_normal_case_workflow_uses_artifacts_and_skips_research(tmp_path: Path) -> None:
    case_dir = copy_case(tmp_path, "case_normal")
    result = run_workflow(case_dir, task_id="normal-001", settings=settings())

    assert result.execution_path == ["document", "financial", "risk", "report"]
    assert result.state["status"] == "COMPLETED"
    assert result.state["anomaly_flags"] == []
    assert result.state["risk_level"] == "LOW"
    assert result.state["research_artifact"] is None
    assert set(result.state) == set(initial_state("", ""))
    assert not any(isinstance(value, dict) for value in result.state.values())

    store = ArtifactStore(case_dir)
    assert store.read_json(result.state["company_artifact"])["company_name"]
    assert store.read_json(result.state["financial_artifact"])["metrics"]
    assert store.read_json(result.state["risk_artifact"])["risk_level"] == "LOW"


def test_risky_case_workflow_routes_through_mcp_research(tmp_path: Path) -> None:
    case_dir = copy_case(tmp_path, "case_risky")
    result = run_workflow(
        case_dir,
        task_id="risky-001",
        settings=settings(),
        research_client=ResearchMCPClient(research_mcp),
    )

    assert result.execution_path == ["document", "financial", "research", "risk"]
    assert result.state["anomaly_flags"] == [
        REVENUE_CASHFLOW_DIVERGENCE,
        DEBT_RATIO_RISING,
    ]
    assert result.state["risk_level"] == "HIGH"
    assert result.state["research_artifact"] == "artifacts/research_result_v1.json"

    store = ArtifactStore(case_dir)
    research = store.read_json(result.state["research_artifact"])
    assert research["status"] == "COMPLETE"
    assert research["company_result"]["found"] is True
    assert research["industry_result"]["found"] is False
    assert research["industry_result"]["source"] == "not_requested"
    risk = store.read_json(result.state["risk_artifact"])
    assert risk["requires_human_review"] is True
    evidence = {
        source_id for flag in risk["risk_flags"] for source_id in flag["evidence"]
    }
    assert "mock-company-xunchi-001" in evidence
    assert "mock-industry-supplychain-001" not in evidence
