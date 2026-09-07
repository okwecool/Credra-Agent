"""M3-D3 chart projections stay bounded, aggregate-only, and optional."""

import json
import math

from app.chainlit_app import _chart_elements
from app.workbench_charts import (
    build_workbench_figures,
    chart_status_markdown,
    project_workbench_charts,
)


def _details() -> dict:
    return {
        "financial": {
            "currency": "CNY_1000",
            "metrics": {
                "revenue_growth": {
                    "years": [2024, 2025],
                    "values": [-0.1, 0.05],
                    "unit": "ratio",
                },
                "net_profit_margin": {
                    "years": [2023, 2024, 2025],
                    "values": [0.02, 0.01, 0.03],
                    "unit": "ratio",
                },
                "operating_cash_flow_trend": {
                    "years": [2023, 2024, 2025],
                    "values": [1000, 1200, 900],
                    "unit": "CNY_1000",
                },
                "current_ratio": {
                    "years": [2023, 2024, 2025],
                    "values": [1.2, 1.1, 0.9],
                    "unit": "ratio",
                },
                "debt_ratio": {
                    "years": [2023, 2024, 2025],
                    "values": [0.6, 0.62, 0.65],
                    "unit": "ratio",
                },
            },
        },
        "research": {
            "company_result": {
                "evidence": [
                    {
                        "source_id": "source-a",
                        "source_tier": "A",
                        "verification_status": "SUPPORTED",
                        "title": "must-not-reach-chart",
                        "content": "raw-body-must-not-reach-chart",
                    },
                    {
                        "source_id": "source-b",
                        "source_tier": "B",
                        "verification_status": "UNVERIFIED",
                    },
                ],
                "candidate_evidence": [
                    {
                        "source_id": "source-a",
                        "source_tier": "A",
                        "verification_status": "SUPPORTED",
                    }
                ],
            },
            "industry_result": {
                "evidence": [
                    {
                        "source_id": "source-c",
                        "source_tier": "C",
                        "verification_status": "CONFLICTING",
                    }
                ],
                "candidate_evidence": [],
            },
        },
        "risk": {
            "risk_flags": [
                {"type": "cashflow", "description": "not chart data"},
                {"type": "liquidity", "description": "not chart data"},
                {"type": "liquidity", "description": "not chart data"},
            ]
        },
    }


def test_chart_projection_uses_only_supported_metrics_and_aggregates() -> None:
    projection = project_workbench_charts(_details())

    series = {item["key"]: item for item in projection["financial"]["series"]}
    assert set(series) == {
        "revenue_growth",
        "net_profit_margin",
        "operating_cash_flow_trend",
        "current_ratio",
        "debt_ratio",
    }
    assert series["revenue_growth"]["values"] == [-10.0, 5.0]
    assert series["debt_ratio"]["values"] == [60.0, 62.0, 65.0]
    assert projection["evidence"]["sourceTiers"] == {"A": 1, "B": 1, "C": 1}
    assert projection["evidence"]["verificationStatuses"] == {
        "SUPPORTED": 1,
        "CONFLICTING": 1,
        "UNVERIFIED": 1,
    }
    assert projection["risk"]["categories"] == {"流动性": 2, "现金流": 1}

    serialized = json.dumps(projection, ensure_ascii=False)
    assert "must-not-reach-chart" not in serialized
    assert "raw-body-must-not-reach-chart" not in serialized
    assert "not chart data" not in serialized


def test_chart_figures_label_units_and_create_chainlit_elements() -> None:
    projection = project_workbench_charts(_details())
    figures = dict(build_workbench_figures(projection))

    assert set(figures) == {"financial-trends", "evidence-risk-distribution"}
    financial = figures["financial-trends"]
    assert {trace.name for trace in financial.data} == {
        "营收增长率",
        "净利润率",
        "经营现金流",
        "流动比率",
        "资产负债率",
    }
    assert financial.layout.height == 840
    assert "CNY_1000" in financial.layout.yaxis2.title.text
    assert list(financial.layout.xaxis.categoryarray) == ["2023", "2024", "2025"]

    distribution = figures["evidence-risk-distribution"]
    assert len(distribution.data) == 3
    assert sum(distribution.data[0].y) == 3
    assert sum(distribution.data[2].y) == 3

    elements = _chart_elements({"thread_id": "chart-thread"}, projection)
    assert [element.name for element in elements] == [
        "financial-trends",
        "evidence-risk-distribution",
    ]
    assert all(element.thread_id == "chart-thread" for element in elements)
    assert all(element.display == "inline" for element in elements)
    assert all(element.size == "large" for element in elements)


def test_chart_projection_rejects_invalid_points_without_blocking_other_data() -> None:
    details = _details()
    details["financial"]["currency"] = "Authorization: must-not-reach-chart"
    details["financial"]["metrics"] = {
        "revenue_growth": {
            "years": [2024, 2025],
            "values": [math.nan, "not-a-number"],
        },
        "current_ratio": {"years": [2024], "values": [1.1, 1.2]},
    }
    details["research"] = None

    projection = project_workbench_charts(details)
    figures = dict(build_workbench_figures(projection))
    markdown = chart_status_markdown(projection)

    assert projection["financial"]["series"] == []
    assert projection["financial"]["currency"] == "—"
    assert "financial-trends" not in figures
    assert "evidence-risk-distribution" in figures
    assert "没有可用数值" in markdown
    assert "年份与数值不匹配" in markdown
    assert "Research Artifact 尚未生成或不可用" in markdown


def test_chart_projection_returns_explicit_placeholders_when_artifacts_missing() -> (
    None
):
    projection = project_workbench_charts({})

    assert build_workbench_figures(projection) == []
    markdown = chart_status_markdown(projection)
    assert "Financial Artifact 尚未生成或不可用" in markdown
    assert "Research Artifact 尚未生成或不可用" in markdown
    assert "Risk Artifact 尚未生成或不可用" in markdown
