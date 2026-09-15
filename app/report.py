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


def write_agent_report(
    store,
    *,
    task,
    references,
    citations,
    coverage,
    status,
    stop_reason,
    limitations,
    version,
):
    """Financial report projection; prose never supplies calculated numbers."""
    from credra_agent.financial.actions import resolve_financial_citations

    resolved = resolve_financial_citations(
        store, citations, references=references, task=task
    )
    from credra_agent.planning.evidence_context import report_evidence

    investigation = report_evidence(store, references, task, citations)
    has_investigation = bool(
        investigation["findings"] or investigation["question_answers"]
    )
    questions = {item.question_id: item for item in task.questions}
    selected = {(item.result_ref, item.result_index) for item in citations}
    uncited = []
    for reference in sorted(references):
        if reference.startswith("artifacts/agent_tool_result_v"):
            value = store.read_json(reference)
            if value.get("schema_version") == "agent_financial_result_v2":
                uncited.extend(
                    f"{reference}#/results/{index}"
                    for index in range(len(value["results"]))
                    if (reference, index) not in selected
                )
    payload = {
        "schema_version": "agent_investigation_report_v1"
        if has_investigation
        else "agent_report_v2_p24",
        "subject_id": task.subject_id,
        "subject_name": task.subject_name,
        "task_spec_version": task.version,
        "as_of": task.as_of.isoformat(),
        "periods": [item.model_dump(mode="json") for item in task.periods],
        "comparison_periods": [
            item.model_dump(mode="json") for item in task.comparison_periods
        ],
        "status": status,
        "stop_reason": stop_reason,
        "approval_status": "NOT_REVIEWED",
        "scope": "EVIDENCE_FINANCIAL_AND_MODEL_ASSESSMENTS"
        if has_investigation
        else "FINANCIAL_RESULTS_AND_INVESTIGATION_GAPS",
        "coverage": coverage.model_dump(mode="json"),
        "financial_citations": resolved,
        **investigation,
        "uncited_result_refs": uncited,
        "limitations": [
            *limitations,
            "CALCULATION_IS_NOT_EVIDENCE_VERIFICATION",
            *(
                ["MODEL_ASSESSMENT_IS_NOT_A_VERIFICATION_RECEIPT"]
                if has_investigation
                else ["FULL_INVESTIGATION_REPORT_PENDING"]
            ),
        ],
    }
    lines = [
        "# Credra Agent 调查报告 · 证据、财务计算与缺口",
        "",
        f"- 主体：{_safe_inline(task.subject_name)}（{_safe_inline(task.subject_id)}）",
        f"- 资料截止：{task.as_of.isoformat()}",
        f"- TaskSpec：v{task.version}；调查状态：{_safe_inline(status)}；停止原因：{_safe_inline(stop_reason)}",
        "- 报告审核：未审核",
        "",
        "> 以下数字来自已保存的 Python 计算结果。原文定位不代表输入已采信，计算不证明逾期、违法或现金危机。",
        "",
        "## 财务计算引用",
        "",
    ]
    labels = {
        "revenue_growth": "营收增长率",
        "receivables_growth": "应收账款净额增长率",
        "growth_gap": "应收与营收增速差",
        "cash_profit_ratio": "经营现金流/合并净利润",
    }
    if not resolved:
        lines.append("本次结束决策未选择计算引用。")
    for citation in resolved:
        metric = citation["metric"]
        lines.extend(
            [
                f"### {_safe_inline(labels[metric['metric_id']])}",
                "",
                f"- 对应问题：{_safe_inline(questions[citation['question_id']].text)}",
                f"- 期间：{metric['period']['start']} 至 {metric['period']['end']}；口径：{metric['accounting_basis']}",
                f"- 结果：{_safe_inline(metric['display'])}"
                if metric["status"] == "COMPUTED"
                else f"- 不可计算：{_safe_inline(metric['reason'])}",
                f"- 计算引用：`{citation['result_ref']}#/results/{citation['result_index']}`",
                f"- 公式：`{_safe_inline(metric['formula'])}`；版本：`{_safe_inline(metric['formula_version'])}`",
                f"- 材料性质：{_references(citation['source_kinds'])}；输入采信：未核验",
                "",
            ]
        )
        if metric["comparison_period"]:
            comparison = metric["comparison_period"]
            lines.append(f"- 比较基期：{comparison['start']} 至 {comparison['end']}")
        for field in citation["fields"]:
            datum = field["datum"]
            lines.append(
                f"- 字段引用：`{field['input_ref']}`；{datum['metric']} / {datum['profit_attribution']} / {datum['period']['end']}；金额：{_safe_inline(datum['value'])} 千元；修订：{_safe_inline(datum['revision'])}"
            )
            for binding in field["source_bindings"]:
                source = binding["declared_source"]
                lines.append(
                    f"  - 来源：{_safe_inline(source['source_id'])}；声明位置：{_safe_inline(source['location'])}"
                )
                if not binding["matches"]:
                    lines.append("  - 原文片段：未在当前运行材料中定位；保留声明血缘。")
                for match in binding["matches"]:
                    location = match["location"]
                    lines.append(
                        f"  - 原文引用：`{match['fragment_ref']}`；物理页：{_safe_inline(location['physical_page'])}；印刷页：{_safe_inline(location['printed_page'])}；哈希范围：{match['hash_scope']}"
                    )
        lines.append("")
    if has_investigation:
        lines.extend(
            [
                "## 主张核验与来源",
                "",
                "> 声明核验仅证明声明曾作出；模型假设与推断保留其性质。来源网址数量不等于独立印证。",
                "",
            ]
        )
        for finding in investigation["findings"]:
            lines.extend(
                [
                    f"### {_safe_inline(finding['claim_id'])}",
                    "",
                    f"- 主张：{_safe_inline(finding['statement'])}",
                    f"- 性质：{finding['kind']}；归属：{_safe_inline(finding['attributed_to'] or '无')}；状态：{finding['status']}；材料：{finding['source_kind']}",
                    f"- 核验范围：{finding['assertion_scope']}；主张引用：`{finding['claim_ref']}`",
                ]
            )
            for receipt in finding["receipts"]:
                lines.extend(
                    [
                        f"- [{receipt['relation']}] 原始来源：{_safe_inline(receipt['original_source_id'])}；原发布者：{_safe_inline(receipt['original_publisher'])}；发布日：{_safe_inline(receipt['published_at'])}",
                        f"- 原文引用：`{receipt['fragment_ref']}`；位置：{_safe_inline(receipt['location']['location'])}；核验版本：{_safe_inline(receipt['verifier_version'])}；哈希范围：{receipt['hash_scope']}",
                        *[
                            f"- 来源限制：{_safe_inline(value)}"
                            for value in receipt["limitations"]
                        ],
                    ]
                )
            if not finding["receipts"]:
                lines.append(
                    "- 当前任务没有可定位且符合来源权限的有效核验回执，不能作为已确认事实。"
                )
            lines.append("")
        lines.extend(
            [
                "## 逐问题模型研判",
                "",
                "> 以下是模型研判，门禁接受不代表每句话经过独立语义核验，也不代表人工审核或授信批准。",
                "",
            ]
        )
        for answer in investigation["question_answers"]:
            question = questions.get(answer["question_id"])
            lines.extend(
                [
                    f"### {_safe_inline(question.text if question else answer['question_id'])}",
                    "",
                    f"- 模型研判 [{answer['acceptance']}/{answer['status']}]：{_safe_inline(answer['conclusion'])}",
                    f"- 研判引用：`{answer['assessment_ref']}`；主张：{_references(answer['claim_ids'])}",
                    *[
                        f"- 研判限制：{_safe_inline(value)}"
                        for value in answer["limitations"]
                    ],
                    "",
                ]
            )
    lines.extend(["## 调查缺口与限制", ""])
    for question_id in coverage.gap_question_ids:
        question = questions.get(question_id)
        lines.append(
            f"- 未完成问题：{_safe_inline(question.text if question else question_id)}"
        )
    lines.extend(
        f"- 未决冲突：{_safe_inline(item)}" for item in coverage.unresolved_conflict_ids
    )
    if uncited:
        lines.append(f"- 已保存但未选择引用的计算项：{len(uncited)} 项。")
    lines.extend(f"- {_safe_inline(item)}" for item in payload["limitations"])
    json_ref = store.write_json(f"artifacts/agent_report_v{version}.json", payload)
    markdown_ref = store.write_markdown(
        f"artifacts/agent_report_v{version}.md", "\n".join(lines) + "\n"
    )
    return [json_ref, markdown_ref]


def _render_report_draft_expression(
    expression: ReportExpressionArtifact | None,
) -> str:
    if expression is None:
        return ""
    lines = [
        "## 执行摘要（受约束模型表达）",
        "",
        f"- Report Draft 源状态：{expression.report_draft_status or '未生成'}",
    ]
    if expression.executive_summary:
        lines.extend(
            (
                "",
                _safe_inline(expression.executive_summary),
                "",
                "引用：" + _references(expression.executive_summary_reference_ids),
            )
        )
    else:
        lines.append("- 没有通过二次校验的模型执行摘要，保留确定性报告内容。")
    section_labels = {
        "financial_analysis": "财务分析表达",
        "risk_analysis": "风险分析表达",
        "evidence_assessment": "证据评估表达",
        "limitations": "局限性表达",
    }
    for item in expression.report_sections:
        lines.extend(
            (
                "",
                f"### {section_labels[item.section]}",
                "",
                _safe_inline(item.text),
                "",
                "引用：" + _references(item.reference_ids),
            )
        )
    return "\n".join(lines)


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
        f"- Report Draft 源状态：{expression.report_draft_status or '未生成'}",
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
    report_draft_expression = _render_report_draft_expression(report_expression)

    return f"""# 企业授信尽调分析报告

{report_draft_expression}

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
