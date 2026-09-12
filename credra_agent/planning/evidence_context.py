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
                            "has_local_text": bool(item.fragments),
                            "limitations": item.limitations[:10],
                        }
                        for item in bundle.documents[:20]
                    ],
                }
            )
        elif reference.startswith("artifacts/agent_tool_result_v"):
            payload = store.read_json(reference)
            if payload.get("schema_version") == "document_read_v2_p22":
                reads.append({"reference": reference, **payload})
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
        "claim_proposals": [
            item.model_dump(mode="json") for item in proposals.values()
        ],
        "untrusted_content": True,
    }


def coordinator_evidence_context(store, references, task) -> dict:
    """Bound model input separately; completion checks retain the full index."""
    import json

    context = evidence_context(store, references, task)
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
    context["context_truncated"] = truncated
    while len(json.dumps(context, ensure_ascii=False)) > 18000:
        context["context_truncated"] = True
        if context["document_reads"]:
            context["document_reads"].pop(0)
        elif context["bundles"]:
            context["bundles"].pop(0)
        elif context["claim_proposals"]:
            context["claim_proposals"].pop(0)
        else:
            break
    return context


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
) -> tuple[list[str], list[str]]:
    """Validate a model's semantic answer; counts alone cannot create an answer."""
    question_ids = {item.question_id for item in task.questions}
    answered, unresolved = [], []
    context = evidence_context(store, references, task)
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
                    if assessment.status == "ANSWERED" and not active_evidence(
                        bundle, claim.claim_id
                    ):
                        raise ValueError("assessment has no active grounded evidence")
                    located[claim.claim_id] = claim
        if set(located) != set(assessment.claim_ids):
            raise ValueError("assessment claims cannot be resolved")
        (answered if assessment.status == "ANSWERED" else unresolved).append(
            assessment.question_id
        )
    return answered, unresolved
