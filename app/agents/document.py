"""Document normalization for the Day 2 artifact workflow."""

import json
from pathlib import Path

from app.models.company import CompanyProfile
from app.models.financial import FinancialStatement


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def normalize_company_documents(source_dir: Path) -> CompanyProfile:
    """Validate all required source documents and normalize the company profile."""

    company_path = source_dir / "company_profile.json"
    business_path = source_dir / "business_info.md"
    financial_path = source_dir / "financial_statement.json"

    company = CompanyProfile.model_validate(_read_json(company_path))
    business_info = business_path.read_text(encoding="utf-8").strip()
    if not business_info:
        raise ValueError("business_info.md cannot be empty")
    FinancialStatement.model_validate(_read_json(financial_path))
    return company
