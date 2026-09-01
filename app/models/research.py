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
    external_research_incomplete: bool = False
    failed_tools: list[str] = Field(default_factory=list)
