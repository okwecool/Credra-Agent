"""Annual P24 ratios: full Decimal calculation, separate financial presentation."""

from datetime import date
from decimal import ROUND_HALF_UP, Decimal, DecimalException, localcontext

from credra_agent.financial.models import FinancialInput, MetricResult
from credra_agent.intent.models import Period, SourcePolicy

FORMULAS = {
    "revenue_growth": "revenue[current] / revenue[previous] - 1",
    "receivables_growth": "receivables[current] / receivables[previous] - 1",
    "growth_gap": "receivables_growth - revenue_growth",
    "cash_profit_ratio": "operating_cash_flow / net_profit[GROUP_TOTAL]",
}


class NotComputable(ValueError):
    pass


def calculate_metrics(
    inputs: dict[str, FinancialInput],
    *,
    metric_ids: list[str],
    periods: list[Period],
    accounting_basis: str,
    as_of: date,
    source_policy: SourcePolicy,
    comparison_periods: list[Period] | None = None,
) -> list[MetricResult]:
    """Never infer profit attribution, choose a revision, or mix periods/bases."""
    if set(metric_ids) - FORMULAS.keys():
        raise ValueError("FINANCIAL_METRIC_UNSUPPORTED")
    if accounting_basis not in {"CONSOLIDATED", "PARENT"}:
        raise ValueError("FINANCIAL_BASIS_UNSUPPORTED")
    results = []
    for period in periods:
        annual = period.start == date(period.start.year, 1, 1) and period.end == date(
            period.start.year, 12, 31
        )
        previous = (
            Period(
                start=date(period.start.year - 1, 1, 1),
                end=date(period.start.year - 1, 12, 31),
            )
            if annual and period.start.year > 1
            else None
        )
        for metric in dict.fromkeys(metric_ids):
            used = []

            def amount(field, target, attribution="NOT_APPLICABLE", _used=used):
                candidates = [
                    (ref, index, datum)
                    for ref, dataset in inputs.items()
                    for index, datum in enumerate(dataset.datums)
                    if datum.metric == field
                    and datum.period == target
                    and datum.profit_attribution == attribution
                ]
                matches = [
                    item
                    for item in candidates
                    if item[2].accounting_basis == accounting_basis
                ]
                if not matches:
                    if (
                        field == "net_profit"
                        and attribution == "GROUP_TOTAL"
                        and (
                            not candidates
                            or all(item[2].value is None for item in candidates)
                        )
                    ):
                        raise NotComputable("MISSING_CONSOLIDATED_PROFIT")
                    raise NotComputable(
                        "ACCOUNTING_BASIS_MISMATCH"
                        if candidates
                        else "MISSING_" + field.upper()
                    )
                if len(matches) != 1:
                    raise NotComputable("AMBIGUOUS_INPUT_REVISION")
                ref, index, datum = matches[0]
                _used.append(f"{ref}#/datums/{index}")
                if datum.value is None:
                    raise NotComputable(datum.missing_reason)
                if (
                    field == "receivables"
                    and datum.receivables_basis != "NET_BOOK_VALUE"
                ):
                    raise NotComputable("RECEIVABLES_BASIS_UNSUPPORTED")
                for source in datum.source_refs:
                    if source.published_at is None:
                        raise NotComputable("SOURCE_PUBLICATION_DATE_UNKNOWN")
                    if source.published_at > as_of:
                        raise NotComputable("SOURCE_AFTER_AS_OF")
                    if not source_policy.permits(source.source_tags):
                        raise NotComputable("SOURCE_POLICY_MISMATCH")
                return datum.value

            def growth(field, _amount=amount, _period=period, _previous=previous):
                if _previous is None:
                    raise NotComputable("COMPARISON_PERIOD_UNAVAILABLE")
                current, prior = _amount(field, _period), _amount(field, _previous)
                if prior <= 0:
                    raise NotComputable("NONPOSITIVE_BASE_" + field.upper())
                return current / prior - 1

            unit = (
                "multiple"
                if metric == "cash_profit_ratio"
                else "percentage_points"
                if metric == "growth_gap"
                else "percent"
            )
            common = {
                "metric_id": metric,
                "period": period,
                "comparison_period": None
                if metric == "cash_profit_ratio"
                else previous,
                "accounting_basis": accounting_basis,
                "formula_version": metric + "_v2",
                "formula": FORMULAS[metric],
                "display_unit": unit,
            }
            try:
                if not annual:
                    raise NotComputable("ANNUAL_PERIOD_REQUIRED")
                if period.end > as_of:
                    raise NotComputable("PERIOD_AFTER_AS_OF")
                if (
                    metric != "cash_profit_ratio"
                    and comparison_periods
                    and previous not in comparison_periods
                ):
                    raise NotComputable("COMPARISON_PERIOD_UNSUPPORTED")
                with localcontext() as context:
                    context.prec = 50
                    context.rounding = ROUND_HALF_UP
                    if metric == "cash_profit_ratio":
                        if accounting_basis != "CONSOLIDATED":
                            raise NotComputable("CONSOLIDATED_BASIS_REQUIRED")
                        profit = amount("net_profit", period, "GROUP_TOTAL")
                        if profit <= 0:
                            raise NotComputable("NONPOSITIVE_CONSOLIDATED_PROFIT")
                        value = amount("operating_cash_flow", period) / profit
                    elif metric == "growth_gap":
                        value = growth("receivables") - growth("revenue")
                    else:
                        value = growth(
                            "receivables"
                            if metric == "receivables_growth"
                            else "revenue"
                        )
                    presented = (value if unit == "multiple" else value * 100).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                    # Suppress presentation-only negative zero.
                    if presented == 0:
                        presented = abs(presented)
                    display = (
                        f"{presented:.2f}"
                        + {
                            "multiple": " 倍",
                            "percentage_points": " 个百分点",
                            "percent": "%",
                        }[unit]
                    )
                result = MetricResult(
                    **common,
                    status="COMPUTED",
                    value=value,
                    input_refs=used,
                    display=display,
                )
            except (NotComputable, DecimalException) as exc:
                result = MetricResult(
                    **common,
                    status="NOT_COMPUTABLE",
                    reason=str(exc)
                    if isinstance(exc, NotComputable)
                    else "ARITHMETIC_LIMIT",
                    input_refs=used,
                )
            results.append(result)
    return results
