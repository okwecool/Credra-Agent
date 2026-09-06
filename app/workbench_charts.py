"""Bounded chart projections for the Chainlit workbench."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from plotly import graph_objects as go
from plotly.subplots import make_subplots

_YEAR = re.compile(r"^\d{4}$")
_SAFE_CATEGORY = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_SAFE_UNIT = re.compile(r"^[A-Za-z0-9_%/.-]{1,32}$")

_FINANCIAL_METRICS = {
    "revenue_growth": ("营收增长率", "percent"),
    "net_profit_margin": ("净利润率", "percent"),
    "operating_cash_flow_trend": ("经营现金流", "currency"),
    "current_ratio": ("流动比率", "multiple"),
    "debt_ratio": ("资产负债率", "percent"),
}
_SOURCE_TIERS = ("A", "B", "C", "OTHER")
_VERIFICATION_STATUSES = (
    "SUPPORTED",
    "CORROBORATED",
    "CONFLICTING",
    "UNVERIFIED",
    "NOT_FOUND",
    "OTHER",
)
_RISK_LABELS = {
    "cashflow": "现金流",
    "liquidity": "流动性",
    "leverage": "杠杆",
    "profitability": "盈利能力",
    "regulatory": "监管",
    "legal": "诉讼",
    "debt": "债务",
    "verification": "事实核验",
    "research": "补充调查",
}


def _safe_year(value: Any) -> str | None:
    text = str(value)
    if not _YEAR.fullmatch(text):
        return None
    year = int(text)
    return text if 1900 <= year <= 2100 else None


def _safe_unit(value: Any) -> str:
    text = str(value or "")
    return text if _SAFE_UNIT.fullmatch(text) else "—"


def _financial_series(
    financial: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    metrics = financial.get("metrics")
    if not isinstance(metrics, dict):
        return [], ["Financial Artifact 的 metrics 不可用"]

    series: list[dict[str, Any]] = []
    warnings: list[str] = []
    for key, (label, display_unit) in _FINANCIAL_METRICS.items():
        metric = metrics.get(key)
        if metric is None:
            continue
        if not isinstance(metric, dict):
            warnings.append(f"{label}结构无效")
            continue
        years = metric.get("years")
        values = metric.get("values")
        if (
            not isinstance(years, list)
            or not isinstance(values, list)
            or not years
            or len(years) != len(values)
        ):
            warnings.append(f"{label}年份与数值不匹配")
            continue
        points: list[tuple[str, float]] = []
        for year, raw_value in zip(years, values):
            safe_year = _safe_year(year)
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if safe_year is None or not math.isfinite(value):
                continue
            points.append((safe_year, value))
        if not points:
            warnings.append(f"{label}没有可用数值")
            continue
        points.sort(key=lambda point: int(point[0]))
        chart_values = [
            value * 100 if display_unit == "percent" else value for _, value in points
        ]
        series.append(
            {
                "key": key,
                "label": label,
                "unit": display_unit,
                "years": [year for year, _ in points],
                "values": chart_values,
                "source_unit": _safe_unit(
                    metric.get("unit") or financial.get("currency")
                ),
            }
        )
    return series, warnings


def _evidence_records(research: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result_key in ("company_result", "industry_result"):
        result = research.get(result_key)
        if not isinstance(result, dict):
            continue
        for collection in ("evidence", "candidate_evidence"):
            items = result.get(collection)
            if not isinstance(items, list):
                continue
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                identity = str(
                    item.get("source_id")
                    or item.get("evidence_id")
                    or item.get("source_url")
                    or f"{result_key}:{collection}:{index}"
                )
                if identity in seen:
                    continue
                seen.add(identity)
                records.append(item)
    return records


def _enum_count(values: list[Any], allowed: tuple[str, ...]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for value in values:
        normalized = str(value or "OTHER").upper()
        counter[normalized if normalized in allowed else "OTHER"] += 1
    return {key: counter[key] for key in allowed if counter[key]}


def _risk_categories(risk: dict[str, Any]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    flags = risk.get("risk_flags")
    if not isinstance(flags, list):
        return {}
    for flag in flags:
        if not isinstance(flag, dict):
            continue
        raw = str(flag.get("type") or "OTHER")
        key = raw.lower() if _SAFE_CATEGORY.fullmatch(raw) else "other"
        counter[_RISK_LABELS.get(key, key if key != "other" else "其他")] += 1
    return dict(sorted(counter.items()))


def project_workbench_charts(details: dict[str, Any]) -> dict[str, Any]:
    """Return a safe aggregate projection without raw evidence or narrative text."""

    notices: list[str] = []
    financial = details.get("financial")
    if isinstance(financial, dict):
        financial_series, financial_warnings = _financial_series(financial)
        if financial_series:
            notices.append(f"财务趋势：已投影 {len(financial_series)} 项指标。")
        else:
            notices.append("财务趋势：Artifact 没有可用的受支持数值序列。")
        notices.extend(f"财务趋势：{warning}。" for warning in financial_warnings)
    else:
        financial_series = []
        notices.append("财务趋势：Financial Artifact 尚未生成或不可用。")

    research = details.get("research")
    if isinstance(research, dict):
        evidence = _evidence_records(research)
        source_tiers = _enum_count(
            [item.get("source_tier") for item in evidence], _SOURCE_TIERS
        )
        verification_statuses = _enum_count(
            [item.get("verification_status") for item in evidence],
            _VERIFICATION_STATUSES,
        )
        if evidence:
            notices.append(f"Evidence 统计：已聚合 {len(evidence)} 条去重记录。")
        else:
            notices.append("Evidence 统计：当前 Research Artifact 没有候选或证据记录。")
    else:
        source_tiers = {}
        verification_statuses = {}
        notices.append("Evidence 统计：Research Artifact 尚未生成或不可用。")

    risk = details.get("risk")
    if isinstance(risk, dict):
        risk_categories = _risk_categories(risk)
        if risk_categories:
            notices.append(
                f"风险类别统计：已聚合 {sum(risk_categories.values())} 个风险项。"
            )
        else:
            notices.append("风险类别统计：当前 Risk Artifact 没有风险项。")
    else:
        risk_categories = {}
        notices.append("风险类别统计：Risk Artifact 尚未生成或不可用。")

    return {
        "financial": {
            "currency": _safe_unit(
                financial.get("currency") if isinstance(financial, dict) else None
            ),
            "series": financial_series,
        },
        "evidence": {
            "sourceTiers": source_tiers,
            "verificationStatuses": verification_statuses,
        },
        "risk": {"categories": risk_categories},
        "notices": notices,
    }


def _series_by_key(projection: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["key"]): item
        for item in projection.get("financial", {}).get("series", [])
        if isinstance(item, dict) and item.get("key")
    }


def _add_line(
    figure: go.Figure,
    series: dict[str, Any],
    *,
    row: int,
    secondary_y: bool = False,
) -> None:
    unit = series["unit"]
    suffix = "%" if unit == "percent" else "x" if unit == "multiple" else ""
    figure.add_trace(
        go.Scatter(
            x=series["years"],
            y=series["values"],
            name=series["label"],
            mode="lines+markers",
            line={"width": 3},
            marker={"size": 8},
            hovertemplate=(
                f"{series['label']}<br>%{{x}}：%{{y:,.2f}}{suffix}<extra></extra>"
            ),
        ),
        row=row,
        col=1,
        secondary_y=secondary_y,
    )


def build_financial_figure(projection: dict[str, Any]) -> go.Figure | None:
    series = _series_by_key(projection)
    groups = [
        ("增长与盈利能力", ["revenue_growth", "net_profit_margin"], False),
        ("经营现金流", ["operating_cash_flow_trend"], False),
        ("偿债能力", ["current_ratio", "debt_ratio"], True),
    ]
    groups = [group for group in groups if any(key in series for key in group[1])]
    if not groups:
        return None

    figure = make_subplots(
        rows=len(groups),
        cols=1,
        specs=[[{"secondary_y": secondary_y}] for _, _, secondary_y in groups],
        subplot_titles=[title for title, _, _ in groups],
        vertical_spacing=0.12,
    )
    for row, (title, keys, secondary_y) in enumerate(groups, start=1):
        group_years: set[str] = set()
        for key in keys:
            item = series.get(key)
            if item is None:
                continue
            group_years.update(item["years"])
            _add_line(
                figure,
                item,
                row=row,
                secondary_y=secondary_y and key == "debt_ratio",
            )
        figure.update_xaxes(
            title_text="年度",
            type="category",
            categoryorder="array",
            categoryarray=sorted(group_years, key=int),
            row=row,
            col=1,
        )
        if title == "增长与盈利能力":
            figure.update_yaxes(title_text="百分比（%）", row=row, col=1)
        elif title == "经营现金流":
            currency = projection.get("financial", {}).get("currency") or "—"
            figure.update_yaxes(title_text=f"金额（{currency}）", row=row, col=1)
        else:
            figure.update_yaxes(title_text="流动比率（倍）", row=row, col=1)
            figure.update_yaxes(
                title_text="资产负债率（%）", row=row, col=1, secondary_y=True
            )

    figure.update_layout(
        title={"text": "财务趋势（当前 Financial Artifact）", "x": 0.02},
        autosize=True,
        height=240 * len(groups) + 120,
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08, "x": 0},
        margin={"l": 72, "r": 72, "t": 100, "b": 48},
        template="plotly_dark",
    )
    return figure


def build_distribution_figure(projection: dict[str, Any]) -> go.Figure | None:
    panels = [
        (
            "Evidence 来源等级",
            projection.get("evidence", {}).get("sourceTiers", {}),
        ),
        (
            "Evidence 核验状态",
            projection.get("evidence", {}).get("verificationStatuses", {}),
        ),
        ("风险类别", projection.get("risk", {}).get("categories", {})),
    ]
    panels = [(title, values) for title, values in panels if values]
    if not panels:
        return None

    figure = make_subplots(
        rows=1,
        cols=len(panels),
        subplot_titles=[title for title, _ in panels],
        horizontal_spacing=0.1,
    )
    for column, (_, values) in enumerate(panels, start=1):
        labels = list(values)
        counts = [int(values[label]) for label in labels]
        figure.add_trace(
            go.Bar(
                x=labels,
                y=counts,
                text=counts,
                textposition="outside",
                width=0.45,
                hovertemplate="%{x}<br>数量：%{y}<extra></extra>",
                showlegend=False,
            ),
            row=1,
            col=column,
        )
        figure.update_xaxes(title_text="类别", tickangle=-20, row=1, col=column)
        figure.update_yaxes(
            title_text="数量（条）", rangemode="tozero", row=1, col=column
        )

    figure.update_layout(
        title={"text": "调查证据与风险分布（当前 Run Artifact）", "x": 0.02},
        autosize=True,
        height=430,
        margin={"l": 64, "r": 32, "t": 100, "b": 72},
        template="plotly_dark",
        bargap=0.35,
    )
    return figure


def build_workbench_figures(projection: dict[str, Any]) -> list[tuple[str, go.Figure]]:
    """Build available figures independently so one missing area cannot block another."""

    figures: list[tuple[str, go.Figure]] = []
    financial = build_financial_figure(projection)
    if financial is not None:
        figures.append(("financial-trends", financial))
    distribution = build_distribution_figure(projection)
    if distribution is not None:
        figures.append(("evidence-risk-distribution", distribution))
    return figures


def chart_status_markdown(projection: dict[str, Any]) -> str:
    notices = projection.get("notices") or []
    lines = ["## 业务指标图表"]
    lines.extend(f"- {notice}" for notice in notices)
    return "\n".join(lines)
