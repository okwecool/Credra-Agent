"""Unit tests for deterministic financial calculations and input validation."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.financial import FinancialStatement
from app.service import _load_json
from app.tools.financial import calculate_financial_metrics

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_statement(case_id: str) -> FinancialStatement:
    path = PROJECT_ROOT / "data" / case_id / "source" / "financial_statement.json"
    return FinancialStatement.model_validate(_load_json(path))


def test_normal_case_financial_metrics_match_expected_values() -> None:
    metrics = calculate_financial_metrics(load_statement("case_normal"))

    assert metrics["revenue_growth"].years == [2024, 2025]
    assert metrics["revenue_growth"].values == [0.1, 0.1]
    assert metrics["net_profit_margin"].values == [0.09, 0.095, 0.1]
    assert metrics["current_ratio"].values == [1.5, 1.559322, 1.645161]
    assert metrics["debt_ratio"].values == [0.4, 0.4, 0.389535]
    assert metrics["operating_cash_flow_trend"].values == [1050.0, 1180.0, 1320.0]

    for metric in metrics.values():
        assert metric.formula
        assert metric.unit
        assert len(metric.years) == len(metric.values)


def test_risky_case_preserves_growth_leverage_and_cashflow_trends() -> None:
    metrics = calculate_financial_metrics(load_statement("case_risky"))

    assert metrics["revenue_growth"].values == [0.3, 0.3]
    assert metrics["debt_ratio"].values == [0.41, 0.5, 0.65]
    assert metrics["operating_cash_flow_trend"].values == [900.0, 520.0, 180.0]


def test_statement_rejects_missing_required_field() -> None:
    payload = _load_json(
        PROJECT_ROOT / "data" / "case_normal" / "source" / "financial_statement.json"
    )
    del payload["statements"][0]["revenue"]

    with pytest.raises(ValidationError, match="revenue"):
        FinancialStatement.model_validate(payload)


def test_statement_rejects_non_increasing_or_duplicate_years() -> None:
    payload = _load_json(
        PROJECT_ROOT / "data" / "case_normal" / "source" / "financial_statement.json"
    )
    payload["statements"][1]["year"] = 2023

    with pytest.raises(ValidationError, match="strictly increasing"):
        FinancialStatement.model_validate(payload)


def test_statement_rejects_zero_denominators_at_validation_boundary() -> None:
    payload = _load_json(
        PROJECT_ROOT / "data" / "case_normal" / "source" / "financial_statement.json"
    )
    payload["statements"][0]["current_liabilities"] = 0
    payload["statements"][0]["total_assets"] = 0

    with pytest.raises(ValidationError) as exc_info:
        FinancialStatement.model_validate(payload)

    errors = {error["loc"][-1] for error in exc_info.value.errors()}
    assert {"current_liabilities", "total_assets"} <= errors


def test_revenue_dependent_metrics_reject_zero_revenue() -> None:
    payload = _load_json(
        PROJECT_ROOT / "data" / "case_normal" / "source" / "financial_statement.json"
    )
    payload["statements"][0]["revenue"] = 0
    statement = FinancialStatement.model_validate(payload)

    with pytest.raises(ValueError, match="revenue growth is undefined"):
        calculate_financial_metrics(statement)
