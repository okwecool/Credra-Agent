"""Offline structured risk baseline for the Day 1 vertical slice."""

from app.config import Settings, get_settings
from app.models.financial import FinancialAnalysis
from app.models.research import ResearchResult
from app.models.risk import RiskAnalysis, RiskFlag, RiskLevel


def analyze_risk(
    analysis: FinancialAnalysis,
    settings: Settings | None = None,
    research: ResearchResult | None = None,
) -> RiskAnalysis:
    """Create a deterministic baseline until the model adapter is connected."""

    settings = settings or get_settings()
    flags: list[RiskFlag] = []
    debt = analysis.metrics["debt_ratio"].values
    cashflow = analysis.metrics["operating_cash_flow_trend"].values
    current_ratio = analysis.metrics["current_ratio"].values
    research_evidence = (
        [
            fact.source_id
            for result in (research.company_result, research.industry_result)
            for fact in result.facts
        ]
        if research
        else []
    )

    if debt[-1] - debt[0] >= settings.debt_ratio_threshold:
        flags.append(
            RiskFlag(
                type="leverage",
                severity=RiskLevel.HIGH if debt[-1] >= 0.65 else RiskLevel.MEDIUM,
                description="资产负债率在观察期内显著上升。",
                evidence=["metric:debt_ratio", *research_evidence],
            )
        )
    if cashflow[-1] < cashflow[0]:
        flags.append(
            RiskFlag(
                type="cashflow",
                severity=RiskLevel.MEDIUM,
                description="经营现金流较观察期初下降。",
                evidence=["metric:operating_cash_flow_trend", *research_evidence],
            )
        )
    if current_ratio[-1] < 1:
        flags.append(
            RiskFlag(
                type="liquidity",
                severity=RiskLevel.HIGH,
                description="期末流动比率低于 1，短期偿债能力承压。",
                evidence=["metric:current_ratio"],
            )
        )
    if research and research.external_research_incomplete:
        flags.append(
            RiskFlag(
                type="external_research",
                severity=RiskLevel.MEDIUM,
                description="外部调查工具调用未全部完成，存在证据缺口。",
                evidence=[f"failed_tool:{tool}" for tool in research.failed_tools],
            )
        )

    if any(flag.severity is RiskLevel.HIGH for flag in flags):
        level = RiskLevel.HIGH
    elif flags:
        level = RiskLevel.MEDIUM
    else:
        level = RiskLevel.LOW

    summary = (
        "发现需要进一步审阅的财务风险信号。"
        if flags
        else "当前财务指标未显示显著风险信号。"
    )
    return RiskAnalysis(
        risk_level=level,
        risk_flags=flags,
        requires_human_review=level in (RiskLevel.MEDIUM, RiskLevel.HIGH),
        summary=summary,
    )
