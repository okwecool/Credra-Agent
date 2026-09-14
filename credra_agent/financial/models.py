"""P24 minimal financial input contract; source values retain decimal precision."""

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from credra_agent.evidence.models import SourceKind
from credra_agent.intent.models import Period

FinancialField = Literal[
    "revenue",
    "net_profit",
    "operating_cash_flow",
    "receivables",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "total_liabilities",
]
AmountUnit = Literal["CNY", "CNY_1000", "CNY_10K"]
BALANCE_FIELDS = {
    "receivables",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "total_liabilities",
}


class FinancialModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FinancialSourceRef(FinancialModel):
    """Field location and identity; declared source metadata is not verification."""

    source_id: str = Field(min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=2000)
    published_at: date | None = None
    source_tags: list[str] = Field(default_factory=list, max_length=20)
    artifact_ref: str | None = None
    source_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    input_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    input_location: str | None = Field(default=None, min_length=1)
    reported_unit: AmountUnit | None = None


class FinancialDatum(FinancialModel):
    metric: FinancialField
    period: Period
    measurement: Literal["BALANCE", "FLOW"] = "FLOW"
    receivables_basis: Literal["NET_BOOK_VALUE", "GROSS_BALANCE", "UNKNOWN"] = "UNKNOWN"
    accounting_basis: Literal["CONSOLIDATED", "PARENT", "UNKNOWN"]
    profit_attribution: Literal[
        "GROUP_TOTAL", "OWNERS_OF_PARENT", "NOT_APPLICABLE", "UNKNOWN"
    ] = "NOT_APPLICABLE"
    currency: Literal["CNY"] = "CNY"
    source_unit: AmountUnit
    normalized_unit: Literal["CNY_1000"] = "CNY_1000"
    revision: str = Field(min_length=1, max_length=200)
    value: Decimal | None = Field(default=None, allow_inf_nan=False)
    missing_reason: str | None = Field(default=None, min_length=1, max_length=2000)
    source_refs: list[FinancialSourceRef] = Field(default_factory=list, max_length=50)

    @field_validator("value", mode="before")
    @classmethod
    def decimal_input(cls, value):
        if isinstance(value, (float, bool)):
            raise ValueError(  # noqa: TRY004 - Pydantic validation must produce a field issue
                "financial amounts require Decimal, integer or decimal string"
            )
        return value

    @model_validator(mode="after")
    def coherent(self):
        if (self.metric in BALANCE_FIELDS) != (self.measurement == "BALANCE"):
            raise ValueError("measurement does not match the financial field")
        if self.metric != "receivables" and self.receivables_basis != "UNKNOWN":
            raise ValueError("receivables basis applies only to receivables")
        if self.metric == "net_profit":
            if self.profit_attribution == "NOT_APPLICABLE":
                raise ValueError("net profit requires an explicit attribution")
        elif self.profit_attribution != "NOT_APPLICABLE":
            raise ValueError("profit attribution applies only to net profit")
        if (self.value is None) != (self.missing_reason is not None):
            raise ValueError(
                "a missing amount requires a reason; a present amount cannot have one"
            )
        if self.value is not None and not self.source_refs:
            raise ValueError("a present amount requires field lineage")
        if (
            self.metric not in {"net_profit", "operating_cash_flow"}
            and self.value is not None
            and self.value < 0
        ):
            raise ValueError("a balance or revenue cannot be negative")
        return self


class MetricResult(FinancialModel):
    metric_id: Literal[
        "revenue_growth", "receivables_growth", "growth_gap", "cash_profit_ratio"
    ]
    period: Period
    comparison_period: Period | None = None
    accounting_basis: Literal["CONSOLIDATED", "PARENT"]
    status: Literal["COMPUTED", "NOT_COMPUTABLE"]
    value: Decimal | None = Field(default=None, allow_inf_nan=False)
    reason: str | None = None
    formula_version: str
    formula: str
    input_refs: list[str] = Field(default_factory=list)
    display: str | None = None
    display_unit: Literal["percent", "percentage_points", "multiple"]
    calculation_precision: int = 50
    display_decimal_places: int = 2
    rounding: Literal["ROUND_HALF_UP"] = "ROUND_HALF_UP"

    @model_validator(mode="after")
    def result_coherent(self):
        if self.status == "COMPUTED":
            if (
                self.value is None
                or self.reason is not None
                or not self.input_refs
                or self.display is None
            ):
                raise ValueError(
                    "computed metric needs a value, input references and display"
                )
        elif self.value is not None or self.reason is None or self.display is not None:
            raise ValueError(
                "not computable metric needs a reason and no numeric result"
            )
        return self


class FinancialInput(FinancialModel):
    schema_version: Literal["agent_financial_input_v2"] = "agent_financial_input_v2"
    subject_id: str = Field(min_length=1, max_length=200)
    source_kind: SourceKind = "UNKNOWN"
    input_file_ref: str | None = None
    input_file_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    datums: list[FinancialDatum] = Field(min_length=1, max_length=1000)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_datums(self):
        keys = [
            (
                d.metric,
                d.period.start,
                d.period.end,
                d.accounting_basis,
                d.profit_attribution,
                d.revision,
            )
            for d in self.datums
        ]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "duplicate financial field, period, basis, attribution and revision"
            )
        return self
