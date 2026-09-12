"""Cross-object grounding, time filtering and explicit dependency handling."""

import hashlib
import re
from collections.abc import Sequence
from itertools import combinations

from credra_agent.evidence.models import (
    Claim,
    Document,
    Entity,
    EntityRelationship,
    Evidence,
    EvidenceBundle,
    EvidenceDraft,
)


def normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold()


def match_entity(
    name: str,
    target_id: str,
    entities: list[Entity],
    relationships: Sequence[EntityRelationship] = (),
) -> dict:
    """Whole-name matching; collisions and affiliations never become EXACT."""
    key = normalized(name)
    matches = [
        item
        for item in entities
        if key
        in {normalized(item.legal_name), *(normalized(alias) for alias in item.aliases)}
    ]
    if len(matches) > 1:
        return {
            "status": "AMBIGUOUS",
            "entity_ids": sorted(item.entity_id for item in matches),
        }
    if not matches:
        return {"status": "UNMATCHED", "entity_ids": []}
    item = matches[0]
    if item.entity_id == target_id:
        status = "EXACT" if key == normalized(item.legal_name) else "ALIAS"
    else:
        status = (
            "RELATED"
            if any(
                relation.kind == "SUBSIDIARY_OF"
                and {relation.subject_id, relation.related_entity_id}
                == {item.entity_id, target_id}
                for relation in relationships
            )
            else "MISMATCH"
        )
    return {"status": status, "entity_ids": [item.entity_id]}


def _index(items: list, key: str) -> dict:
    result = {getattr(item, key): item for item in items}
    if len(result) != len(items):
        raise ValueError(f"duplicate {key}")
    return result


def _walk(start: str, index: dict, link: str) -> list[str]:
    path = []
    current = start
    while current is not None:
        if current in path:
            raise ValueError(f"cyclic {link}")
        if current not in index:
            raise ValueError(f"unresolved {link}")
        path.append(current)
        current = getattr(index[current], link)
    return path


def in_scope(document: Document, as_of) -> bool:
    # Retrieval may happen later: it is not the event or publication date.
    return bool(
        document.published_at is not None
        and document.published_at <= as_of
        and (document.event_date is None or document.event_date <= as_of)
        and (document.event_period is None or document.event_period.end <= as_of)
        and (
            document.reporting_period is None or document.reporting_period.end <= as_of
        )
    )


def _grounded(item: EvidenceDraft, document: Document) -> bool:
    return bool(
        item.excerpt
        and item.location
        and any(
            fragment.location == item.location
            and normalized(item.excerpt) in normalized(fragment.text)
            for fragment in document.fragments
        )
    )


def accept_evidence(
    draft: EvidenceDraft,
    *,
    document: Document,
    claim: Claim,
    verifier_version: str,
    scope_subject_id: str,
    entity_scope_verifier_version: str,
    origin_group_id: str | None = None,
) -> Evidence:
    """Trusted verifier entry point; planner output alone cannot invoke acceptance.

    The caller must separately establish the assertion's entity scope. A legacy
    alias-match flag and an affiliation are not entity-scope receipts.
    """
    if draft.document_id != document.document_id or draft.claim_id != claim.claim_id:
        raise ValueError("verifier input reference mismatch")
    if draft.subject_id != claim.subject_id or scope_subject_id != claim.subject_id:
        raise ValueError("entity scope does not match claim")
    if not document.document_hash or document.source_kind == "UNKNOWN":
        raise ValueError("accepted evidence requires a hashed, classified document")
    if document.source_kind != claim.source_kind or not _grounded(draft, document):
        raise ValueError("evidence excerpt or source kind does not match")
    if not document.retrieved_at or not document.published_at:
        raise ValueError("accepted evidence needs retrieval and publication dates")
    return Evidence(
        **draft.model_dump(),
        verification_status="ACCEPTED",
        verifier_version=verifier_version,
        entity_scope_verifier_version=entity_scope_verifier_version,
        origin_group_id=origin_group_id,
    )


def active_evidence(bundle: EvidenceBundle, claim_id: str) -> list[Evidence]:
    documents = {item.document_id: item for item in bundle.documents}
    eligible = [
        item
        for item in bundle.evidence
        if (
            item.claim_id == claim_id
            and item.verification_status == "ACCEPTED"
            and in_scope(documents[item.document_id], bundle.as_of)
        )
    ]
    index = {item.evidence_id: item for item in bundle.evidence}
    superseded = {
        ancestor
        for item in eligible
        for ancestor in _walk(item.evidence_id, index, "revises_evidence_id")[1:]
    }
    return [item for item in eligible if item.evidence_id not in superseded]


def _origin(item: Evidence, documents: dict) -> str | None:
    return item.origin_group_id or documents[item.document_id].original_source_id


def validate_bundle(bundle: EvidenceBundle) -> None:
    entities = _index(bundle.entities, "entity_id")
    documents = _index(bundle.documents, "document_id")
    evidence = _index(bundle.evidence, "evidence_id")
    claims = _index(bundle.claims, "claim_id")
    _index(bundle.relationships, "relationship_id")
    for document in bundle.documents:
        path = _walk(document.document_id, documents, "revision_of")
        if document.hash_scope == "EXTRACTED_TEXT" and document.fragments:
            text = "\n".join(fragment.text for fragment in document.fragments)
            digest = f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
            if digest != document.document_hash:
                raise ValueError("extracted text does not match document hash")
        if len(path) > 1:
            parent = documents[path[1]]
            if (
                not document.original_source_id
                or document.original_source_id != parent.original_source_id
                or not document.original_publisher
                or not parent.original_publisher
                or normalized(document.original_publisher)
                != normalized(parent.original_publisher)
                or document.source_kind != parent.source_kind
                or not document.published_at
                or not parent.published_at
                or document.published_at < parent.published_at
            ):
                raise ValueError("invalid document revision provenance or date")
    for item in bundle.evidence:
        if (
            item.document_id not in documents
            or item.claim_id not in claims
            or item.subject_id not in entities
        ):
            raise ValueError("unresolved evidence reference")
        document, claim = documents[item.document_id], claims[item.claim_id]
        if document.source_kind != claim.source_kind:
            raise ValueError(
                "REAL/SYNTHETIC/UNKNOWN evidence cannot mix within a claim"
            )
        if item.verification_status == "ACCEPTED":
            accept_evidence(
                EvidenceDraft.model_validate(
                    item.model_dump(include=set(EvidenceDraft.model_fields))
                ),
                document=document,
                claim=claim,
                verifier_version=item.verifier_version,
                scope_subject_id=item.subject_id,
                entity_scope_verifier_version=item.entity_scope_verifier_version,
            )
        path = _walk(item.evidence_id, evidence, "revises_evidence_id")
        if len(path) > 1:
            previous = evidence[path[1]]
            if (
                item.claim_id != previous.claim_id
                or item.subject_id != previous.subject_id
                or previous.document_id
                not in _walk(item.document_id, documents, "revision_of")[1:]
                or item.verification_status != "ACCEPTED"
            ):
                raise ValueError(
                    "evidence revision must identify a verified same-claim document revision"
                )
    for claim in bundle.claims:
        if claim.subject_id not in entities:
            raise ValueError("unresolved claim entity")
        if (
            claim.entity_relation
            and claim.entity_relation.related_entity_id not in entities
        ):
            raise ValueError("unresolved related claim entity")
        active = active_evidence(bundle, claim.claim_id)
        supporting = sorted(
            item.evidence_id for item in active if item.relation == "SUPPORTS"
        )
        refuting = sorted(
            item.evidence_id for item in active if item.relation == "REFUTES"
        )
        if (
            sorted(claim.supporting_evidence_ids) != supporting
            or sorted(claim.refuting_evidence_ids) != refuting
        ):
            raise ValueError("claim references must match active grounded evidence")
        expected = (
            "CONFLICTING"
            if supporting and refuting
            else "SUPPORTED"
            if supporting
            else "REFUTED"
            if refuting
            else None
        )
        if expected and claim.status != expected:
            raise ValueError("claim status must preserve support and refutation")
        if not expected and claim.status not in {"UNVERIFIED", "UNRESOLVED"}:
            raise ValueError("claim status has no active evidence")
    for relationship in bundle.relationships:
        if (
            relationship.subject_id not in entities
            or relationship.related_entity_id not in entities
            or relationship.subject_id == relationship.related_entity_id
        ):
            raise ValueError("invalid relationship entities")
        for reference in relationship.evidence_ids:
            item = evidence.get(reference)
            assertion = claims[item.claim_id].entity_relation if item else None
            if (
                item is None
                or item.subject_id != relationship.subject_id
                or item.verification_status != "ACCEPTED"
                or item.relation != "SUPPORTS"
                or not in_scope(documents[item.document_id], bundle.as_of)
                or assertion is None
                or assertion.related_entity_id != relationship.related_entity_id
                or assertion.kind != relationship.kind
            ):
                raise ValueError(
                    "relationship requires grounded attributed entity evidence"
                )
    for decision in bundle.corroborations:
        if decision.claim_id not in claims or len(set(decision.evidence_ids)) != len(
            decision.evidence_ids
        ):
            raise ValueError("invalid corroboration claim or duplicate evidence")
        active = {
            item.evidence_id: item
            for item in active_evidence(bundle, decision.claim_id)
        }
        selected = []
        for reference in decision.evidence_ids:
            if reference not in active or active[reference].relation != "SUPPORTS":
                raise ValueError("corroboration requires active supporting evidence")
            selected.append(active[reference])
        for left, right in combinations(selected, 2):
            ld, rd = documents[left.document_id], documents[right.document_id]
            if (
                not _origin(left, documents)
                or not _origin(right, documents)
                or _origin(left, documents) == _origin(right, documents)
                or not ld.original_publisher
                or not rd.original_publisher
                or normalized(ld.original_publisher)
                == normalized(rd.original_publisher)
                or ld.document_hash == rd.document_hash
            ):
                raise ValueError(
                    "same or unknown origin cannot establish independent corroboration"
                )


def summarize_bundle(bundle: EvidenceBundle) -> dict:
    """Structured observation inputs; no automatic question-completion decision."""
    documents = {item.document_id: item for item in bundle.documents}
    groups = []
    unknown = []
    for claim in bundle.claims:
        origins: dict[str, list[str]] = {}
        for item in active_evidence(bundle, claim.claim_id):
            origin = _origin(item, documents)
            if origin is None:
                unknown.append(item.evidence_id)
            else:
                origins.setdefault(origin, []).append(item.evidence_id)
        groups.extend(
            {
                "claim_id": claim.claim_id,
                "origin_group_id": origin,
                "evidence_ids": sorted(ids),
            }
            for origin, ids in sorted(origins.items())
        )
    duplicates: dict[tuple, list[str]] = {}
    for document in bundle.documents:
        if document.document_hash:
            duplicates.setdefault(
                (document.source_kind, document.hash_scope, document.document_hash), []
            ).append(document.document_id)
    return {
        "claim_statuses": {item.claim_id: item.status for item in bundle.claims},
        "conflict_ids": sorted(
            item.claim_id for item in bundle.claims if item.status == "CONFLICTING"
        ),
        "origin_groups": groups,
        "duplicate_document_groups": [
            sorted(ids) for ids in duplicates.values() if len(ids) > 1
        ],
        "unknown_origin_evidence_ids": sorted(unknown),
        "independently_corroborated_claim_ids": sorted(
            {item.claim_id for item in bundle.corroborations}
        ),
        "out_of_scope_evidence_ids": sorted(
            item.evidence_id
            for item in bundle.evidence
            if not in_scope(documents[item.document_id], bundle.as_of)
        ),
        "pending_evidence_ids": sorted(
            item.evidence_id
            for item in bundle.evidence
            if item.verification_status == "PENDING"
        ),
        "limitations": bundle.limitations,
    }


def assemble_bundle(
    *,
    as_of,
    entities: list[Entity],
    documents: list[Document],
    evidence: list[Evidence],
    claims: list[Claim],
    relationships: list[EntityRelationship] | None = None,
    corroborations: list | None = None,
    limitations: list[str] | None = None,
) -> EvidenceBundle:
    """Project statuses from receipts, then validate the entire resulting graph."""
    _index(documents, "document_id")
    _index(evidence, "evidence_id")
    _index(claims, "claim_id")
    document_ids = {item.document_id for item in documents}
    if any(item.document_id not in document_ids for item in evidence):
        raise ValueError("unresolved evidence document")
    # This temporary object is only used to derive indexes; it is never stored
    # or returned. Final model validation includes every cross-object check.
    provisional = EvidenceBundle.model_construct(
        as_of=as_of, documents=documents, evidence=evidence
    )
    projected = []
    for claim in claims:
        active = active_evidence(provisional, claim.claim_id)
        supporting = sorted(
            item.evidence_id for item in active if item.relation == "SUPPORTS"
        )
        refuting = sorted(
            item.evidence_id for item in active if item.relation == "REFUTES"
        )
        status = (
            "CONFLICTING"
            if supporting and refuting
            else "SUPPORTED"
            if supporting
            else "REFUTED"
            if refuting
            else "UNRESOLVED"
            if claim.status == "UNRESOLVED"
            else "UNVERIFIED"
        )
        projected.append(
            Claim.model_validate(
                {
                    **claim.model_dump(),
                    "status": status,
                    "supporting_evidence_ids": supporting,
                    "refuting_evidence_ids": refuting,
                }
            )
        )
    return EvidenceBundle(
        as_of=as_of,
        entities=entities,
        documents=documents,
        evidence=evidence,
        claims=projected,
        relationships=relationships or [],
        corroborations=corroborations or [],
        limitations=limitations or [],
    )
