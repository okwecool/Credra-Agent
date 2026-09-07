"""Five named deterministic regression cases from the MVP plan."""

import json
from pathlib import Path

import pytest

from app.agents.financial import analyze_financials
from app.config import Settings
from app.graph.routing import route_after_financial
from app.graph.state import initial_state
from app.models.financial import FinancialStatement
from app.tools.anomalies import detect_anomalies

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "regression_cases.json"


def load_cases() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def statement_for(case: dict) -> FinancialStatement:
    rows = []
    for index, year in enumerate((2023, 2024, 2025)):
        total_assets = 10000.0
        rows.append(
            {
                "year": year,
                "revenue": case["revenue"][index],
                "net_profit": case["revenue"][index] * 0.08,
                "current_assets": 3000,
                "current_liabilities": 2000,
                "total_assets": total_assets,
                "total_liabilities": total_assets * case["debt_ratio"][index],
                "operating_cash_flow": case["cashflow"][index],
            }
        )
    return FinancialStatement(currency="CNY_10K", statements=rows)


@pytest.mark.parametrize(
    "case_name",
    ("normal_01", "normal_02", "risk_cashflow", "risk_debt", "tool_failure"),
)
def test_named_regression_case_routing(case_name: str) -> None:
    case = load_cases()[case_name]
    analysis = analyze_financials(case_name, statement_for(case))
    settings = Settings(
        _env_file=None,
        revenue_threshold=0.20,
        cashflow_threshold=0.0,
        debt_ratio_threshold=0.15,
    )
    flags = detect_anomalies(analysis, settings)
    state = initial_state(case_name, case_name)
    state["anomaly_flags"] = flags

    assert flags == case["expected_flags"]
    assert route_after_financial(state) == case["expected_route"]
