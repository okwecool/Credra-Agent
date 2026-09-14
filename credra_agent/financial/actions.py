"""Financial action consumes only immutable, run-visible imported inputs."""

from credra_agent.execution.executor import ExecutionContext, ExecutionOutcome
from credra_agent.execution.models import ComputeMetricsArgs
from credra_agent.financial.calculations import calculate_metrics
from credra_agent.financial.models import FinancialCalculationResult, FinancialInput


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
        comparison_periods=context.task_spec.comparison_periods,
    )
    complete = all(item.status == "COMPUTED" for item in results)
    return ExecutionOutcome(
        status="SUCCESS" if complete else "MISSING_DATA",
        summary="财务计算与字段引用已保存；输入采信及调查结论仍需核验。"
        if complete
        else "财务结果含不可计算指标；缺项与口径限制已保存。",
        payload=FinancialCalculationResult(
            subject_id=context.task_spec.subject_id,
            task_spec_version=context.task_spec.version,
            as_of=context.task_spec.as_of,
            source_kinds=sorted({item.source_kind for item in inputs.values()}),
            results=results,
            limitations=[
                "CALCULATION_IS_NOT_EVIDENCE_VERIFICATION",
                "GROWTH_DOES_NOT_PROVE_OVERDUE_RECEIVABLES",
            ],
            input_refs=list(inputs),
        ).model_dump(mode="json"),
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


def resolve_financial_citations(store, citations, *, references, task):
    """Recompute stored results and locate source fragments without accepting claims."""
    import re

    from credra_agent.evidence.artifacts import validate_evidence_artifacts
    from credra_agent.evidence.models import EvidenceBundle

    seen = set()
    resolved = []
    if not citations:
        return resolved
    bundles = {}
    evidence_refs = {
        ref
        for ref in references
        if ref.startswith(("artifacts/agent_evidence_v", "artifacts/agent_claims_v"))
    }
    if evidence_refs:
        for ref in sorted(evidence_refs):
            if not ref.startswith("artifacts/agent_claims_v"):
                continue
            bundle_ref = store.read_json(ref)["evidence_bundle_ref"]
            if bundle_ref not in evidence_refs:
                raise ValueError("FINANCIAL_CITATION_EVIDENCE_NOT_VISIBLE")
            bundle = EvidenceBundle.model_validate(store.read_json(bundle_ref))
            validate_evidence_artifacts(
                store, [ref, bundle_ref], as_of=bundle.as_of, subject_id=task.subject_id
            )
            if bundle.as_of == task.as_of:
                bundles[bundle_ref] = bundle
    question_ids = {q.question_id for q in task.questions}
    for citation in citations:
        key = (citation.question_id, citation.result_ref, citation.result_index)
        if (
            key in seen
            or citation.question_id not in question_ids
            or citation.result_ref not in references
        ):
            raise ValueError("FINANCIAL_CITATION_NOT_VISIBLE_OR_DUPLICATE")
        seen.add(key)
        payload = FinancialCalculationResult.model_validate(
            store.read_json(citation.result_ref)
        )
        if (
            payload.subject_id != task.subject_id
            or payload.task_spec_version not in (None, task.version)
            or payload.as_of not in (None, task.as_of)
            or citation.result_index >= len(payload.results)
        ):
            raise ValueError("FINANCIAL_CITATION_SCOPE_MISMATCH")
        result = payload.results[citation.result_index]
        if result.period not in task.periods:
            raise ValueError("FINANCIAL_CITATION_PERIOD_MISMATCH")
        inputs = {}
        for reference in payload.input_refs:
            if reference not in references or not re.fullmatch(
                r"artifacts/agent_financial_input_v\d+\.json", reference
            ):
                raise ValueError("FINANCIAL_CITATION_INPUT_NOT_VISIBLE")
            data = FinancialInput.model_validate(store.read_json(reference))
            if data.subject_id != task.subject_id:
                raise ValueError("FINANCIAL_CITATION_INPUT_SUBJECT_MISMATCH")
            inputs[reference] = data
        if sorted(set(payload.source_kinds)) != sorted(
            {data.source_kind for data in inputs.values()}
        ):
            raise ValueError("FINANCIAL_CITATION_SOURCE_KIND_MISMATCH")
        expected = calculate_metrics(
            inputs,
            metric_ids=[result.metric_id],
            periods=[result.period],
            accounting_basis=result.accounting_basis,
            as_of=task.as_of,
            source_policy=task.source_policy,
            comparison_periods=task.comparison_periods,
        )[0]
        if result != expected:
            raise ValueError("FINANCIAL_CITATION_RESULT_MISMATCH")
        fields = []
        for field_ref in dict.fromkeys(result.input_refs):
            reference, pointer = field_ref.split("#", 1)
            match = re.fullmatch(r"/datums/(\d+)", pointer)
            if (
                reference not in inputs
                or not match
                or int(match[1]) >= len(inputs[reference].datums)
            ):
                raise ValueError("FINANCIAL_CITATION_FIELD_NOT_VISIBLE")
            datum = inputs[reference].datums[int(match[1])]
            locations = []
            for source in datum.source_refs:
                binding = {
                    "declared_source": source.model_dump(mode="json"),
                    "matches": [],
                }
                pointer_match = re.search(
                    r"#/documents/(\d+)/fragments/(\d+)$", source.input_location or ""
                )
                for bundle_ref, bundle in sorted(bundles.items()):
                    if not pointer_match:
                        continue
                    doc_index, fragment_index = map(int, pointer_match.groups())
                    if doc_index >= len(bundle.documents):
                        continue
                    doc = bundle.documents[doc_index]
                    if (
                        fragment_index >= len(doc.fragments)
                        or source.source_id
                        not in {doc.document_id, doc.original_source_id}
                        or not source.source_hash
                        or source.source_hash != doc.document_hash
                        or source.published_at != doc.published_at
                    ):
                        continue
                    fragment = doc.fragments[fragment_index]
                    binding["matches"].append(
                        {
                            "evidence_ref": bundle_ref,
                            "fragment_ref": f"{bundle_ref}#/documents/{doc_index}/fragments/{fragment_index}",
                            "document_id": doc.document_id,
                            "source_kind": doc.source_kind,
                            "source_hash": doc.document_hash,
                            "hash_scope": doc.hash_scope,
                            "url": doc.url,
                            "location": fragment.model_dump(
                                mode="json", exclude={"text"}
                            ),
                            "limitations": doc.limitations,
                        }
                    )
                binding["lineage_status"] = (
                    "LOCATED" if binding["matches"] else "UNLOCATED"
                )
                locations.append(binding)
            fields.append(
                {
                    "input_ref": field_ref,
                    "datum": datum.model_dump(mode="json"),
                    "source_bindings": locations,
                }
            )
        resolved.append(
            {
                **citation.model_dump(mode="json"),
                "metric": result.model_dump(mode="json"),
                "source_kinds": sorted({data.source_kind for data in inputs.values()}),
                "fields": fields,
                "verification_status": "NOT_VERIFIED",
                "limitations": payload.limitations,
            }
        )
    return resolved
