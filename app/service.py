"""Application service for the Day 1 business vertical slice."""

import json
from pathlib import Path

from app.agents.financial import analyze_financials
from app.agents.risk import analyze_risk
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis, FinancialStatement
from app.models.risk import RiskAnalysis
from app.report import render_credit_report


def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def run_case(case_dir: Path) -> tuple[FinancialAnalysis, RiskAnalysis, str]:
    source_dir = case_dir / "source"
    company = CompanyProfile.model_validate(
        _load_json(source_dir / "company_profile.json")
    )
    statement = FinancialStatement.model_validate(
        _load_json(source_dir / "financial_statement.json")
    )
    financial = analyze_financials(case_dir.name, statement)
    risk = analyze_risk(financial)
    report = render_credit_report(company, financial, risk)
    return financial, risk, report
