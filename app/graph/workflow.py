"""Day 2 artifact-backed workflow with state-driven conditional routing."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agents.document import normalize_company_documents
from app.agents.financial import analyze_financials
from app.agents.risk import analyze_risk
from app.config import Settings
from app.graph.routing import route_after_financial
from app.graph.state import AgentState
from app.models.financial import FinancialAnalysis, FinancialStatement
from app.models.risk import RiskAnalysis
from app.tools.anomalies import detect_anomalies
from app.tools.artifacts import ArtifactStore

Node = Callable[[AgentState], dict[str, Any]]


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def build_workflow(case_dir: Path, settings: Settings) -> CompiledStateGraph:
    """Build the graph with case-scoped artifact dependencies."""

    case_dir = case_dir.resolve()
    source_dir = case_dir / "source"
    artifacts = ArtifactStore(case_dir)

    def document_node(_: AgentState) -> dict[str, Any]:
        company = normalize_company_documents(source_dir)
        reference = artifacts.write_json("artifacts/company_profile_v1.json", company)
        return {
            "status": "RUNNING",
            "current_node": "document",
            "company_artifact": reference,
        }

    def financial_node(state: AgentState) -> dict[str, Any]:
        statement = FinancialStatement.model_validate(
            _read_json(source_dir / "financial_statement.json")
        )
        analysis = analyze_financials(state["case_id"], statement)
        reference = artifacts.write_json(
            "artifacts/financial_analysis_v1.json", analysis
        )
        return {
            "current_node": "financial",
            "financial_artifact": reference,
            "anomaly_flags": detect_anomalies(analysis, settings),
        }

    def research_node(_: AgentState) -> dict[str, Any]:
        # Day 3 replaces this path marker with MCP-backed research.
        return {"current_node": "research"}

    def risk_node(state: AgentState) -> dict[str, Any]:
        if state["financial_artifact"] is None:
            raise ValueError("financial artifact is required before risk analysis")
        financial = FinancialAnalysis.model_validate(
            artifacts.read_json(state["financial_artifact"])
        )
        risk = analyze_risk(financial, settings)
        reference = artifacts.write_json("artifacts/risk_analysis_v1.json", risk)
        return {
            "status": "COMPLETED",
            "current_node": "risk",
            "risk_artifact": reference,
            "risk_level": risk.risk_level.value,
        }

    builder = StateGraph(AgentState)
    builder.add_node("document", document_node)
    builder.add_node("financial", financial_node)
    builder.add_node("research", research_node)
    builder.add_node("risk", risk_node)
    builder.add_edge(START, "document")
    builder.add_edge("document", "financial")
    builder.add_conditional_edges(
        "financial",
        route_after_financial,
        {"research": "research", "risk": "risk"},
    )
    builder.add_edge("research", "risk")
    builder.add_edge("risk", END)
    return builder.compile()


def load_risk_artifact(case_dir: Path, reference: str) -> RiskAnalysis:
    return RiskAnalysis.model_validate(ArtifactStore(case_dir).read_json(reference))
