"""Financial agent orchestration around deterministic calculations."""

from app.models.financial import FinancialAnalysis, FinancialStatement
from app.tools.financial import calculate_financial_metrics


def analyze_financials(
    case_id: str, statement: FinancialStatement
) -> FinancialAnalysis:
    return FinancialAnalysis(
        case_id=case_id,
        currency=statement.currency,
        metrics=calculate_financial_metrics(statement),
    )
