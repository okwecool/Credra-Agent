"""Structured external research contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResearchFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    source_id: str = Field(min_length=1)


class ResearchQueryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query_type: Literal["company", "industry"]
    query: str = Field(min_length=1)
    found: bool
    facts: list[ResearchFact]
    source: str = "mock_dataset"


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str = Field(min_length=1)
    industry: str = Field(min_length=1)
    anomaly_flags: list[str]
    company_result: ResearchQueryResult
    industry_result: ResearchQueryResult
    status: Literal["COMPLETE", "PARTIAL", "EMPTY"]
