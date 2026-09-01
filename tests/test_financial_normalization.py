"""M1 unit normalization tests for structured Case import."""

import json
from pathlib import Path

from app.cases import normalize_financial_statement
from app.models.financial import FinancialStatement

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_legacy_ten_thousand_yuan_input_normalizes_to_thousands() -> None:
    path = PROJECT_ROOT / "data" / "case_normal" / "source" / "financial_statement.json"
    statement = FinancialStatement.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )

    normalized = normalize_financial_statement(statement)

    assert statement.currency == "CNY_10K"
    assert statement.statements[0].revenue == 10000
    assert normalized.currency == "CNY_1000"
    assert normalized.statements[0].revenue == 100000
    assert normalized.statements[0].operating_cash_flow == 10500


def test_canonical_thousand_yuan_input_is_preserved() -> None:
    path = (
        PROJECT_ROOT
        / "data"
        / "case_byd_002594"
        / "source"
        / "financial_statement.json"
    )
    statement = FinancialStatement.model_validate(
        json.loads(path.read_text(encoding="utf-8"))
    )

    normalized = normalize_financial_statement(statement)

    assert normalized.currency == "CNY_1000"
    assert normalized.statements[2].revenue == 803964958
