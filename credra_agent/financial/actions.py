"""Financial action consumes only immutable, run-visible imported inputs."""

from credra_agent.execution.executor import ExecutionContext, ExecutionOutcome
from credra_agent.execution.models import ComputeMetricsArgs
from credra_agent.financial.calculations import calculate_metrics
from credra_agent.financial.models import FinancialInput


def compute_metrics(
    args: ComputeMetricsArgs, context: ExecutionContext
) -> ExecutionOutcome:
    if not context.task_spec.periods:
        return ExecutionOutcome(
            status="MISSING_DATA",
            summary="年度财务计算需要先明确调查期间。",
            error_code="FINANCIAL_PERIOD_REQUIRED",
            actual_external_requests=0,
        )
    inputs = {}
    for reference in dict.fromkeys(args.input_refs):
        if reference not in context.available_refs or not reference.startswith(
            "artifacts/agent_financial_input_v"
        ):
            return ExecutionOutcome(
                status="FAILED",
                summary="财务输入引用不可见或不是版本化财务输入。",
                error_code="FINANCIAL_INPUT_NOT_VISIBLE",
                actual_external_requests=0,
            )
        dataset = FinancialInput.model_validate(context.artifacts.read_json(reference))
        if dataset.subject_id != context.task_spec.subject_id:
            return ExecutionOutcome(
                status="FAILED",
                summary="财务输入不属于当前调查主体。",
                error_code="FINANCIAL_SUBJECT_MISMATCH",
                actual_external_requests=0,
            )
        inputs[reference] = dataset
    results = calculate_metrics(
        inputs,
        metric_ids=args.metric_ids,
        periods=context.task_spec.periods,
        accounting_basis=args.accounting_basis,
        as_of=context.task_spec.as_of,
        source_policy=context.task_spec.source_policy,
    )
    complete = all(item.status == "COMPUTED" for item in results)
    return ExecutionOutcome(
        status="SUCCESS" if complete else "MISSING_DATA",
        summary="财务计算与字段引用已保存；输入采信及调查结论仍需核验。"
        if complete
        else "财务结果含不可计算指标；缺项与口径限制已保存。",
        payload={
            "schema_version": "agent_financial_result_v2",
            "subject_id": context.task_spec.subject_id,
            "source_kinds": sorted({item.source_kind for item in inputs.values()}),
            "results": [item.model_dump(mode="json") for item in results],
            "limitations": [
                "CALCULATION_IS_NOT_EVIDENCE_VERIFICATION",
                "GROWTH_DOES_NOT_PROVE_OVERDUE_RECEIVABLES",
            ],
            "input_refs": list(inputs),
        },
        novelty_keys=[
            f"financial:{ref}:{item.metric_id}:{item.period.end}:{item.status}"
            for ref in inputs
            for item in results
        ],
        gap_question_ids=[
            q.question_id
            for q in context.task_spec.questions
            if q.focus in {"receivables", "cash_quality", "general"}
        ],
        actual_external_requests=0,
    )
