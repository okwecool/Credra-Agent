"""Financial input and analysis schemas."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FinancialYear(BaseModel):
    """One annual financial statement in base currency units."""

    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=1900, le=2200)
    revenue: float = Field(ge=0)
    net_profit: float
    current_assets: float = Field(ge=0)
    current_liabilities: float = Field(gt=0)
    total_assets: float = Field(gt=0)
    total_liabilities: float = Field(ge=0)
    operating_cash_flow: float

    @model_validator(mode="after")
    def liabilities_cannot_exceed_declared_assets(self) -> FinancialYear:
        if self.total_liabilities > self.total_assets:
            raise ValueError("total_liabilities cannot exceed total_assets")
        return self


class FinancialStatement(BaseModel):
    """Chronologically ordered financial statements for one case."""

    model_config = ConfigDict(extra="forbid")

    currency: str = Field(min_length=1)
    statements: list[FinancialYear] = Field(min_length=2)

    @model_validator(mode="after")
    def years_must_be_strictly_increasing(self) -> FinancialStatement:
        years = [statement.year for statement in self.statements]
        if years != sorted(years) or len(years) != len(set(years)):
            raise ValueError("financial years must be unique and strictly increasing")
        return self


class FinancialMetric(BaseModel):
    """Auditable output from one deterministic financial calculation."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    years: list[int]
    values: list[float]
    unit: str
    formula: str

    @model_validator(mode="after")
    def years_and_values_must_align(self) -> FinancialMetric:
        if len(self.years) != len(self.values):
            raise ValueError("metric years and values must have equal length")
        return self


class FinancialAnalysis(BaseModel):
    """Complete deterministic financial analysis for a case."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    currency: str
    metrics: dict[str, FinancialMetric]
