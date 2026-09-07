"""M0 real-case deterministic risk baseline test."""

import json
from pathlib import Path

from app.agents.financial import analyze_financials
from app.agents.risk import analyze_risk
from app.config import Settings
from app.models.financial import FinancialStatement

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "case_byd_002594"


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def test_byd_risk_baseline_requires_human_review() -> None:
    statement = FinancialStatement.model_validate(
        load_json(
            PROJECT_ROOT / "data" / CASE_ID / "source" / "financial_statement.json"
        )
    )
    expected = load_json(
        PROJECT_ROOT
        / "tests"
        / "fixtures"
        / "expected"
        / CASE_ID
        / "expected_risk.json"
    )
    settings = Settings(_env_file=None)
    risk = analyze_risk(analyze_financials(CASE_ID, statement), settings=settings)

    assert risk.risk_level.value == expected["expected_risk_level"]
    assert risk.requires_human_review is expected["expected_requires_human_review"]
    assert [flag.type for flag in risk.risk_flags] == expected["expected_flag_types"]
