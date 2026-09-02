"""Fixed credit due-diligence report template."""

from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis
from app.models.risk import RiskAnalysis


def _format_values(years: list[int], values: list[float]) -> str:
    return "；".join(f"{year}: {value:.4f}" for year, value in zip(years, values))


def render_credit_report(
    company: CompanyProfile,
    financial: FinancialAnalysis,
    risk: RiskAnalysis,
    human_comment: str | None = None,
    external_research_incomplete: bool = False,
) -> str:
    revenue = financial.metrics["revenue_growth"]
    margin = financial.metrics["net_profit_margin"]
    current = financial.metrics["current_ratio"]
    debt = financial.metrics["debt_ratio"]
    cashflow = financial.metrics["operating_cash_flow_trend"]

    if risk.risk_flags:
        risk_items = "\n".join(
            f"- [{flag.severity.value}] {flag.description}" for flag in risk.risk_flags
        )
        evidence = "\n".join(
            f"- {flag.type}: {', '.join(flag.evidence)}" for flag in risk.risk_flags
        )
    else:
        risk_items = "- 未识别出显著财务风险项。"
        evidence = "- 财务指标计算结果。"

    has_research_conflict = any(
        flag.type == "external_research_conflict" for flag in risk.risk_flags
    )
    has_evidence_gap = any(
        flag.type == "external_research_evidence_gap" for flag in risk.risk_flags
    )
    if has_research_conflict:
        research_disclosure = (
            "外部调查存在可靠来源冲突，系统未自动选边；具体来源见风险证据。"
        )
    elif external_research_incomplete:
        research_disclosure = "外部调查未全部完成，相关结论存在证据缺口。"
    elif has_evidence_gap:
        research_disclosure = (
            "外部调查已执行，但未形成可采信的核验事实，当前存在证据缺口。"
        )
    else:
        research_disclosure = "外部调查状态正常；具体事实见风险证据。"

    return f"""# 企业授信尽调分析报告

## 1. 企业概况

- 企业名称：{company.company_name}
- 行业：{company.industry}
- 注册资本：{company.registered_capital}
- 成立日期：{company.established_date}
- 主营范围：{company.business_scope}

## 2. 财务情况

- 净利润率：{_format_values(margin.years, margin.values)}
- 流动比率：{_format_values(current.years, current.values)}
- 资产负债率：{_format_values(debt.years, debt.values)}

## 3. 财务趋势

- 营收增长率：{_format_values(revenue.years, revenue.values)}
- 经营现金流：{_format_values(cashflow.years, cashflow.values)} {cashflow.unit}

## 4. 外部经营调查

{research_disclosure}

## 5. 风险项

{risk_items}

## 6. 风险证据

{evidence}

## 7. 人工审核意见

{human_comment or "无人工审核意见。"}

## 8. 综合分析

风险等级：{risk.risk_level.value}。{risk.summary}

> 本报告仅用于授信尽调辅助分析，不构成最终贷款决策。
"""
