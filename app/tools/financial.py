"""Deterministic, auditable financial calculations."""

from collections.abc import Callable

from app.models.financial import FinancialMetric, FinancialStatement, FinancialYear


def _round(value: float) -> float:
    return round(value, 6)


def _ratio_metric(
    statement: FinancialStatement,
    *,
    name: str,
    formula: str,
    calculation: Callable[[FinancialYear], float],
) -> FinancialMetric:
    return FinancialMetric(
        metric=name,
        years=[row.year for row in statement.statements],
        values=[_round(calculation(row)) for row in statement.statements],
        unit="ratio",
        formula=formula,
    )


def calculate_revenue_growth(statement: FinancialStatement) -> FinancialMetric:
    years: list[int] = []
    values: list[float] = []
    for previous, current in zip(statement.statements, statement.statements[1:]):
        if previous.revenue == 0:
            raise ValueError(
                f"revenue growth is undefined when revenue is zero in {previous.year}"
            )
        years.append(current.year)
        values.append(_round((current.revenue - previous.revenue) / previous.revenue))
    return FinancialMetric(
        metric="revenue_growth",
        years=years,
        values=values,
        unit="ratio",
        formula="(revenue[current] - revenue[previous]) / revenue[previous]",
    )


def calculate_net_profit_margin(statement: FinancialStatement) -> FinancialMetric:
    for row in statement.statements:
        if row.revenue == 0:
            raise ValueError(
                f"net profit margin is undefined when revenue is zero in {row.year}"
            )
    return _ratio_metric(
        statement,
        name="net_profit_margin",
        formula="net_profit / revenue",
        calculation=lambda row: row.net_profit / row.revenue,
    )


def calculate_current_ratio(statement: FinancialStatement) -> FinancialMetric:
    return _ratio_metric(
        statement,
        name="current_ratio",
        formula="current_assets / current_liabilities",
        calculation=lambda row: row.current_assets / row.current_liabilities,
    )


def calculate_debt_ratio(statement: FinancialStatement) -> FinancialMetric:
    return _ratio_metric(
        statement,
        name="debt_ratio",
        formula="total_liabilities / total_assets",
        calculation=lambda row: row.total_liabilities / row.total_assets,
    )


def calculate_operating_cash_flow_trend(
    statement: FinancialStatement,
) -> FinancialMetric:
    return FinancialMetric(
        metric="operating_cash_flow_trend",
        years=[row.year for row in statement.statements],
        values=[_round(row.operating_cash_flow) for row in statement.statements],
        unit=statement.currency,
        formula="operating_cash_flow",
    )


def calculate_financial_metrics(
    statement: FinancialStatement,
) -> dict[str, FinancialMetric]:
    metrics = (
        calculate_revenue_growth(statement),
        calculate_net_profit_margin(statement),
        calculate_current_ratio(statement),
        calculate_debt_ratio(statement),
        calculate_operating_cash_flow_trend(statement),
    )
    return {metric.metric: metric for metric in metrics}
