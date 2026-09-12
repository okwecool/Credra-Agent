"""P21 artifacts: provenance is supplied by trusted ingestion, never by a planner."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from credra_agent.intent.models import Period

SourceKind = Literal["REAL", "SYNTHETIC", "UNKNOWN"]
Relation = Literal["SUPPORTS", "REFUTES", "IRRELEVANT", "INSUFFICIENT"]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Entity(EvidenceModel):
    entity_id: str = Field(min_length=1)
    legal_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class EntityRelationship(EvidenceModel):
    relationship_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    related_entity_id: str = Field(min_length=1)
    kind: Literal["SUBSIDIARY_OF", "SAME_NAME_AS"]
    evidence_ids: list[str] = Field(min_length=1)
    verifier_version: str = Field(min_length=1)


class Location(EvidenceModel):
    location: str = Field(min_length=1)
    physical_page: int | None = Field(default=None, ge=1)
    printed_page: str | None = None

    @model_validator(mode="after")
    def physical_locator(self) -> "Location":
        if (
            self.location.startswith("page:")
            and self.physical_page is not None
            and self.location != f"page:{self.physical_page}"
        ):
            raise ValueError("physical page does not match page locator")
        return self


class Fragment(Location):
    text: str = Field(min_length=1)


class Document(EvidenceModel):
    document_id: str = Field(min_length=1)
    original_source_id: str | None = Field(default=None, min_length=1)
    original_publisher: str | None = Field(default=None, min_length=1)
    hosting_publisher: str | None = None
    declared_publisher: str | None = None
    title: str = Field(min_length=1)
    url: str | None = None
    published_at: date | None = None
    event_date: date | None = None
    event_period: Period | None = None
    reporting_period: Period | None = None
    reporting_period_label: str | None = None
    retrieved_at: datetime | None = None
    document_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    hash_scope: Literal["RESPONSE_BYTES", "EXTRACTED_TEXT", "UNKNOWN"] = "UNKNOWN"
    source_kind: SourceKind
    reliability: Literal["PRIMARY", "SECONDARY", "UNKNOWN"] = "UNKNOWN"
    access_scope: Literal["FULL", "PARTIAL", "UNKNOWN"] = "UNKNOWN"
    fragments: list[Fragment] = Field(default_factory=list)
    location_hints: list[Location] = Field(default_factory=list)
    revision_of: str | None = None
    limitations: list[str] = Field(default_factory=list)
    untrusted_input: Literal[True] = True

    @model_validator(mode="after")
    def consistent_hash(self) -> "Document":
        if (self.document_hash is None) != (self.hash_scope == "UNKNOWN"):
            raise ValueError("a document hash needs an explicit hash scope")
        locations = [fragment.location for fragment in self.fragments]
        if len(locations) != len(set(locations)):
            raise ValueError("duplicate fragment location")
        if self.retrieved_at and self.retrieved_at.utcoffset() is None:
            raise ValueError("retrieval timestamp needs a timezone")
        if (
            self.event_date
            and self.event_period
            and not self.event_period.start <= self.event_date <= self.event_period.end
        ):
            raise ValueError("event date lies outside event period")
        return self


class EvidenceDraft(EvidenceModel):
    """Untrusted relation proposal; cannot specify acceptance or verifier identity."""

    evidence_id: str = Field(min_length=1)
    document_id: str = Field(min_length=1)
    claim_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    relation: Relation
    excerpt: str | None = Field(default=None, min_length=1)
    location: str | None = Field(default=None, min_length=1)


class Evidence(EvidenceDraft):
    verification_status: Literal["PENDING", "ACCEPTED", "REJECTED"] = "PENDING"
    verifier_version: str | None = Field(default=None, min_length=1)
    entity_scope_verifier_version: str | None = Field(default=None, min_length=1)
    # Claim-specific dependency, e.g. a media story citing a research estimate.
    origin_group_id: str | None = Field(default=None, min_length=1)
    revises_evidence_id: str | None = None
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def acceptance_has_receipt(self) -> "Evidence":
        if self.verification_status == "ACCEPTED" and (
            not self.verifier_version
            or not self.entity_scope_verifier_version
            or self.relation not in {"SUPPORTS", "REFUTES"}
            or not self.excerpt
            or not self.location
        ):
            raise ValueError("accepted evidence needs a verifier and a located excerpt")
        return self


class EntityRelationAssertion(EvidenceModel):
    related_entity_id: str = Field(min_length=1)
    kind: Literal["SUBSIDIARY_OF", "SAME_NAME_AS"]


class Claim(EvidenceModel):
    claim_id: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    kind: Literal[
        "REPORTED_FACT",
        "PARTY_STATEMENT",
        "ANALYST_ESTIMATE",
        "SYSTEM_INFERENCE",
        "HYPOTHESIS",
    ]
    attributed_to: str | None = Field(default=None, min_length=1)
    source_kind: SourceKind
    status: Literal[
        "UNVERIFIED", "SUPPORTED", "REFUTED", "CONFLICTING", "UNRESOLVED"
    ] = "UNVERIFIED"
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    refuting_evidence_ids: list[str] = Field(default_factory=list)
    reasoning: str | None = None
    entity_relation: EntityRelationAssertion | None = None

    @model_validator(mode="after")
    def statement_attribution(self) -> "Claim":
        if (
            self.kind in {"PARTY_STATEMENT", "ANALYST_ESTIMATE"}
            and not self.attributed_to
        ):
            raise ValueError("statements and estimates need attribution")
        if self.kind == "SYSTEM_INFERENCE" and not self.reasoning:
            raise ValueError("system inference needs an explicit reasoning summary")
        if self.entity_relation is not None and self.kind != "REPORTED_FACT":
            raise ValueError(
                "entity relation needs a separately verified factual claim"
            )
        return self


class Corroboration(EvidenceModel):
    """A verifier's semantic independence decision, not a website count."""

    claim_id: str
    evidence_ids: list[str] = Field(min_length=2)
    verifier_version: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class EvidenceBundle(EvidenceModel):
    schema_version: Literal["evidence_bundle_v2_p21"] = "evidence_bundle_v2_p21"
    as_of: date
    entities: list[Entity]
    documents: list[Document]
    evidence: list[Evidence]
    claims: list[Claim]
    relationships: list[EntityRelationship] = Field(default_factory=list)
    corroborations: list[Corroboration] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_graph(self) -> "EvidenceBundle":
        from credra_agent.evidence.service import validate_bundle

        validate_bundle(self)
        return self
