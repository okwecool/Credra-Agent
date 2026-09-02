"""Deterministic structured risk baseline."""

from app.config import Settings, get_settings
from app.models.financial import FinancialAnalysis
from app.models.research import ResearchResult
from app.models.risk import RiskAnalysis, RiskFlag, RiskLevel
from app.tools.investigation import canonical_research_category


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
    if debt[-1] - debt[0] >= settings.debt_ratio_threshold:
        flags.append(
            RiskFlag(
                type="leverage",
                severity=RiskLevel.HIGH if debt[-1] >= 0.65 else RiskLevel.MEDIUM,
                description="资产负债率在观察期内显著上升。",
                evidence=["metric:debt_ratio"],
            )
        )
    if cashflow[-1] < cashflow[0]:
        flags.append(
            RiskFlag(
                type="cashflow",
                severity=RiskLevel.MEDIUM,
                description="经营现金流较观察期初下降。",
                evidence=["metric:operating_cash_flow_trend"],
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
        incomplete_evidence = [f"failed_tool:{tool}" for tool in research.failed_tools]
        if research.content_fetch_incomplete:
            incomplete_evidence.append(
                f"content_fetch_status:{research.content_fetch_status}"
            )
            if research.failed_content_count:
                incomplete_evidence.append(
                    f"content_fetch_failed:{research.failed_content_count}"
                )
        if research.verification_incomplete:
            incomplete_evidence.append(
                f"verification_status:{research.verification_execution_status}"
            )
            if research.failed_verification_count:
                incomplete_evidence.append(
                    f"verification_failed:{research.failed_verification_count}"
                )
        if not incomplete_evidence:
            incomplete_evidence.append("external_research:incomplete")
        incomplete_areas = sum(
            (
                bool(research.failed_tools),
                research.content_fetch_incomplete,
                research.verification_incomplete,
            )
        )
        if incomplete_areas > 1:
            description = "外部调查、正文获取或事实核验未全部完成，存在证据缺口。"
        elif research.content_fetch_incomplete:
            description = "外部候选正文未全部获取，存在证据缺口。"
        elif research.verification_incomplete:
            description = "外部候选事实未全部核验，存在证据缺口。"
        else:
            description = "外部调查工具调用未全部完成，存在证据缺口。"
        flags.append(
            RiskFlag(
                type="external_research",
                severity=RiskLevel.MEDIUM,
                description=description,
                evidence=incomplete_evidence,
            )
        )
    if research and research.verification_status == "CONFLICTING":
        conflicting_evidence = [
            reference
            for result in (research.company_result, research.industry_result)
            for item in result.evidence
            if item.verification_status == "CONFLICTING"
            for reference in [
                item.source_id,
                *([item.source_url] if item.source_url else []),
                *(
                    [f"evidence_location:{item.verification.evidence_location}"]
                    if item.verification and item.verification.evidence_location
                    else []
                ),
            ]
        ]
        flags.append(
            RiskFlag(
                type="external_research_conflict",
                severity=RiskLevel.MEDIUM,
                description="可靠外部来源对待核查事实存在冲突，不自动采信任一结论。",
                evidence=conflicting_evidence or ["verification_status:CONFLICTING"],
            )
        )

    if research:
        descriptions = {
            "operations": "外部核验显示企业存在经营异常或重大经营风险事项。",
            "regulatory": "外部核验显示企业存在监管处罚、罚款或纪律处分事项。",
            "legal": "外部核验显示企业涉及重大诉讼、仲裁或法院判决。",
            "debt": "外部核验显示企业存在债务逾期或违约事项。",
            "fraud": "外部核验显示企业存在财务造假、虚假记载或违规披露事项。",
            "performance": "外部核验显示企业存在业绩预亏、预减或显著下滑事项。",
            "controller": "外部核验显示企业实际控制人或控制权存在重大变化。",
            "industry_risk": "外部核验显示所在行业存在景气下行、需求收缩或产能风险。",
        }
        severities = {
            "fraud": RiskLevel.HIGH,
            "debt": RiskLevel.HIGH,
            "legal": RiskLevel.HIGH,
            "controller": RiskLevel.HIGH,
        }
        facts_by_category: dict[str, list] = {}
        for result in (research.company_result, research.industry_result):
            for fact in result.facts:
                if fact.verification_status in {"SUPPORTED", "CORROBORATED"}:
                    category = canonical_research_category(fact.category)
                    facts_by_category.setdefault(category, []).append(fact)
        for category, facts in facts_by_category.items():
            if category not in descriptions:
                continue
            evidence: list[str] = []
            for fact in facts:
                evidence.extend(
                    [
                        fact.fact_id or fact.source_id,
                        fact.source_id,
                        f"source_id:{fact.source_id}",
                        *([fact.source_url] if fact.source_url else []),
                        *([f"subject:{fact.subject}"] if fact.subject else []),
                        *([f"claim_id:{fact.claim_id}"] if fact.claim_id else []),
                        *([f"relation:{fact.relation}"] if fact.relation else []),
                        *(
                            [f"verifier_model:{fact.verifier_model}"]
                            if fact.verifier_model
                            else []
                        ),
                        *(
                            [f"verified_at:{fact.verified_at.isoformat()}"]
                            if fact.verified_at
                            else []
                        ),
                        *(
                            [f"evidence_location:{fact.evidence_location}"]
                            if fact.evidence_location
                            else []
                        ),
                    ]
                )
            flags.append(
                RiskFlag(
                    type=f"external_{category}",
                    severity=severities.get(category, RiskLevel.MEDIUM),
                    description=descriptions[category],
                    evidence=list(dict.fromkeys(evidence)),
                )
            )
        if (
            not facts_by_category
            and research.verification_status in {"UNVERIFIED", "NOT_FOUND"}
            and not research.external_research_incomplete
        ):
            flags.append(
                RiskFlag(
                    type="external_research_evidence_gap",
                    severity=RiskLevel.MEDIUM,
                    description="外部调查未形成可采信的核验事实，当前存在证据缺口。",
                    evidence=[
                        f"verification_status:{research.verification_status}",
                        f"candidate_count:{research.candidate_count}",
                    ],
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
