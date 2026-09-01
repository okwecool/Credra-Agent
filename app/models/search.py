"""Provider-neutral search contracts and evidence metadata."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.content import FetchedContentReference

QueryType = Literal["company", "industry"]
SourceTier = Literal["A", "B", "C"]
SubjectMatch = Literal["EXACT", "ALIAS", "NONE"]
EvidenceStage = Literal["REJECTED", "CANDIDATE", "VERIFIED"]
FilterReason = Literal[
    "SUBJECT_MISMATCH",
    "CATEGORY_MISMATCH",
    "LOW_RELEVANCE",
    "DUPLICATE_CONTENT",
]
VerificationStatus = Literal[
    "SUPPORTED",
    "CORROBORATED",
    "CONFLICTING",
    "UNVERIFIED",
    "NOT_FOUND",
]


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_type: QueryType
    subject: str = Field(min_length=1)
    subject_aliases: list[str] = Field(default_factory=list)
    category: str = Field(min_length=1)
    query: str = Field(min_length=1)


class SearchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    url: str | None = None
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    published_at: str | None = None
    source_id: str | None = None
    category: str | None = None


class SearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    request: SearchRequest
    items: list[SearchItem]
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResearchEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str = Field(min_length=1)
    verification_status: VerificationStatus
    title: str = Field(min_length=1)
    source_url: str | None = None
    source_domain: str | None = None
    source_tier: SourceTier
    published_at: str | None = None
    retrieved_at: datetime
    query: str = Field(min_length=1)
    relevance_score: float = Field(ge=0.0, le=1.0)
    content_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    source_id: str = Field(min_length=1)
    category: str = "external_research"
    query_match: bool = False
    subject_match: SubjectMatch = "NONE"
    category_match: bool = False
    filter_reasons: list[FilterReason] = Field(default_factory=list)
    evidence_stage: EvidenceStage = "REJECTED"
    fetched_content: FetchedContentReference | None = None
