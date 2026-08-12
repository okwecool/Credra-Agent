"""Day 2 artifact-backed workflow with state-driven conditional routing."""

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt

from app.agents.document import normalize_company_documents
from app.agents.financial import analyze_financials
from app.agents.research import research_company_and_industry
from app.agents.risk import analyze_risk
from app.config import Settings
from app.graph.routing import (
    route_after_approval,
    route_after_financial,
    route_after_risk,
)
from app.graph.state import AgentState
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis, FinancialStatement
from app.models.research import ResearchResult
from app.models.risk import RiskAnalysis
from app.models.trace import TraceStatus
from app.report import render_credit_report
from app.runtime.fault import ResearchFaultInjector
from app.runtime.tracing import TimedTrace, TraceWriter
from app.tools.anomalies import detect_anomalies
from app.tools.artifacts import ArtifactStore


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def build_workflow(
    case_dir: Path,
    settings: Settings,
    research_client: Any | None = None,
    checkpointer: Any | None = None,
    trace_writer: TraceWriter | None = None,
    fault_injector: ResearchFaultInjector | None = None,
) -> CompiledStateGraph:
    """Build the graph with case-scoped artifact dependencies."""

    case_dir = case_dir.resolve()
    source_dir = case_dir / "source"
    artifacts = ArtifactStore(case_dir)
    trace = trace_writer or TraceWriter(settings.trace_dir)
    fault = fault_injector or ResearchFaultInjector(
        settings.trace_dir / ".fault_state", settings.research_fail_first
    )

    def traced(node_name: str, node: Callable[[AgentState], dict[str, Any]]):
        def wrapped(state: AgentState) -> dict[str, Any]:
            trace.instant(
                task_id=state["task_id"],
                node=node_name,
                event_type="NODE_START",
                input_summary=f"case_id={state['case_id']}",
            )
            timer = TimedTrace()
            try:
                result = node(state)
            except Exception as exc:
                end_time, latency_ms = timer.finish()
                trace.write(
                    task_id=state["task_id"],
                    node=node_name,
                    event_type="NODE_END",
                    status=TraceStatus.FAILED,
                    start_time=timer.start_time,
                    end_time=end_time,
                    latency_ms=latency_ms,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise
            end_time, latency_ms = timer.finish()
            trace.write(
                task_id=state["task_id"],
                node=node_name,
                event_type="NODE_END",
                status=TraceStatus.SUCCESS,
                start_time=timer.start_time,
                end_time=end_time,
                latency_ms=latency_ms,
                output_summary="keys=" + ",".join(sorted(result)),
            )
            return result

        return wrapped

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

    def research_node(state: AgentState) -> dict[str, Any]:
        if state["company_artifact"] is None:
            raise ValueError("company artifact is required before research")
        company = CompanyProfile.model_validate(
            artifacts.read_json(state["company_artifact"])
        )
        research = research_company_and_industry(
            state["task_id"],
            company,
            state["anomaly_flags"],
            settings.max_retry,
            trace,
            fault,
            research_client,
        )
        reference = artifacts.next_version_reference(
            "research_result", state["research_artifact"]
        )
        reference = artifacts.write_json(reference, research)
        return {
            "current_node": "research",
            "research_artifact": reference,
            "external_research_incomplete": research.external_research_incomplete,
        }

    def risk_node(state: AgentState) -> dict[str, Any]:
        if state["financial_artifact"] is None:
            raise ValueError("financial artifact is required before risk analysis")
        financial = FinancialAnalysis.model_validate(
            artifacts.read_json(state["financial_artifact"])
        )
        research = (
            ResearchResult.model_validate(
                artifacts.read_json(state["research_artifact"])
            )
            if state["research_artifact"]
            else None
        )
        risk = analyze_risk(financial, settings, research)
        reference = artifacts.next_version_reference(
            "risk_analysis", state["risk_artifact"]
        )
        reference = artifacts.write_json(reference, risk)
        return {
            "status": (
                "WAITING_APPROVAL"
                if risk.risk_level.value in ("MEDIUM", "HIGH")
                else "RUNNING"
            ),
            "current_node": "risk",
            "risk_artifact": reference,
            "risk_level": risk.risk_level.value,
        }

    def approval_node(state: AgentState) -> dict[str, Any]:
        if state["risk_artifact"] is None:
            raise ValueError("risk artifact is required before approval")
        risk = RiskAnalysis.model_validate(artifacts.read_json(state["risk_artifact"]))
        trace.instant(
            task_id=state["task_id"],
            node="approval",
            event_type="INTERRUPT",
            input_summary=f"risk_level={risk.risk_level.value}",
            output_summary="waiting for approve or research",
        )
        decision = interrupt(
            {
                "task_id": state["task_id"],
                "risk_level": risk.risk_level.value,
                "risk_flags": [
                    flag.model_dump(mode="json") for flag in risk.risk_flags
                ],
                "allowed_decisions": ["approve", "research"],
            }
        )
        if not isinstance(decision, dict):
            raise TypeError("human decision payload must be an object")
        action = decision.get("decision")
        if action not in ("approve", "research"):
            raise ValueError("human decision must be approve or research")
        comment = decision.get("comment")
        if comment is not None and not isinstance(comment, str):
            raise ValueError("human comment must be a string")
        return {
            "status": "RUNNING",
            "current_node": "approval",
            "human_decision": action,
            "human_comment": comment,
        }

    def report_node(state: AgentState) -> dict[str, Any]:
        required = (
            state["company_artifact"],
            state["financial_artifact"],
            state["risk_artifact"],
        )
        if any(reference is None for reference in required):
            raise ValueError("company, financial and risk artifacts are required")
        company = CompanyProfile.model_validate(
            artifacts.read_json(state["company_artifact"])
        )
        financial = FinancialAnalysis.model_validate(
            artifacts.read_json(state["financial_artifact"])
        )
        risk = RiskAnalysis.model_validate(artifacts.read_json(state["risk_artifact"]))
        report = render_credit_report(
            company,
            financial,
            risk,
            state["human_comment"],
            state["external_research_incomplete"],
        )
        output_path = case_dir / "output" / "credit_report.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        return {
            "status": "COMPLETED",
            "current_node": "report",
        }

    builder = StateGraph(AgentState)
    builder.add_node("document", traced("document", document_node))
    builder.add_node("financial", traced("financial", financial_node))
    builder.add_node("research", traced("research", research_node))
    builder.add_node("risk", traced("risk", risk_node))
    builder.add_node("approval", traced("approval", approval_node))
    builder.add_node("report", traced("report", report_node))
    builder.add_edge(START, "document")
    builder.add_edge("document", "financial")
    builder.add_conditional_edges(
        "financial",
        route_after_financial,
        {"research": "research", "risk": "risk"},
    )
    builder.add_edge("research", "risk")
    builder.add_conditional_edges(
        "risk",
        route_after_risk,
        {"approval": "approval", "report": "report"},
    )
    builder.add_conditional_edges(
        "approval",
        route_after_approval,
        {"research": "research", "report": "report"},
    )
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)


def load_risk_artifact(case_dir: Path, reference: str) -> RiskAnalysis:
    return RiskAnalysis.model_validate(ArtifactStore(case_dir).read_json(reference))
