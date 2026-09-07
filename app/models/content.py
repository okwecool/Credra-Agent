"""Contracts for untrusted external document retrieval and extraction."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ContentKind = Literal["HTML", "PDF"]
FetchStatus = Literal["SUCCESS", "FAILED", "SKIPPED"]
FetchErrorCode = Literal[
    "INVALID_URL",
    "PRIVATE_ADDRESS",
    "DNS_ERROR",
    "TIMEOUT",
    "NETWORK_ERROR",
    "HTTP_ERROR",
    "ACCESS_BLOCKED",
    "TOO_MANY_REDIRECTS",
    "RESPONSE_TOO_LARGE",
    "UNSUPPORTED_CONTENT_TYPE",
    "EMPTY_CONTENT",
    "PARSE_ERROR",
    "SNAPSHOT_MISSING",
    "BUDGET_EXCEEDED",
    "INTERNAL_ERROR",
]


class ContentSegment(BaseModel):
    """One addressable paragraph or PDF page extracted from a document."""

    model_config = ConfigDict(extra="forbid")

    location: str = Field(min_length=1)
    text: str = Field(min_length=1)


class FetchedDocument(BaseModel):
    """Full untrusted content kept in the runtime content snapshot store."""

    model_config = ConfigDict(extra="forbid")

    requested_url: str = Field(min_length=1)
    final_url: str | None = None
    status: FetchStatus
    content_kind: ContentKind | None = None
    content_type: str | None = None
    title: str | None = None
    published_at: str | None = None
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    byte_size: int = Field(default=0, ge=0)
    char_count: int = Field(default=0, ge=0)
    document_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    segments: list[ContentSegment] = Field(default_factory=list)
    untrusted_input: bool = True
    prompt_injection_signals: list[str] = Field(default_factory=list)
    error_code: FetchErrorCode | None = None
    error_message: str | None = None

    @model_validator(mode="after")
    def status_fields_are_consistent(self) -> "FetchedDocument":
        if self.status == "SUCCESS":
            if not self.segments or not self.document_hash or not self.content_kind:
                raise ValueError("successful fetch requires extracted content metadata")
            if self.error_code or self.error_message:
                raise ValueError("successful fetch cannot contain an error")
        elif not self.error_code:
            raise ValueError("failed or skipped fetch requires an error code")
        return self


class FetchedContentReference(BaseModel):
    """Small fetch result embedded in Research Evidence artifacts."""

    model_config = ConfigDict(extra="forbid")

    status: FetchStatus
    requested_url: str = Field(min_length=1)
    final_url: str | None = None
    content_kind: ContentKind | None = None
    content_type: str | None = None
    title: str | None = None
    published_at: str | None = None
    fetched_at: datetime
    byte_size: int = Field(default=0, ge=0)
    char_count: int = Field(default=0, ge=0)
    document_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    locations: list[str] = Field(default_factory=list)
    snapshot_id: str | None = Field(default=None, pattern=r"^content:[0-9a-f]{64}$")
    untrusted_input: bool = True
    prompt_injection_signals: list[str] = Field(default_factory=list)
    error_code: FetchErrorCode | None = None
    error_message: str | None = None


ContentFetchStatus = Literal[
    "NOT_NEEDED",
    "DISABLED",
    "COMPLETE",
    "PARTIAL",
    "FAILED",
]
