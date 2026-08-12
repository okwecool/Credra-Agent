"""Structured risk analysis schemas."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class RiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class HumanDecision(str, Enum):
    APPROVE = "approve"
    RESEARCH = "research"


class RiskFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = Field(min_length=1)
    severity: RiskLevel
    description: str = Field(min_length=1)
    evidence: list[str] = Field(min_length=1)


class RiskAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_level: RiskLevel
    risk_flags: list[RiskFlag]
    requires_human_review: bool
    summary: str = Field(min_length=1)
