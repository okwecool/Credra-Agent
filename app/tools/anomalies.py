"""Configuration-driven deterministic anomaly detection."""

from app.config import Settings
from app.models.financial import FinancialAnalysis

REVENUE_CASHFLOW_DIVERGENCE = "REVENUE_CASHFLOW_DIVERGENCE"
DEBT_RATIO_RISING = "DEBT_RATIO_RISING"


def detect_anomalies(
    analysis: FinancialAnalysis,
    settings: Settings,
) -> list[str]:
    revenue_growth = analysis.metrics["revenue_growth"].values[-1]
    cashflows = analysis.metrics["operating_cash_flow_trend"].values
    debt_ratios = analysis.metrics["debt_ratio"].values

    first_cashflow = cashflows[0]
    if first_cashflow == 0:
        cashflow_growth = 0.0 if cashflows[-1] == 0 else float("inf")
    else:
        cashflow_growth = (cashflows[-1] - first_cashflow) / abs(first_cashflow)

    flags: list[str] = []
    if (
        revenue_growth > settings.revenue_threshold
        and cashflow_growth < settings.cashflow_threshold
    ):
        flags.append(REVENUE_CASHFLOW_DIVERGENCE)
    if debt_ratios[-1] - debt_ratios[0] > settings.debt_ratio_threshold:
        flags.append(DEBT_RATIO_RISING)
    return flags
