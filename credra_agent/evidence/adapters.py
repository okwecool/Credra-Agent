"""Loss-aware ingestion: a link, snippet hash or old verified flag is not proof."""

import hashlib
from collections.abc import Callable
from datetime import date

from app.models.case import SourceManifest
from app.models.content import FetchedDocument
from app.models.research import ResearchQueryResult
from credra_agent.evidence.models import (
    Claim,
    Document,
    Entity,
    Evidence,
    EvidenceBundle,
    Fragment,
    Location,
    SourceKind,
)


def _identifier(prefix: str, text: str) -> str:
    return f"{prefix}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        # Date-time values retain their displayed calendar day, not fetch day.
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None


def from_source_manifest(
    manifest: SourceManifest, *, subject_id: str, as_of: date
) -> EvidenceBundle:
    """Metadata only: accessed_at is not a per-document fetch receipt."""
    return EvidenceBundle(
        as_of=as_of,
        entities=[Entity(entity_id=subject_id, legal_name=manifest.company.legal_name)],
        documents=[
            Document(
                document_id=source.source_id,
                title=source.title,
                url=str(source.url),
                declared_publisher=source.publisher,
                published_at=source.publication_date,
                reporting_period_label=source.reporting_period,
                location_hints=[
                    Location(
                        location=lineage.location,
                        physical_page=lineage.document_page,
                        printed_page=str(lineage.printed_page)
                        if lineage.printed_page
                        else None,
                    )
                    for lineage in manifest.field_lineage
                    if lineage.source_id == source.source_id
                ],
                source_kind="REAL",
                limitations=[
                    "LEGACY_MANIFEST_NO_FETCH_RECEIPT",
                    "ORIGINAL_ORIGIN_UNKNOWN",
                    "LEGACY_LOCATIONS_ARE_METADATA_ONLY",
                ],
            )
            for source in manifest.sources
        ],
        evidence=[],
        claims=[],
        limitations=["LEGACY_MANIFEST_IS_NOT_VERIFIED_EVIDENCE"],
    )


def from_research_result(
    result: ResearchQueryResult,
    *,
    target: Entity,
    as_of: date,
    source_kind: SourceKind = "UNKNOWN",
    document_loader: Callable[[str], FetchedDocument] | None = None,
) -> EvidenceBundle:
    """Retain legacy receipts without inventing original publisher/entity scope.

    The legacy schema has no assertion-entity receipt or original dependency
    identity. All accepted legacy records therefore remain PENDING in V2 until
    the richer verifier reviews them. The caller keeps the untouched legacy
    payload alongside this artifact.
    """
    documents, evidence = [], []
    claims: dict[str, Claim] = {}
    seen = set()
    for item in [*result.evidence, *result.candidate_evidence]:
        record = item.model_dump_json()
        if record in seen:
            continue
        seen.add(record)
        identifier = _identifier("legacy-record", record)
        receipt = item.verification
        proposed_claim = receipt.claim if receipt else item.verification_claim
        claim_id = (
            proposed_claim.claim_id
            if proposed_claim
            else _identifier("legacy-claim", item.fact)
        )
        statement = proposed_claim.statement if proposed_claim else item.fact
        claim = Claim(
            claim_id=claim_id,
            subject_id=target.entity_id,
            statement=statement,
            kind="HYPOTHESIS",
            source_kind=source_kind,
        )
        if claim_id in claims and claims[claim_id].statement != statement:
            raise ValueError("legacy claim id has contradictory statements")
        claims[claim_id] = claim
        limitations = [
            "ORIGINAL_ORIGIN_UNKNOWN",
            "ASSERTION_ENTITY_SCOPE_UNRESOLVED",
            "SEARCH_SNIPPET_HASH_IS_NOT_DOCUMENT_HASH",
        ]
        fetched = item.fetched_content
        document = None
        if document_loader and fetched and fetched.status == "SUCCESS":
            try:
                loaded = document_loader(fetched.requested_url)
                digest = _identifier(
                    "sha256", "\n".join(segment.text for segment in loaded.segments)
                )
                if (
                    loaded.status != "SUCCESS"
                    or loaded.requested_url != fetched.requested_url
                    or loaded.final_url != fetched.final_url
                    or loaded.document_hash != fetched.document_hash
                    or digest != loaded.document_hash
                ):
                    limitations.append("SNAPSHOT_IDENTITY_OR_HASH_MISMATCH")
                else:
                    document = loaded
            except Exception:  # noqa: BLE001 - preserve an already returned service result
                # A local compatibility read must not lose an already returned
                # external result or cause it to be retried.
                limitations.append("LOCAL_SNAPSHOT_UNAVAILABLE")
        if document is None:
            limitations.append("ORIGINAL_TEXT_NOT_LOCALLY_RESOLVED")
        if receipt and receipt.accepted:
            limitations.append("LEGACY_ACCEPTED_NOT_UPGRADED")
        documents.append(
            Document(
                document_id=identifier,
                title=item.title,
                url=fetched.final_url if fetched else item.source_url,
                hosting_publisher=item.source_domain,
                published_at=_date(item.published_at),
                retrieved_at=document.fetched_at
                if document
                else fetched.fetched_at
                if fetched
                else None,
                document_hash=document.document_hash
                if document
                else fetched.document_hash
                if fetched and fetched.status == "SUCCESS"
                else None,
                hash_scope="EXTRACTED_TEXT"
                if (
                    document
                    or fetched
                    and fetched.status == "SUCCESS"
                    and fetched.document_hash
                )
                else "UNKNOWN",
                source_kind=source_kind,
                fragments=[
                    Fragment(location=segment.location, text=segment.text)
                    for segment in document.segments
                ]
                if document
                else [],
                limitations=limitations,
            )
        )
        evidence.append(
            Evidence(
                evidence_id=_identifier("evidence", record),
                document_id=identifier,
                claim_id=claim_id,
                subject_id=target.entity_id,
                relation=receipt.relation if receipt else "INSUFFICIENT",
                excerpt=receipt.evidence_excerpt if receipt else None,
                location=receipt.evidence_location if receipt else None,
                verification_status="REJECTED"
                if item.evidence_stage == "REJECTED"
                else "PENDING",
                verifier_version=f"legacy:{receipt.verifier}:{receipt.verifier_model}:{receipt.prompt_version}"
                if receipt
                else None,
                limitations=limitations,
            )
        )
    for fact in result.facts:
        claim_id = fact.claim_id or _identifier("legacy-fact", fact.model_dump_json())
        if claim_id not in claims:
            claims[claim_id] = Claim(
                claim_id=claim_id,
                subject_id=target.entity_id,
                statement=fact.statement,
                kind="HYPOTHESIS",
                source_kind=source_kind,
            )
    return EvidenceBundle(
        as_of=as_of,
        entities=[target],
        documents=documents,
        evidence=evidence,
        claims=list(claims.values()),
        limitations=[
            "LEGACY_MAPPING_REQUIRES_ENTITY_AND_ORIGIN_REVIEW",
            "CLAIM_STATUS_IS_NOT_QUESTION_COMPLETION",
        ],
    )
