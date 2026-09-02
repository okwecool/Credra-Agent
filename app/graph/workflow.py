"""Artifact-backed workflow with routing, MCP research, and HITL."""

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from langgraph.errors import GraphInterrupt
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
from app.llm.gateway import StructuredModel, StructuredModelError, build_analysis_model
from app.llm.research_analysis import build_research_analysis
from app.llm.risk_narrative import build_risk_narrative
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis, FinancialStatement
from app.models.investigation import QueryPlan
from app.models.research import ResearchResult
from app.models.risk import RiskAnalysis
from app.models.trace import TraceStatus
from app.report import render_credit_report
from app.runtime.fault import ResearchFaultInjector
from app.runtime.tracing import TimedTrace, TraceWriter
from app.tools.anomalies import detect_anomalies
from app.tools.artifacts import ArtifactStore
from app.tools.investigation import build_investigation_intent, build_query_plan


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
    *,
    run_dir: Path | None = None,
    analysis_model: StructuredModel | None = None,
) -> CompiledStateGraph:
    """Build the graph with case inputs and isolated execution outputs."""

    case_dir = case_dir.resolve()
    source_dir = case_dir / "source"
    run_dir = (run_dir or case_dir).resolve()
    artifacts = ArtifactStore(run_dir)
    trace = trace_writer or TraceWriter(settings.trace_dir)
    fault = fault_injector or ResearchFaultInjector(
        settings.trace_dir / ".fault_state", settings.research_fail_first
    )
    active_analysis_model = analysis_model
    analysis_initialization_error: StructuredModelError | None = None
    if settings.analysis_mode == "llm" and active_analysis_model is None:
        try:
            active_analysis_model = build_analysis_model(settings)
        except StructuredModelError as exc:
            analysis_initialization_error = exc
        except (TypeError, ValueError):
            analysis_initialization_error = StructuredModelError(
                "CONFIG_ERROR", "structured analysis model configuration is invalid"
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
            except GraphInterrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - node boundary persists safe failure
                end_time, latency_ms = timer.finish()
                message = re.sub(
                    r"(?i)(api[_-]?key|authorization|bearer)(\s*[:=]\s*)\S+",
                    r"\1\2[REDACTED]",
                    str(exc),
                )[:240]
                error_summary = f"{type(exc).__name__}: {message}"
                trace.write(
                    task_id=state["task_id"],
                    node=node_name,
                    event_type="NODE_END",
                    status=TraceStatus.FAILED,
                    start_time=timer.start_time,
                    end_time=end_time,
                    latency_ms=latency_ms,
                    error=error_summary,
                )
                return {
                    "status": "FAILED",
                    "current_node": node_name,
                    "failed_node": node_name,
                    "error_summary": error_summary,
                }
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
        risk_flags = (
            RiskAnalysis.model_validate(
                artifacts.read_json(state["risk_artifact"])
            ).risk_flags
            if state["risk_artifact"]
            else []
        )
        intent = build_investigation_intent(
            anomaly_flags=state["anomaly_flags"],
            risk_flags=risk_flags,
            human_decision=state["human_decision"],
            human_comment=state["human_comment"],
        )
        previous_plan_reference = state.get("query_plan_artifact")
        previous_plan = (
            QueryPlan.model_validate(artifacts.read_json(previous_plan_reference))
            if previous_plan_reference
            else None
        )
        plan = build_query_plan(company, intent, previous_plan)
        intent_reference = artifacts.next_version_reference(
            "investigation_intent", state.get("investigation_intent_artifact")
        )
        intent_reference = artifacts.write_json(intent_reference, intent)
        plan_reference = artifacts.next_version_reference(
            "query_plan", previous_plan_reference
        )
        plan_reference = artifacts.write_json(plan_reference, plan)
        trace.instant(
            task_id=state["task_id"],
            node="research",
            event_type="QUERY_PLAN",
            input_summary=(
                f"categories={','.join(intent.categories)};"
                f"human_comment={(intent.human_comment or '')[:80]}"
            ),
            output_summary=f"plan_id={plan.plan_id};queries={len(plan.queries)}",
        )
        research = research_company_and_industry(
            state["task_id"],
            company,
            state["anomaly_flags"],
            settings.max_retry,
            trace,
            fault,
            research_client,
            plan,
        )
        reference = artifacts.next_version_reference(
            "research_result", state["research_artifact"]
        )
        reference = artifacts.write_json(reference, research)
        research_analysis_timer = TimedTrace()
        research_analysis = build_research_analysis(
            research,
            plan,
            analysis_mode=settings.analysis_mode,
            model=active_analysis_model,
            model_name=settings.analysis_model or settings.model_name,
            initialization_error=analysis_initialization_error,
        )
        evidence_summary_reference = artifacts.next_version_reference(
            "evidence_summary", state.get("evidence_summary_artifact")
        )
        evidence_summary_reference = artifacts.write_json(
            evidence_summary_reference, research_analysis.evidence_summary
        )
        query_proposal_reference = artifacts.next_version_reference(
            "query_proposal", state.get("query_proposal_artifact")
        )
        query_proposal_reference = artifacts.write_json(
            query_proposal_reference, research_analysis.query_proposal
        )
        if settings.analysis_mode == "llm":
            analysis_end, analysis_latency = research_analysis_timer.finish()
            summary = research_analysis.evidence_summary
            trace.write(
                task_id=state["task_id"],
                node="research",
                event_type=(
                    "LLM_SKIP"
                    if summary.execution_status == "NOT_NEEDED"
                    else "LLM_CALL"
                ),
                status=(
                    TraceStatus.FAILED
                    if summary.execution_status == "DEGRADED"
                    else TraceStatus.SUCCESS
                ),
                start_time=research_analysis_timer.start_time,
                end_time=analysis_end,
                latency_ms=analysis_latency,
                input_summary="purpose=evidence_summary_query_proposal",
                output_summary=(
                    f"model={summary.model_name};prompt={summary.prompt_version};"
                    f"execution={summary.execution_status};"
                    f"attempts={summary.attempts};"
                    f"input_tokens={summary.input_tokens};"
                    f"output_tokens={summary.output_tokens}"
                ),
                error=summary.error_code,
            )
        return {
            "current_node": "research",
            "research_artifact": reference,
            "investigation_intent_artifact": intent_reference,
            "query_plan_artifact": plan_reference,
            "evidence_summary_artifact": evidence_summary_reference,
            "query_proposal_artifact": query_proposal_reference,
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
        narrative_timer = TimedTrace()
        narrative = build_risk_narrative(
            risk,
            analysis_mode=settings.analysis_mode,
            model=active_analysis_model,
            model_name=settings.analysis_model or settings.model_name,
            initialization_error=analysis_initialization_error,
        )
        narrative_reference = artifacts.next_version_reference(
            "risk_narrative", state.get("risk_narrative_artifact")
        )
        narrative_reference = artifacts.write_json(narrative_reference, narrative)
        if settings.analysis_mode == "llm":
            narrative_end, narrative_latency = narrative_timer.finish()
            trace.write(
                task_id=state["task_id"],
                node="risk",
                event_type=(
                    "LLM_SKIP"
                    if narrative.execution_status == "NOT_NEEDED"
                    else "LLM_CALL"
                ),
                status=(
                    TraceStatus.FAILED
                    if narrative.execution_status == "DEGRADED"
                    else TraceStatus.SUCCESS
                ),
                start_time=narrative_timer.start_time,
                end_time=narrative_end,
                latency_ms=narrative_latency,
                input_summary="purpose=risk_narrative",
                output_summary=(
                    f"model={narrative.model_name};prompt={narrative.prompt_version};"
                    f"execution={narrative.execution_status};"
                    f"attempts={narrative.attempts};"
                    f"input_tokens={narrative.input_tokens};"
                    f"output_tokens={narrative.output_tokens}"
                ),
                error=narrative.error_code,
            )
        return {
            "status": (
                "WAITING_APPROVAL"
                if risk.risk_level.value in ("MEDIUM", "HIGH")
                else "RUNNING"
            ),
            "current_node": "risk",
            "risk_artifact": reference,
            "risk_narrative_artifact": narrative_reference,
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
        output_path = run_dir / "output" / "credit_report.md"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        return {
            "status": "COMPLETED",
            "current_node": "report",
            "report_artifact": "output/credit_report.md",
        }

    builder = StateGraph(AgentState)
    builder.add_node("document", traced("document", document_node))
    builder.add_node("financial", traced("financial", financial_node))
    builder.add_node("research", traced("research", research_node))
    builder.add_node("risk", traced("risk", risk_node))
    builder.add_node("approval", traced("approval", approval_node))
    builder.add_node("report", traced("report", report_node))
    builder.add_edge(START, "document")
    builder.add_conditional_edges(
        "document",
        lambda state: "end" if state["status"] == "FAILED" else "financial",
        {"end": END, "financial": "financial"},
    )
    builder.add_conditional_edges(
        "financial",
        route_after_financial,
        {"research": "research", "risk": "risk", "end": END},
    )
    builder.add_conditional_edges(
        "research",
        lambda state: "end" if state["status"] == "FAILED" else "risk",
        {"end": END, "risk": "risk"},
    )
    builder.add_conditional_edges(
        "risk",
        route_after_risk,
        {"approval": "approval", "report": "report", "end": END},
    )
    builder.add_conditional_edges(
        "approval",
        route_after_approval,
        {"research": "research", "report": "report", "end": END},
    )
    builder.add_edge("report", END)
    return builder.compile(checkpointer=checkpointer)


def load_risk_artifact(case_dir: Path, reference: str) -> RiskAnalysis:
    return RiskAnalysis.model_validate(ArtifactStore(case_dir).read_json(reference))
