"""Structured external research contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.search import ResearchEvidence, VerificationStatus


class ResearchFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_url: str | None = None
    verification_status: VerificationStatus = "UNVERIFIED"


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
    verification_status: VerificationStatus = "NOT_FOUND"
    source: str = "mock_dataset"
    status: Literal["SUCCESS", "FAILED"] = "SUCCESS"
    error: str | None = None


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    anomaly_flags: list[str]
    company_result: ResearchQueryResult
    industry_result: ResearchQueryResult
    status: Literal["COMPLETE", "PARTIAL", "EMPTY", "INCOMPLETE"]
    execution_status: Literal["COMPLETE", "INCOMPLETE"] = "COMPLETE"
    verification_status: VerificationStatus = "NOT_FOUND"
    candidate_count: int = Field(default=0, ge=0)
    verified_fact_count: int = Field(default=0, ge=0)
    rejected_result_count: int = Field(default=0, ge=0)
    external_research_incomplete: bool = False
    failed_tools: list[str] = Field(default_factory=list)
