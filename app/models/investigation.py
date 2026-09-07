"""Deterministic investigation intent and query-plan contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.search import SearchRequest


class IntentTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["anomaly", "risk", "human"]
    value: str = Field(min_length=1)
    categories: list[str] = Field(min_length=1)


class InvestigationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent_id: str = Field(pattern=r"^intent:[0-9a-f]{64}$")
    categories: list[str] = Field(min_length=1)
    triggers: list[IntentTrigger] = Field(min_length=1)
    human_decision: str | None = None
    human_comment: str | None = None


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(pattern=r"^query-plan:[0-9a-f]{64}$")
    intent: InvestigationIntent
    queries: list[SearchRequest] = Field(min_length=1)
    previous_plan_id: str | None = None
    added_queries: list[str] = Field(default_factory=list)
    removed_queries: list[str] = Field(default_factory=list)
