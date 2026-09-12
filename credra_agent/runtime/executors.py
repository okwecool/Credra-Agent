"""Production adapters from registered agent actions to existing services."""

from collections.abc import Callable

from app.config import Settings
from app.llm.gateway import StructuredModel, build_analysis_model
from app.mcp.research_client import ResearchMCPClient, run_async
from app.models.content import FetchedDocument
from credra_agent.evidence.adapters import from_research_result
from credra_agent.evidence.investigation import InvestigationActions
from credra_agent.evidence.models import Entity, SourceKind
from credra_agent.evidence.service import summarize_bundle
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.execution.models import SearchEvidenceArgs
from credra_agent.intent.models import TaskSpec
from credra_agent.planning.models import CoordinatorLimits


def build_agentic_model(
    settings: Settings, limits: CoordinatorLimits
) -> StructuredModel | None:
    """Preserve thinking, bind each stage to authorized caps and aggregate usage."""
    scoped = settings.model_copy(
        update={
            "analysis_llm_max_retry": min(
                settings.analysis_llm_max_retry, limits.model_attempt_reservation - 1
            ),
            "analysis_llm_thinking_budget_tokens": min(
                settings.analysis_llm_thinking_budget_tokens,
                limits.decision_max_output_tokens,
            ),
        }
    )
    return build_analysis_model(
        scoped,
        process_max_output_tokens=limits.decision_max_output_tokens,
        aggregate_accounting=True,
    )


def build_agentic_executor(
    task_spec: TaskSpec,
    *,
    research_client: ResearchMCPClient | None = None,
    evidence_source_kind: SourceKind = "UNKNOWN",
    document_loader: Callable[[str], FetchedDocument] | None = None,
    verifier_model: StructuredModel | None = None,
    content_fetcher: Callable[[str], FetchedDocument] | None = None,
    fetch_external_requests: int = 0,
    fetch_actual_external_requests: int | None = None,
) -> ActionExecutor:
    """Expose only handlers that are connected to real application services."""

    client = research_client or ResearchMCPClient()

    def search_evidence(arguments):
        assert isinstance(arguments, SearchEvidenceArgs)
        search = (
            client.search_candidates
            if isinstance(client, ResearchMCPClient)
            else client.search_evidence
        )
        result = run_async(search(arguments))
        matching_questions = [
            item.question_id
            for item in task_spec.questions
            if item.focus in {"general", arguments.category}
        ]
        mapping_failed = False
        try:
            bundle = from_research_result(
                result,
                target=Entity(
                    entity_id=task_spec.subject_id, legal_name=task_spec.subject_name
                ),
                as_of=task_spec.as_of,
                source_kind=evidence_source_kind,
                document_loader=document_loader,
            )
            evidence_summary = summarize_bundle(bundle)
        except Exception:  # noqa: BLE001 - local mapping cannot invalidate request delivery
            # Keep the actual returned request result; local mapping failure
            # must not turn it into an uncertain/retryable external request.
            bundle = None
            evidence_summary = {"conflict_ids": []}
            mapping_failed = True
        if result.status == "FAILED":
            status = "FAILED"
            error_code = "RESEARCH_SERVICE_FAILED"
        elif not result.found:
            status = "NO_RESULT"
            error_code = None
        else:
            status = "SUCCESS"
            error_code = None
        novelty = sorted(
            {
                item.source_id
                for item in [*result.evidence, *result.candidate_evidence]
                if item.source_id
            }
            | {item.fact_id for item in result.facts if item.fact_id}
        )
        return ExecutionOutcome(
            status=status,
            summary=(
                "检索结果已保存；V2 证据映射失败，需修复后核验。"
                if mapping_failed
                else "检索结果与 V2 证据已保存；主体、来源和问题完成度仍需核验。"
                if result.found
                else "指定范围内未检出可采信结果。"
            ),
            payload=result.model_dump(mode="json"),
            evidence_bundle=bundle,
            novelty_keys=novelty,
            # Aggregate legacy verification is not semantic completion of a
            # user's question; P22 will consume the richer evidence artifacts.
            gap_question_ids=matching_questions,
            conflict_ids=evidence_summary["conflict_ids"],
            error_code=error_code,
            actual_external_requests=1,
        )

    services = InvestigationActions(
        model=verifier_model,
        fetcher=content_fetcher,
        fetch_external_requests=fetch_external_requests,
        fetch_actual_external_requests=fetch_actual_external_requests,
    )
    handlers = {
        "search_evidence": HandlerDefinition(
            search_evidence, external_request_reservation=1
        ),
        "read_document": HandlerDefinition(services.read, contextual=True),
    }
    if content_fetcher is not None:
        handlers["fetch_content"] = HandlerDefinition(
            services.fetch,
            external_request_reservation=fetch_external_requests,
            contextual=True,
        )
    if verifier_model is not None:
        handlers["verify_claim"] = HandlerDefinition(
            services.verify, contextual=True, uses_model=True, model=verifier_model
        )
    return ActionExecutor(handlers)
