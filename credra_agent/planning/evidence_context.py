"""Bounded, grounded evidence context and semantic question-completion gates."""

from app.tools.artifacts import ArtifactStore
from credra_agent.evidence.artifacts import validate_evidence_artifacts
from credra_agent.evidence.models import Claim, EvidenceBundle
from credra_agent.evidence.service import active_evidence, summarize_bundle
from credra_agent.intent.models import TaskSpec
from credra_agent.planning.models import ClaimProposal, QuestionAssessment


def evidence_context(
    store: ArtifactStore, references: set[str], task: TaskSpec
) -> dict:
    """Only explicit read_document results expose text; indexes expose metadata."""
    bundles = []
    reads = []
    financial_inputs = []
    financial_results = []
    proposals = {}
    # The latest receipt for each claim replaces its earlier pending view;
    # source documents and contradictory independent evidence remain retained.
    for reference in sorted(references, key=_version):
        if reference.startswith("artifacts/agent_claims_v"):
            payload = store.read_json(reference)
            bundle_ref = payload["evidence_bundle_ref"]
            bundle = EvidenceBundle.model_validate(store.read_json(bundle_ref))
            validate_evidence_artifacts(
                store,
                [reference, bundle_ref],
                as_of=bundle.as_of,
                subject_id=task.subject_id,
            )
            bundles.append(
                {
                    "reference": bundle_ref,
                    "claims_reference": reference,
                    "eligible_for_current_task": bundle.as_of == task.as_of,
                    "summary": summarize_bundle(bundle),
                    "claims": [item.model_dump(mode="json") for item in bundle.claims],
                    "documents": [
                        {
                            "document_id": item.document_id,
                            "title": item.title,
                            "url": item.url,
                            "original_source_id": item.original_source_id,
                            "original_publisher": item.original_publisher,
                            "source_tags": item.source_tags,
                            "published_at": item.published_at.isoformat()
                            if item.published_at
                            else None,
                            "source_kind": item.source_kind,
                            "access_scope": item.access_scope,
                            "hash_scope": item.hash_scope,
                            "has_local_text": bool(item.fragments),
                            "limitations": item.limitations[:10],
                        }
                        for item in bundle.documents[:20]
                    ],
                }
            )
        elif reference.startswith("artifacts/agent_financial_input_v"):
            from credra_agent.financial.models import FinancialInput

            data = FinancialInput.model_validate(store.read_json(reference))
            financial_inputs.append(
                {
                    "reference": reference,
                    "subject_id": data.subject_id,
                    "source_kind": data.source_kind,
                    "input_file_ref": data.input_file_ref,
                    "input_file_hash": data.input_file_hash,
                    "fields": [
                        {
                            "metric": item.metric,
                            "period": item.period.model_dump(mode="json"),
                            "measurement": item.measurement,
                            "receivables_basis": item.receivables_basis,
                            "accounting_basis": item.accounting_basis,
                            "profit_attribution": item.profit_attribution,
                            "revision": item.revision,
                            "missing": item.value is None,
                            "missing_reason": item.missing_reason,
                            "normalized_unit": item.normalized_unit,
                            "source_refs": [
                                source.model_dump(mode="json")
                                for source in item.source_refs
                            ],
                        }
                        for item in data.datums
                    ],
                    "limitations": data.limitations,
                }
            )
        elif reference.startswith("artifacts/agent_tool_result_v"):
            payload = store.read_json(reference)
            if payload.get("schema_version") == "document_read_v2_p22":
                reads.append({"reference": reference, **payload})
            elif payload.get("schema_version") == "agent_financial_result_v2":
                financial_results.append(
                    {
                        "reference": reference,
                        **payload,
                        "results": [
                            {"result_index": index, **item}
                            for index, item in enumerate(payload["results"])
                        ],
                    }
                )
        elif reference.startswith("artifacts/agent_claim_proposals_v"):
            payload = store.read_json(reference)
            for item in payload["proposals"]:
                proposal = ClaimProposal.model_validate(item)
                prior = proposals.get(proposal.proposal_id)
                if prior is not None and prior != proposal:
                    raise ValueError("proposal id rebound to another claim")
                proposals[proposal.proposal_id] = proposal
    return {
        "bundles": bundles,
        "document_reads": reads,
        "financial_inputs": financial_inputs,
        "financial_results": financial_results,
        "claim_proposals": [
            item.model_dump(mode="json") for item in proposals.values()
        ],
        "untrusted_content": True,
    }


def coordinator_evidence_context(store, references, task) -> dict:
    """Bound model input separately; completion checks retain the full index."""
    import json
    from urllib.parse import urlparse

    context = evidence_context(store, references, task)
    context["read_document_ids"] = sorted(
        {read["document_id"] for read in context["document_reads"]}
    )
    for read in context["document_reads"]:
        # A read exposes every retained fragment available for that document in
        # the immutable run input. Repeating it cannot page through more text.
        read["repeat_read_adds_content"] = False
    # Shared hashes/units belong in a catalog, not repeated on every amount.
    # The immutable input and full evidence context remain authoritative.
    for financial in context["financial_inputs"]:
        sources = []
        source_indexes = {}
        for field in financial["fields"]:
            projected = []
            for source in field["source_refs"]:
                common = {
                    key: value
                    for key, value in source.items()
                    if key not in {"location", "input_location", "artifact_ref"}
                }
                key = json.dumps(common, sort_keys=True)
                if key not in source_indexes:
                    source_indexes[key] = len(sources)
                    sources.append(common)
                projected.append(
                    {
                        "source_index": source_indexes[key],
                        "source_id": source["source_id"],
                        "location": source["location"],
                        "input_location": source["input_location"],
                        "artifact_ref": source["artifact_ref"],
                    }
                )
            field["source_refs"] = projected
        financial["source_catalog"] = sources
    for bundle in context["bundles"]:
        for document in bundle["documents"]:
            # Fetch/read use the full URL from the selected artifact, never a
            # planner-supplied URL. Preserve origin and scope instead of URL bulk.
            url = document.pop("url")
            document["has_url"] = bool(url)
            try:
                document["url_host"] = urlparse(url).hostname if url else None
            except ValueError:
                document["url_host"] = None
            document["limitations"] = [
                item
                for item in document["limitations"]
                if item
                not in {
                    "ONLY_NECESSARY_EXCERPTS_RETAINED",
                    "EXCERPT_WHITESPACE_NORMALIZED",
                }
            ]
    latest_claims = {}
    for bundle in context["bundles"]:
        if bundle["eligible_for_current_task"]:
            for claim in bundle["claims"]:
                latest_claims[claim["claim_id"]] = claim
    proposal_questions = {
        item["proposal_id"]: item["question_id"] for item in context["claim_proposals"]
    }
    computed_metrics = {
        item["metric_id"]
        for result in context["financial_results"]
        for item in result["results"]
    }
    context["completion_progress"] = [
        {
            "question_id": question.question_id,
            "required_metric_ids": question.required_metric_ids,
            "missing_metric_ids": sorted(
                set(question.required_metric_ids) - computed_metrics
            ),
            "minimum_verified_findings": question.minimum_verified_findings,
            "verified_finding_claim_ids": sorted(
                claim_id
                for claim_id, bound_question in proposal_questions.items()
                if bound_question == question.question_id
                and latest_claims.get(claim_id, {}).get("status")
                in {"SUPPORTED", "REFUTED", "CONFLICTING"}
            ),
        }
        for question in task.questions
    ]
    context["metadata_compacted"] = True
    truncated = len(context["bundles"]) > 10 or len(context["document_reads"]) > 3
    latest = {}
    for bundle in context["bundles"]:
        for claim in bundle["claims"]:
            latest[claim["claim_id"]] = bundle["reference"]
    context["bundles"] = [
        {
            **bundle,
            "claims": [
                claim
                for claim in bundle["claims"]
                if latest[claim["claim_id"]] == bundle["reference"]
            ],
        }
        for bundle in context["bundles"][-10:]
    ]
    context["document_reads"] = context["document_reads"][-3:]
    # Verification versions retain the full source corpus on disk. Do not send
    # that identical catalog once per receipt or discard the only usable catalog.
    seen_documents = set()
    for bundle in reversed(context["bundles"]):
        unique = []
        for document in bundle["documents"]:
            key = (bundle["eligible_for_current_task"], document["document_id"])
            if key not in seen_documents:
                unique.append(document)
                seen_documents.add(key)
        bundle["documents"] = unique
    context["bundles"] = [
        bundle
        for bundle in context["bundles"]
        if bundle["documents"] or bundle["claims"]
    ]
    if len(json.dumps(context, ensure_ascii=False)) > 18000:
        truncated = True
        for bundle in context["bundles"]:
            for document in bundle["documents"]:
                limits = document["limitations"]
                document["limitation_count"] = len(limits)
                document["limitations"] = []
                document["metadata_details_omitted"] = bool(limits)
        if context["financial_results"]:
            for financial in context["financial_inputs"]:
                financial["fields"] = [
                    {
                        key: field[key]
                        for key in (
                            "metric",
                            "period",
                            "profit_attribution",
                            "accounting_basis",
                            "receivables_basis",
                            "missing",
                            "missing_reason",
                        )
                    }
                    for field in financial["fields"]
                ]
                financial["full_lineage_in_input_artifact"] = True
    context["context_truncated"] = truncated
    while len(json.dumps(context, ensure_ascii=False)) > 18000:
        context["context_truncated"] = True
        if context["document_reads"]:
            context["document_reads"].pop(0)
        elif context["bundles"]:
            context["bundles"].pop(0)
        elif context["claim_proposals"]:
            context["claim_proposals"].pop(0)
        elif context["financial_results"]:
            context["financial_results"].pop(0)
        elif context["financial_inputs"]:
            context["financial_inputs"].pop(0)
        else:
            break
    return context


def incomplete_completion_requirements(context: dict) -> list[str]:
    """Return compact, model-visible gaps without turning a limited run unbounded."""
    gaps = []
    for item in context.get("completion_progress", []):
        if item["missing_metric_ids"]:
            gaps.append(
                f"{item['question_id']}:missing_metrics="
                + ",".join(item["missing_metric_ids"])
            )
        actual = len(item["verified_finding_claim_ids"])
        required = item["minimum_verified_findings"]
        if actual < required:
            gaps.append(f"{item['question_id']}:verified_findings={actual}/{required}")
    return gaps


def current_conflicts(store, references, task, historical) -> list[str]:
    """Typed latest receipts supersede typed history, never unknown legacy IDs."""
    latest = {}
    managed = set()
    for bundle in evidence_context(store, references, task)["bundles"]:
        if bundle["eligible_for_current_task"]:
            for claim in bundle["claims"]:
                managed.add(claim["claim_id"])
                latest[claim["claim_id"]] = claim["status"]
    return sorted(
        (set(historical) - managed)
        | {key for key, status in latest.items() if status == "CONFLICTING"}
    )


def _version(reference: str) -> tuple[int, str]:
    import re

    match = re.search(r"_v(\d+)\.json$", reference)
    return (int(match[1]) if match else 0, reference)


def store_proposals(
    store: ArtifactStore,
    proposals: list[ClaimProposal],
    *,
    references: set[str],
    task: TaskSpec,
    version: int,
) -> str | None:
    if not proposals:
        return None
    known = {
        item["proposal_id"]: ClaimProposal.model_validate(item)
        for item in evidence_context(store, references, task)["claim_proposals"]
    }
    question_ids = {item.question_id for item in task.questions}
    for proposal in proposals:
        if proposal.question_id not in question_ids:
            raise ValueError("proposal references unknown question")
        if proposal.proposal_id in known and known[proposal.proposal_id] != proposal:
            raise ValueError("proposal id is immutable")
        known[proposal.proposal_id] = proposal
    return store.write_json(
        f"artifacts/agent_claim_proposals_v{version}.json",
        {
            "schema_version": "claim_proposals_v2_p22",
            "task_spec_version": task.version,
            "proposals": [item.model_dump(mode="json") for item in proposals],
        },
    )


def proposal_claim(proposal: ClaimProposal, *, task: TaskSpec, source_kind) -> Claim:
    return Claim(
        claim_id=proposal.proposal_id,
        subject_id=task.subject_id,
        statement=proposal.statement,
        kind=proposal.kind,
        attributed_to=proposal.attributed_to,
        source_kind=source_kind,
    )


def assess_questions(
    store: ArtifactStore,
    assessments: list[QuestionAssessment],
    *,
    references: set[str],
    task: TaskSpec,
    financial_citations=(),
) -> tuple[list[str], list[str]]:
    """Validate a model's semantic answer; counts alone cannot create an answer."""
    question_ids = {item.question_id for item in task.questions}
    answered, unresolved = [], []
    context = evidence_context(store, references, task)
    from credra_agent.financial.actions import resolve_financial_citations

    financial = resolve_financial_citations(
        store, financial_citations, references=references, task=task
    )
    latest = {}
    for item in context["bundles"]:
        if item["eligible_for_current_task"]:
            for claim in item["claims"]:
                latest[claim["claim_id"]] = (item["reference"], claim)
    bindings = {
        item["proposal_id"]: item["question_id"] for item in context["claim_proposals"]
    }
    if len({item.question_id for item in assessments}) != len(assessments):
        raise ValueError("duplicate question assessment")
    for assessment in assessments:
        if (
            assessment.question_id not in question_ids
            or not set(assessment.evidence_refs) <= references
        ):
            raise ValueError("unknown question or evidence reference")
        if bool(assessment.evidence_refs) != bool(assessment.claim_ids):
            raise ValueError(
                "assessment evidence and claims must be referenced together"
            )
        located = {}
        for reference in assessment.evidence_refs:
            if not reference.startswith("artifacts/agent_evidence_v"):
                raise ValueError("assessment needs typed evidence bundle references")
            bundle = EvidenceBundle.model_validate(store.read_json(reference))
            if bundle.as_of != task.as_of:
                raise ValueError("assessment cutoff mismatch")
            for claim in bundle.claims:
                if claim.claim_id in assessment.claim_ids:
                    if (
                        bindings.get(claim.claim_id, assessment.question_id)
                        != assessment.question_id
                    ):
                        raise ValueError("claim proposal belongs to another question")
                    if latest.get(claim.claim_id, (None,))[0] != reference:
                        raise ValueError("assessment uses obsolete claim evidence")
                    if claim.subject_id != task.subject_id:
                        raise ValueError("assessment subject mismatch")
                    if assessment.status == "ANSWERED" and claim.status not in {
                        "SUPPORTED",
                        "REFUTED",
                    }:
                        raise ValueError(
                            "pending or conflicting claim cannot complete a question"
                        )
                    if claim.status in {"SUPPORTED", "REFUTED", "CONFLICTING"}:
                        documents = {
                            item.document_id: item for item in bundle.documents
                        }
                        evidence = active_evidence(bundle, claim.claim_id)
                        permitted = bool(evidence) and not any(
                            not task.source_policy.permits(
                                documents[item.document_id].source_tags
                            )
                            for item in evidence
                        )
                        if not permitted and assessment.status == "ANSWERED":
                            raise ValueError(
                                "assessment has no permitted active grounded evidence"
                            )
                    located[claim.claim_id] = claim
        if set(located) != set(assessment.claim_ids):
            raise ValueError("assessment claims cannot be resolved")
        question = next(
            q for q in task.questions if q.question_id == assessment.question_id
        )
        eligible_financial = (
            {"cash_profit_ratio"}
            if question.focus == "cash_quality"
            else {"receivables_growth", "growth_gap"}
            if question.focus == "receivables"
            else set()
        )
        financially_supported = any(
            item["question_id"] == question.question_id
            and item["metric"]["status"] == "COMPUTED"
            and item["metric"]["metric_id"] in eligible_financial
            for item in financial
        )
        if (
            assessment.status == "ANSWERED"
            and not assessment.claim_ids
            and not financially_supported
        ):
            raise ValueError(
                "answered assessment needs a verified claim or financial citation"
            )
        if (
            assessment.status == "ANSWERED"
            and context["financial_inputs"]
            and question.focus in {"cash_quality", "receivables"}
            and not financially_supported
        ):
            raise ValueError("financial completion needs a valid computed citation")
        (answered if assessment.status == "ANSWERED" else unresolved).append(
            assessment.question_id
        )
    return answered, unresolved


def report_evidence(store, references, task, financial_citations=()) -> dict:
    """Project latest verifier receipts separately from a planner's inference."""
    context = evidence_context(store, references, task)
    bindings = {
        item["proposal_id"]: item["question_id"] for item in context["claim_proposals"]
    }
    latest = {}
    for item in context["bundles"]:
        if item["eligible_for_current_task"]:
            bundle = EvidenceBundle.model_validate(store.read_json(item["reference"]))
            for claim in bundle.claims:
                latest[claim.claim_id] = (item["reference"], bundle, claim)
    findings = []
    for claim_id, (reference, bundle, claim) in latest.items():
        documents = {item.document_id: item for item in bundle.documents}
        receipts = []
        for evidence in active_evidence(bundle, claim_id):
            document = documents[evidence.document_id]
            if not task.source_policy.permits(document.source_tags):
                continue
            fragment_index = next(
                (
                    i
                    for i, fragment in enumerate(document.fragments)
                    if fragment.location == evidence.location
                ),
                None,
            )
            if fragment_index is None:
                continue
            receipts.append(
                {
                    "evidence_ref": f"{reference}#/evidence/{bundle.evidence.index(evidence)}",
                    "fragment_ref": f"{reference}#/documents/{bundle.documents.index(document)}/fragments/{fragment_index}",
                    "document_id": document.document_id,
                    "original_source_id": document.original_source_id,
                    "original_publisher": document.original_publisher,
                    "url": document.url,
                    "published_at": document.published_at.isoformat()
                    if document.published_at
                    else None,
                    "source_kind": document.source_kind,
                    "document_hash": document.document_hash,
                    "hash_scope": document.hash_scope,
                    "location": document.fragments[fragment_index].model_dump(
                        mode="json", exclude={"text"}
                    ),
                    "relation": evidence.relation,
                    "verifier_version": evidence.verifier_version,
                    "limitations": [*document.limitations, *evidence.limitations],
                }
            )
        findings.append(
            {
                "claim_id": claim_id,
                "question_id": bindings.get(claim_id),
                "claim_ref": f"{reference}#/claims/{bundle.claims.index(claim)}",
                "statement": claim.statement,
                "kind": claim.kind,
                "attributed_to": claim.attributed_to,
                "source_kind": claim.source_kind,
                "status": claim.status if receipts else "UNRESOLVED",
                "receipts": receipts,
                "assertion_scope": "STATEMENT_WAS_MADE"
                if claim.kind in {"PARTY_STATEMENT", "ANALYST_ESTIMATE"}
                else "CLAIM_SPECIFIC_VERIFICATION",
            }
        )
    answers = []
    assessment_refs = sorted(
        (
            ref
            for ref in references
            if ref.startswith("artifacts/agent_question_assessment_v")
        ),
        key=_version,
    )
    if assessment_refs:
        reference = assessment_refs[-1]
        payload = store.read_json(reference)
        assessments = [
            QuestionAssessment.model_validate(item) for item in payload["assessments"]
        ]
        try:
            accepted, unresolved = assess_questions(
                store,
                assessments,
                references=references,
                task=task,
                financial_citations=financial_citations,
            )
        except ValueError:
            accepted, unresolved = [], []
        for index, item in enumerate(assessments):
            answers.append(
                {
                    **item.model_dump(mode="json"),
                    "assessment_ref": f"{reference}#/assessments/{index}",
                    "acceptance": "ACCEPTED"
                    if item.question_id in accepted
                    or item.question_id in unresolved
                    and item.status == "UNRESOLVED"
                    else "REJECTED",
                    "expression_kind": "MODEL_INFERENCE",
                }
            )
    return {"findings": findings, "question_answers": answers}
