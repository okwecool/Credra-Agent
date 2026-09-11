"""Production adapters from registered agent actions to existing services."""

from app.mcp.research_client import ResearchMCPClient, run_async
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.execution.models import SearchEvidenceArgs
from credra_agent.intent.models import TaskSpec


def build_agentic_executor(
    task_spec: TaskSpec, *, research_client: ResearchMCPClient | None = None
) -> ActionExecutor:
    """Expose only handlers that are connected to real application services."""

    client = research_client or ResearchMCPClient()

    def search_evidence(arguments):
        assert isinstance(arguments, SearchEvidenceArgs)
        result = run_async(client.search_evidence(arguments))
        matching_questions = [
            item.question_id
            for item in task_spec.questions
            if item.focus in {"general", arguments.category}
        ]
        verified = result.verification_status in {"SUPPORTED", "CORROBORATED"}
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
                "检索与核验完成，结果已保存为可追溯 Artifact。"
                if result.found
                else "指定范围内未检出可采信结果。"
            ),
            payload=result.model_dump(mode="json"),
            novelty_keys=novelty,
            answered_question_ids=matching_questions if verified else [],
            gap_question_ids=[] if verified else matching_questions,
            error_code=error_code,
            actual_external_requests=1,
        )

    return ActionExecutor(
        {
            "search_evidence": HandlerDefinition(
                search_evidence, external_request_reservation=1
            )
        }
    )
