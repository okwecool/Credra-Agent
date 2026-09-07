"""Versioned contracts for deterministic business evaluation."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NegativeEvidenceExpectation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture: str = Field(min_length=1)
    expected_filter_reason: str = Field(min_length=1)


class BusinessEvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    source_dir: str = Field(min_length=1)
    expected_financial: str = Field(min_length=1)
    expected_anomalies: str = Field(min_length=1)
    expected_risk: str = Field(min_length=1)
    expected_market: str | None = Field(default=None, min_length=1)
    expected_primary_source_host: str | None = Field(default=None, min_length=1)
    approval_comment: str = Field(min_length=1, max_length=500)
    prohibited_report_text: list[str] = Field(default_factory=list, max_length=30)
    negative_evidence: list[NegativeEvidenceExpectation] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def source_expectations_must_be_paired(self) -> "BusinessEvalCase":
        if (self.expected_market is None) != (
            self.expected_primary_source_host is None
        ):
            raise ValueError(
                "expected_market and expected_primary_source_host must be set together"
            )
        return self


class EvalSuiteManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    suite_id: str = Field(pattern=r"^[a-z][a-z0-9_-]+$")
    description: str = Field(min_length=1, max_length=1_000)
    calculation_tolerance: float = Field(default=1e-6, ge=0, le=0.01)
    cases: list[BusinessEvalCase] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def case_ids_are_unique(self) -> "EvalSuiteManifest":
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("eval case ids must be unique")
        return self


class EvalCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_id: str = Field(pattern=r"^[a-z0-9_.-]+$")
    category: Literal[
        "financial",
        "anomaly",
        "risk",
        "hitl",
        "report",
        "evidence",
        "runtime",
        "source",
    ]
    status: Literal["PASS", "FAIL"]
    expected: Any = None
    actual: Any = None
    message: str = Field(min_length=1, max_length=1_000)


class EvalCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    status: Literal["PASS", "FAIL"]
    thread_id: str = Field(min_length=1)
    run_id: str | None = None
    runtime_directory: str = Field(min_length=1)
    checks: list[EvalCheck] = Field(min_length=1)
    metrics: dict[str, float] = Field(default_factory=dict)


class EvalSuiteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    suite_id: str = Field(min_length=1)
    suite_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    status: Literal["PASS", "FAIL"]
    execution_mode: Literal["offline_deterministic"] = "offline_deterministic"
    external_call_count: Literal[0] = 0
    cases: list[EvalCaseResult] = Field(min_length=1)
    metrics: dict[str, float] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def check_count(self) -> int:
        return sum(len(case.checks) for case in self.cases)

    @property
    def passed_check_count(self) -> int:
        return sum(
            check.status == "PASS" for case in self.cases for check in case.checks
        )
