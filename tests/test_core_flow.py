"""Acceptance tests for the Financial -> Risk -> Report slice."""

from pathlib import Path

from app.models.risk import RiskAnalysis, RiskLevel
from app.service import run_case

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SECTIONS = (
    "## 1. 企业概况",
    "## 2. 财务情况",
    "## 3. 财务趋势",
    "## 4. 外部经营调查",
    "## 5. 风险项",
    "## 6. 风险证据",
    "## 7. 人工审核意见",
    "## 8. 综合分析",
)


def test_normal_case_completes_low_risk_vertical_slice() -> None:
    financial, risk, report = run_case(PROJECT_ROOT / "data" / "case_normal")

    assert set(financial.metrics) == {
        "revenue_growth",
        "net_profit_margin",
        "current_ratio",
        "debt_ratio",
        "operating_cash_flow_trend",
    }
    assert isinstance(risk, RiskAnalysis)
    assert risk.risk_level is RiskLevel.LOW
    assert risk.risk_flags == []
    assert risk.requires_human_review is False
    assert "海岳精密制造有限公司" in report


def test_risky_case_completes_high_risk_vertical_slice() -> None:
    _, risk, report = run_case(PROJECT_ROOT / "data" / "case_risky")

    assert risk.risk_level is RiskLevel.HIGH
    assert risk.requires_human_review is True
    assert {flag.type for flag in risk.risk_flags} == {
        "leverage",
        "cashflow",
        "liquidity",
    }
    assert "迅驰供应链科技有限公司" in report


def test_report_has_fixed_sections_and_no_automatic_credit_decision() -> None:
    _, _, report = run_case(PROJECT_ROOT / "data" / "case_risky")

    positions = [report.index(section) for section in REQUIRED_SECTIONS]
    assert positions == sorted(positions)
    assert "批准贷款" not in report
    assert "拒绝贷款" not in report
    assert "授信额度为" not in report
    assert "不构成最终贷款决策" in report


def test_risk_schema_rejects_unknown_fields() -> None:
    payload = {
        "risk_level": "LOW",
        "risk_flags": [],
        "requires_human_review": False,
        "summary": "No material signal.",
        "unexpected": True,
    }

    try:
        RiskAnalysis.model_validate(payload)
    except ValueError as exc:
        assert "unexpected" in str(exc)
    else:
        raise AssertionError("RiskAnalysis accepted an unknown field")
