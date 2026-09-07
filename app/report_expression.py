"""Second-pass validation for model prose included in the fixed report."""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

from app.llm.report_draft import (
    build_report_source_index,
    report_source_fingerprint,
)
from app.llm.research_analysis import (
    build_research_evidence_gaps,
    build_research_evidence_index,
)
from app.models.analysis import (
    EvidenceSummaryArtifact,
    QueryProposalArtifact,
    ReportClaimComponent,
    ReportDraftArtifact,
    ReportEvidenceGap,
    ReportEvidenceSummaryItem,
    ReportExpressionArtifact,
    ReportExpressionSection,
    ReportQuerySuggestion,
    ReportRiskExplanation,
    RiskNarrativeArtifact,
    UnsupportedReportClaim,
    UnsupportedReportClaimReason,
)
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis
from app.models.investigation import QueryPlan
from app.models.research import ResearchResult
from app.models.risk import RiskAnalysis

_URL_PATTERN = re.compile(r"(?i)(?:https?://|www\.)")
_CREDIT_DECISION_PATTERN = re.compile(
    r"(?i)(?:(?:建议|应当|应|可以|同意|批准|拒绝|不予).{0,12}"
    r"(?:授信|贷款|放款)|(?:授信|贷款|放款).{0,12}"
    r"(?:同意|批准|拒绝|不予)|(?:approve|reject|grant|deny).{0,12}"
    r"(?:credit|loan))"
)
_NUMBER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:])[-+]?(?:\d+(?:,\d{3})*(?:\.\d+)?|\.\d+)%?"
    r"(?![A-Za-z0-9_])"
)


def _normalized_numbers(text: str) -> set[str]:
    normalized: set[str] = set()
    for match in _NUMBER_PATTERN.finditer(text):
        raw = match.group(0).replace(",", "")
        suffix = "%" if raw.endswith("%") else ""
        value = raw.removesuffix("%")
        try:
            decimal = Decimal(value)
        except InvalidOperation:
            normalized.add(raw)
            continue
        canonical = format(decimal.normalize(), "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        normalized.add(f"{canonical}{suffix}")
    return normalized


def _text_rejections(
    text: str, allowed_sources: Iterable[str]
) -> list[UnsupportedReportClaimReason]:
    reasons: list[UnsupportedReportClaimReason] = []
    if _URL_PATTERN.search(text):
        reasons.append("UNSUPPORTED_URL")
    if _CREDIT_DECISION_PATTERN.search(text):
        reasons.append("UNSUPPORTED_DECISION")
    allowed_numbers = {
        number for source in allowed_sources for number in _normalized_numbers(source)
    }
    if not _normalized_numbers(text).issubset(allowed_numbers):
        reasons.append("UNSUPPORTED_NUMBER")
    return reasons


def _risk_entries(risk: RiskAnalysis) -> dict[str, dict[str, object]]:
    return {
        f"risk:{index}:{flag.type}": {
            "description": flag.description,
            "evidence_ids": set(flag.evidence),
        }
        for index, flag in enumerate(risk.risk_flags, start=1)
    }


def build_report_expression(
    risk: RiskAnalysis,
    *,
    analysis_mode: str,
    company: CompanyProfile | None = None,
    financial: FinancialAnalysis | None = None,
    external_research_incomplete: bool = False,
    research: ResearchResult | None = None,
    plan: QueryPlan | None = None,
    risk_narrative: RiskNarrativeArtifact | None = None,
    evidence_summary: EvidenceSummaryArtifact | None = None,
    query_proposal: QueryProposalArtifact | None = None,
    report_draft: ReportDraftArtifact | None = None,
    source_artifacts: dict[str, str] | None = None,
    invalid_sources: Iterable[ReportClaimComponent] = (),
) -> ReportExpressionArtifact:
    """Accept only cross-checked model prose; never calculate or alter facts."""

    unsupported: list[UnsupportedReportClaim] = []

    def reject(
        component: ReportClaimComponent,
        item_id: str,
        reason: UnsupportedReportClaimReason,
    ) -> None:
        claim = UnsupportedReportClaim(
            component=component,
            item_id=item_id,
            reason=reason,
        )
        if claim not in unsupported:
            unsupported.append(claim)

    for component in invalid_sources:
        reject(component, "source_artifact", "SOURCE_INVALID")

    model_names = list(
        dict.fromkeys(
            artifact.model_name
            for artifact in (
                risk_narrative,
                evidence_summary,
                query_proposal,
                report_draft,
            )
            if artifact is not None and artifact.mode == "llm"
        )
    )
    executive_summary: str | None = None
    executive_summary_reference_ids: list[str] = []
    report_sections: list[ReportExpressionSection] = []
    report_sources: dict[str, str] = {}
    if company is not None and financial is not None:
        report_sources = build_report_source_index(
            company,
            financial,
            risk,
            research,
            external_research_incomplete=external_research_incomplete,
        )
    if report_draft is not None and report_draft.mode == "llm":
        if report_draft.execution_status != "COMPLETE":
            reject("report_draft", "source_artifact", "SOURCE_NOT_COMPLETE")
        elif (
            not report_sources
            or report_draft.source_fingerprint
            != report_source_fingerprint(report_sources)
        ):
            reject("report_draft", "source_artifact", "SOURCE_MISMATCH")
        else:
            summary_reasons: list[UnsupportedReportClaimReason] = []
            summary_references = report_draft.executive_summary_reference_ids
            if len(summary_references) != len(set(summary_references)) or not set(
                summary_references
            ).issubset(report_sources):
                summary_reasons.append("UNSUPPORTED_EVIDENCE")
            else:
                summary_reasons.extend(
                    _text_rejections(
                        report_draft.executive_summary,
                        [report_sources[item] for item in summary_references],
                    )
                )
            for reason in dict.fromkeys(summary_reasons):
                reject("report_draft", "executive_summary", reason)
            if not summary_reasons:
                executive_summary = report_draft.executive_summary
                executive_summary_reference_ids = summary_references
            for section in report_draft.sections:
                reasons: list[UnsupportedReportClaimReason] = []
                if len(section.reference_ids) != len(
                    set(section.reference_ids)
                ) or not set(section.reference_ids).issubset(report_sources):
                    reasons.append("UNSUPPORTED_EVIDENCE")
                else:
                    reasons.extend(
                        _text_rejections(
                            section.text,
                            [report_sources[item] for item in section.reference_ids],
                        )
                    )
                for reason in dict.fromkeys(reasons):
                    reject("report_draft", section.section, reason)
                if not reasons:
                    report_sections.append(
                        ReportExpressionSection(
                            section=section.section,
                            text=section.text,
                            reference_ids=section.reference_ids,
                        )
                    )

    risk_summary: str | None = None
    risk_summary_ids: list[str] = []
    risk_explanations: list[ReportRiskExplanation] = []
    risk_index = _risk_entries(risk)
    if risk_narrative is not None and risk_narrative.mode == "llm":
        if risk_narrative.execution_status != "COMPLETE":
            reject("risk_narrative", "source_artifact", "SOURCE_NOT_COMPLETE")
        elif risk_narrative.risk_level != risk.risk_level.value:
            reject("risk_narrative", "source_artifact", "SOURCE_MISMATCH")
        else:
            narrative_ids = [item.risk_id for item in risk_narrative.explanations]
            if len(set(narrative_ids)) != len(narrative_ids) or set(
                narrative_ids
            ) != set(risk_index):
                reject("risk_narrative", "source_artifact", "SOURCE_MISMATCH")
            else:
                all_risk_evidence = set().union(
                    *(entry["evidence_ids"] for entry in risk_index.values())
                )
                if not set(risk_narrative.summary_evidence_ids).issubset(
                    all_risk_evidence
                ):
                    reject(
                        "risk_narrative",
                        "overall_summary",
                        "UNSUPPORTED_EVIDENCE",
                    )
                else:
                    summary_sources = [
                        risk.summary,
                        *(str(entry["description"]) for entry in risk_index.values()),
                        str(len(risk.risk_flags)),
                    ]
                    summary_rejections = _text_rejections(
                        risk_narrative.overall_summary, summary_sources
                    )
                    for reason in summary_rejections:
                        reject("risk_narrative", "overall_summary", reason)
                    if not summary_rejections:
                        risk_summary = risk_narrative.overall_summary
                        risk_summary_ids = risk_narrative.summary_evidence_ids
                for explanation in risk_narrative.explanations:
                    entry = risk_index[explanation.risk_id]
                    reasons: list[UnsupportedReportClaimReason] = []
                    if not set(explanation.evidence_ids).issubset(
                        entry["evidence_ids"]
                    ):
                        reasons.append("UNSUPPORTED_EVIDENCE")
                    reasons.extend(
                        _text_rejections(
                            explanation.explanation,
                            [str(entry["description"]), risk.summary],
                        )
                    )
                    for reason in dict.fromkeys(reasons):
                        reject("risk_narrative", explanation.risk_id, reason)
                    if not reasons:
                        risk_explanations.append(
                            ReportRiskExplanation(
                                risk_id=explanation.risk_id,
                                explanation=explanation.explanation,
                                evidence_ids=explanation.evidence_ids,
                            )
                        )

    evidence_overall_summary: str | None = None
    evidence_summary_ids: list[str] = []
    evidence_items: list[ReportEvidenceSummaryItem] = []
    evidence_gaps: list[ReportEvidenceGap] = []
    expected_records: dict[str, dict] = {}
    expected_gaps: dict[str, dict] = {}
    if research is not None:
        expected_records = {
            item["evidence_id"]: item
            for item in build_research_evidence_index(research)
        }
        if plan is not None:
            expected_gaps = {
                item["gap_id"]: item
                for item in build_research_evidence_gaps(
                    research, plan, list(expected_records.values())
                )
            }
    if evidence_summary is not None and evidence_summary.mode == "llm":
        if evidence_summary.execution_status != "COMPLETE":
            reject("evidence_summary", "source_artifact", "SOURCE_NOT_COMPLETE")
        elif (
            research is None
            or plan is None
            or evidence_summary.intent_id != plan.intent.intent_id
            or evidence_summary.query_plan_id != plan.plan_id
            or research.query_plan_id != plan.plan_id
        ):
            reject("evidence_summary", "source_artifact", "SOURCE_MISMATCH")
        else:
            verified_entries = [
                item
                for item in evidence_summary.entries
                if item.verification_status in {"SUPPORTED", "CORROBORATED"}
            ]
            verified_ids = {item.evidence_id for item in verified_entries}
            summary_ids_are_exact = (
                len(evidence_summary.summary_evidence_ids)
                == len(set(evidence_summary.summary_evidence_ids))
                and set(evidence_summary.summary_evidence_ids) == verified_ids
            )
            if not summary_ids_are_exact:
                reject("evidence_summary", "overall_summary", "UNSUPPORTED_EVIDENCE")
            accepted_evidence_ids: set[str] = set()
            verified_fact_statements: list[str] = []
            for item in verified_entries:
                expected = expected_records.get(item.evidence_id)
                reasons: list[UnsupportedReportClaimReason] = []
                if expected is None:
                    reasons.append("UNSUPPORTED_EVIDENCE")
                elif (
                    item.source_id != expected["source_id"]
                    or item.claim_id != expected["claim_id"]
                    or item.fact_id != expected["fact_id"]
                    or item.fact_statement != expected["fact_statement"]
                    or item.verification_status != expected["verification_status"]
                ) or item.fact_statement is None:
                    reasons.append("UNSUPPORTED_FACT")
                else:
                    reasons.extend(
                        _text_rejections(item.summary, [item.fact_statement])
                    )
                for reason in dict.fromkeys(reasons):
                    reject("evidence_summary", item.evidence_id, reason)
                if not reasons:
                    evidence_items.append(
                        ReportEvidenceSummaryItem(
                            evidence_id=item.evidence_id,
                            verification_status=item.verification_status,
                            summary=item.summary,
                            source_id=item.source_id,
                            claim_id=item.claim_id,
                            fact_id=item.fact_id,
                        )
                    )
                    accepted_evidence_ids.add(item.evidence_id)
                    verified_fact_statements.append(item.fact_statement)
            if summary_ids_are_exact and accepted_evidence_ids == verified_ids:
                summary_rejections = _text_rejections(
                    evidence_summary.verified_summary,
                    [*verified_fact_statements, str(len(verified_fact_statements))],
                )
                for reason in summary_rejections:
                    reject("evidence_summary", "overall_summary", reason)
                if not summary_rejections:
                    evidence_overall_summary = evidence_summary.verified_summary
                    evidence_summary_ids = evidence_summary.summary_evidence_ids
            for gap in evidence_summary.gaps:
                expected = expected_gaps.get(gap.gap_id)
                reasons = []
                if expected is None or any(
                    (
                        gap.query_type != expected.get("query_type"),
                        gap.category != expected.get("category"),
                        gap.reason != expected.get("reason"),
                    )
                ):
                    reasons.append("SOURCE_MISMATCH")
                reasons.extend(
                    _text_rejections(
                        gap.summary,
                        [gap.reason, gap.category, str(len(evidence_summary.gaps))],
                    )
                )
                for reason in dict.fromkeys(reasons):
                    reject("evidence_gap", gap.gap_id, reason)
                if not reasons:
                    evidence_gaps.append(ReportEvidenceGap(**gap.model_dump()))

    query_suggestions: list[ReportQuerySuggestion] = []
    rejected_proposals = 0
    if query_proposal is not None and query_proposal.mode == "llm":
        rejected_proposals = sum(
            item.decision == "REJECTED" for item in query_proposal.proposals
        )
        if query_proposal.execution_status != "COMPLETE":
            reject("query_proposal", "source_artifact", "SOURCE_NOT_COMPLETE")
        elif (
            plan is None
            or query_proposal.intent_id != plan.intent.intent_id
            or query_proposal.query_plan_id != plan.plan_id
            or query_proposal.allowed_categories != plan.intent.categories
        ):
            reject("query_proposal", "source_artifact", "SOURCE_MISMATCH")
        else:
            allowed_pairs = {
                (request.query_type, request.category): request.subject
                for request in plan.queries
            }
            allowed_references = {
                *expected_records,
                *(item["source_id"] for item in expected_records.values()),
                *expected_gaps,
            }
            for item in query_proposal.proposals:
                if item.decision != "ACCEPTED_FOR_REVIEW":
                    continue
                subject = allowed_pairs.get((item.query_type, item.category))
                invalid_reference = (
                    subject is None
                    or subject.casefold() not in item.query.casefold()
                    or not set(item.reference_ids).issubset(allowed_references)
                )
                if invalid_reference:
                    reject(
                        "query_proposal",
                        item.proposal_id,
                        "INVALID_QUERY_REFERENCE",
                    )
                    rejected_proposals += 1
                    continue
                reference_sources = [subject, item.category]
                for reference_id in item.reference_ids:
                    if record := expected_records.get(reference_id):
                        reference_sources.extend(
                            filter(
                                None,
                                (
                                    record.get("fact_statement"),
                                    record.get("category"),
                                    record.get("source_id"),
                                ),
                            )
                        )
                    if gap := expected_gaps.get(reference_id):
                        reference_sources.extend(
                            filter(
                                None,
                                (
                                    gap.get("reason"),
                                    gap.get("category"),
                                ),
                            )
                        )
                text_rejections = [
                    *_text_rejections(item.query, reference_sources),
                    *_text_rejections(item.rationale, reference_sources),
                ]
                if text_rejections:
                    for reason in dict.fromkeys(text_rejections):
                        reject("query_proposal", item.proposal_id, reason)
                    rejected_proposals += 1
                    continue
                query_suggestions.append(
                    ReportQuerySuggestion(
                        proposal_id=item.proposal_id,
                        query_type=item.query_type,
                        category=item.category,
                        query=item.query,
                        reference_ids=item.reference_ids,
                        rationale=item.rationale,
                    )
                )

    has_expression = bool(
        executive_summary
        or report_sections
        or risk_summary
        or risk_explanations
        or evidence_overall_summary
        or evidence_items
        or evidence_gaps
        or query_suggestions
    )
    if not has_expression:
        execution_status = "FALLBACK"
    elif unsupported:
        execution_status = "PARTIAL"
    else:
        execution_status = "COMPLETE"
    limitations = [
        "财务数字、风险等级、事实状态和人工决定均来自确定性 Artifact。",
        "后续调查建议仅供人工审核，未被自动执行。",
    ]
    if execution_status == "FALLBACK":
        limitations.append("未采用模型表达，报告完整使用确定性模板内容。")
    elif unsupported:
        limitations.append("不受支持的模型陈述已从最终报告中排除。")
    return ReportExpressionArtifact(
        mode="llm" if analysis_mode == "llm" else "deterministic",
        execution_status=execution_status,
        risk_narrative_status=(
            risk_narrative.execution_status if risk_narrative else None
        ),
        evidence_summary_status=(
            evidence_summary.execution_status if evidence_summary else None
        ),
        query_proposal_status=(
            query_proposal.execution_status if query_proposal else None
        ),
        report_draft_status=(report_draft.execution_status if report_draft else None),
        model_names=model_names,
        executive_summary=executive_summary,
        executive_summary_reference_ids=executive_summary_reference_ids,
        report_sections=report_sections,
        risk_overall_summary=risk_summary,
        risk_summary_evidence_ids=risk_summary_ids,
        risk_explanations=risk_explanations,
        evidence_overall_summary=evidence_overall_summary,
        evidence_summary_ids=evidence_summary_ids,
        evidence_items=evidence_items,
        evidence_gaps=evidence_gaps,
        query_suggestions=query_suggestions,
        rejected_query_proposal_count=rejected_proposals,
        unsupported_claims=unsupported,
        source_artifacts=source_artifacts or {},
        limitations=limitations,
    )
