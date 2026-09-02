"""Structured external research contracts."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.content import ContentFetchStatus
from app.models.search import ResearchEvidence, VerificationStatus
from app.models.verification import VerificationExecutionStatus, VerificationRelation


class ResearchFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str | None = Field(default=None, pattern=r"^fact:[0-9a-f]{64}$")
    subject: str | None = None
    category: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_url: str | None = None
    verification_status: VerificationStatus = "UNVERIFIED"
    claim_id: str | None = Field(default=None, pattern=r"^claim:[0-9a-f]{64}$")
    relation: VerificationRelation | None = None
    evidence_excerpt: str | None = None
    evidence_location: str | None = None
    verifier_model: str | None = None
    verified_at: datetime | None = None


class ResearchQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_type: Literal["company", "industry"]
    query: str = Field(min_length=1)
    found: bool
    facts: list[ResearchFact]
    evidence: list[ResearchEvidence] = Field(default_factory=list)
    candidate_evidence: list[ResearchEvidence] = Field(default_factory=list)
    raw_result_count: int = Field(default=0, ge=0)
    rejected_result_count: int = Field(default=0, ge=0)
    candidate_found: bool = False
    content_fetch_status: ContentFetchStatus = "NOT_NEEDED"
    fetched_content_count: int = Field(default=0, ge=0)
    failed_content_count: int = Field(default=0, ge=0)
    verification_execution_status: VerificationExecutionStatus = "NOT_NEEDED"
    completed_verification_count: int = Field(default=0, ge=0)
    failed_verification_count: int = Field(default=0, ge=0)
    verification_status: VerificationStatus = "NOT_FOUND"
    source: str = "mock_dataset"
    status: Literal["SUCCESS", "FAILED"] = "SUCCESS"
    error: str | None = None


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    anomaly_flags: list[str]
    intent_id: str | None = Field(default=None, pattern=r"^intent:[0-9a-f]{64}$")
    query_plan_id: str | None = Field(
        default=None, pattern=r"^query-plan:[0-9a-f]{64}$"
    )
    company_result: ResearchQueryResult
    industry_result: ResearchQueryResult
    status: Literal["COMPLETE", "PARTIAL", "EMPTY", "INCOMPLETE"]
    execution_status: Literal["COMPLETE", "INCOMPLETE"] = "COMPLETE"
    verification_status: VerificationStatus = "NOT_FOUND"
    candidate_count: int = Field(default=0, ge=0)
    verified_fact_count: int = Field(default=0, ge=0)
    rejected_result_count: int = Field(default=0, ge=0)
    content_fetch_status: ContentFetchStatus = "NOT_NEEDED"
    fetched_content_count: int = Field(default=0, ge=0)
    failed_content_count: int = Field(default=0, ge=0)
    content_fetch_incomplete: bool = False
    verification_execution_status: VerificationExecutionStatus = "NOT_NEEDED"
    completed_verification_count: int = Field(default=0, ge=0)
    failed_verification_count: int = Field(default=0, ge=0)
    verification_incomplete: bool = False
    external_research_incomplete: bool = False
    failed_tools: list[str] = Field(default_factory=list)
