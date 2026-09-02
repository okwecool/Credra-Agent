"""Fixed credit due-diligence report template."""

import re

from app.models.analysis import ReportExpressionArtifact
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis
from app.models.risk import RiskAnalysis


def _format_values(years: list[int], values: list[float]) -> str:
    return "；".join(f"{year}: {value:.4f}" for year, value in zip(years, values))


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>#])")


def _safe_inline(value: object) -> str:
    """Keep accepted model text on one escaped Markdown line."""

    text = " ".join(str(value).split())
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _references(values: list[str]) -> str:
    return ", ".join(_safe_inline(value) for value in values) or "无"


def _render_evidence_expression(expression: ReportExpressionArtifact | None) -> str:
    if expression is None:
        return ""
    lines = [
        "### 4.1 受约束 Evidence Summary",
        "",
        f"- 报告投影状态：{expression.execution_status}",
        f"- Evidence Summary 源状态：{expression.evidence_summary_status or '未生成'}",
    ]
    if expression.evidence_overall_summary:
        lines.extend(
            (
                "",
                _safe_inline(expression.evidence_overall_summary),
                "",
                "引用 Evidence：" + _references(expression.evidence_summary_ids),
            )
        )
    for item in expression.evidence_items:
        lines.append(
            f"- [{item.verification_status}] {_safe_inline(item.summary)} "
            f"（Evidence：{_safe_inline(item.evidence_id)}；"
            f"Source：{_safe_inline(item.source_id)}；"
            f"Claim：{_safe_inline(item.claim_id)}；"
            f"Fact：{_safe_inline(item.fact_id)}）"
        )
    if not expression.evidence_overall_summary and not expression.evidence_items:
        lines.append("- 没有通过二次校验的模型 Evidence 表达，保留确定性调查披露。")
    lines.extend(("", "### 4.2 证据缺口", ""))
    if expression.evidence_gaps:
        lines.extend(
            f"- [{gap.query_type}/{gap.category}/{gap.reason}] "
            f"{_safe_inline(gap.summary)}（Gap：{_safe_inline(gap.gap_id)}）"
            for gap in expression.evidence_gaps
        )
    else:
        lines.append("- 没有通过二次校验的模型缺口说明。")
    lines.extend(("", "### 4.3 后续调查建议（未执行）", ""))
    if expression.query_suggestions:
        lines.extend(
            f"- [NOT_EXECUTED] {_safe_inline(item.query)}；"
            f"原因：{_safe_inline(item.rationale)}；"
            f"引用：{_references(item.reference_ids)}"
            for item in expression.query_suggestions
        )
    else:
        lines.append("- 没有通过确定性门禁且待人工审核的新增查询建议。")
    if expression.rejected_query_proposal_count:
        lines.append(
            f"- 已有 {expression.rejected_query_proposal_count} 条模型建议被门禁拒绝，"
            "拒绝内容未写入报告。"
        )
    return "\n".join(lines)


def _render_risk_expression(expression: ReportExpressionArtifact | None) -> str:
    if expression is None:
        return ""
    lines = [
        "### 5.1 受约束 LLM 风险解释",
        "",
        f"- Risk Narrative 源状态：{expression.risk_narrative_status or '未生成'}",
    ]
    if expression.risk_overall_summary:
        lines.extend(
            (
                "",
                _safe_inline(expression.risk_overall_summary),
                "",
                "引用风险证据：" + _references(expression.risk_summary_evidence_ids),
            )
        )
    for item in expression.risk_explanations:
        lines.append(
            f"- [{_safe_inline(item.risk_id)}] {_safe_inline(item.explanation)} "
            f"（证据：{_references(item.evidence_ids)}）"
        )
    if not expression.risk_overall_summary and not expression.risk_explanations:
        lines.append("- 没有通过二次校验的模型风险表达，保留确定性风险描述。")
    return "\n".join(lines)


def _render_expression_audit(expression: ReportExpressionArtifact | None) -> str:
    if expression is None:
        return ""
    models = "、".join(_safe_inline(name) for name in expression.model_names) or "无"
    lines = [
        "### 8.1 模型表达审计",
        "",
        f"- 运行模式：{expression.mode}",
        f"- 报告投影状态：{expression.execution_status}",
        f"- 模型：{models}",
        f"- 被拒绝的不受支持陈述：{len(expression.unsupported_claims)} 条",
        "- 财务数字、风险等级、事实状态和人工决定均来自确定性 Artifact。",
        "- Query Proposal 仅供人工审核，本报告没有自动执行新增调查。",
    ]
    if expression.unsupported_claims:
        reason_counts: dict[str, int] = {}
        for claim in expression.unsupported_claims:
            reason_counts[claim.reason] = reason_counts.get(claim.reason, 0) + 1
        lines.append(
            "- 拒绝原因："
            + "；".join(
                f"{reason}={count}" for reason, count in sorted(reason_counts.items())
            )
        )
    return "\n".join(lines)


def render_credit_report(
    company: CompanyProfile,
    financial: FinancialAnalysis,
    risk: RiskAnalysis,
    human_comment: str | None = None,
    external_research_incomplete: bool = False,
    report_expression: ReportExpressionArtifact | None = None,
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

    evidence_expression = _render_evidence_expression(report_expression)
    risk_expression = _render_risk_expression(report_expression)
    expression_audit = _render_expression_audit(report_expression)

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

{evidence_expression}

## 5. 风险项

{risk_items}

{risk_expression}

## 6. 风险证据

{evidence}

## 7. 人工审核意见

{human_comment or "无人工审核意见。"}

## 8. 综合分析

风险等级：{risk.risk_level.value}。{risk.summary}

{expression_audit}

> 本报告仅用于授信尽调辅助分析，不构成最终贷款决策。
"""
