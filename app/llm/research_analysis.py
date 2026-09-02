"""Citation-bound research summaries and review-only Query proposals."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.llm.gateway import StructuredModel, StructuredModelError
from app.models.analysis import (
    AnalysisErrorCode,
    EvidenceGap,
    EvidenceGapReason,
    EvidenceSummaryArtifact,
    EvidenceSummaryEntry,
    QueryProposal,
    QueryProposalArtifact,
    QueryProposalDraft,
    ResearchAnalysisDraft,
)
from app.models.investigation import QueryPlan
from app.models.research import ResearchFact, ResearchResult
from app.models.search import ResearchEvidence
from app.tools.investigation import canonical_research_category

PROMPT_VERSION = "m4b-evidence-summary-query-proposal-v1"
_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "research_analysis.md"
_SUMMARY_STATUSES = {"SUPPORTED", "CORROBORATED", "CONFLICTING", "UNVERIFIED"}
_VERIFIED_STATUSES = {"SUPPORTED", "CORROBORATED"}
_INCOMPLETE_CONTENT_STATUSES = {"DISABLED", "PARTIAL", "FAILED"}
_INCOMPLETE_VERIFICATION_STATUSES = {"DISABLED", "PARTIAL", "FAILED"}


@dataclass(frozen=True)
class ResearchAnalysisArtifacts:
    """Two independently versioned artifacts emitted from one model response."""

    evidence_summary: EvidenceSummaryArtifact
    query_proposal: QueryProposalArtifact


def _digest(prefix: str, payload: object) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"{prefix}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _claim_id(item: ResearchEvidence) -> str | None:
    if item.verification is not None:
        return item.verification.claim.claim_id
    if item.verification_claim is not None:
        return item.verification_claim.claim_id
    return None


def _fact_key(fact: ResearchFact) -> tuple[str, str | None, str]:
    return (fact.source_id, fact.claim_id, fact.category)


def _evidence_records(research: ResearchResult) -> list[dict[str, Any]]:
    """Build a compact index without rejected evidence or fetched document text."""

    facts_by_key = {
        _fact_key(fact): fact
        for result in (research.company_result, research.industry_result)
        for fact in result.facts
    }
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in (research.company_result, research.industry_result):
        for item in result.candidate_evidence:
            status = item.verification_status
            if status not in _SUMMARY_STATUSES:
                continue
            claim_id = _claim_id(item)
            fact = facts_by_key.get((item.source_id, claim_id, item.category))
            source_category = item.category
            category = canonical_research_category(source_category)
            identity = {
                "query_type": result.query_type,
                "category": category,
                "source_category": source_category,
                "source_id": item.source_id,
                "claim_id": claim_id,
                "content_hash": item.content_hash,
            }
            evidence_id = _digest("evidence", identity)
            if evidence_id in seen:
                continue
            seen.add(evidence_id)
            records.append(
                {
                    "evidence_id": evidence_id,
                    "verification_status": status,
                    "query_type": result.query_type,
                    "category": category,
                    "source_category": source_category,
                    "source_id": item.source_id,
                    "claim_id": claim_id,
                    "fact_id": fact.fact_id if fact else None,
                    "fact_statement": fact.statement if fact else None,
                }
            )
    return sorted(
        records,
        key=lambda item: (
            item["query_type"],
            item["category"],
            item["source_id"],
            item["evidence_id"],
        ),
    )


def _evidence_gaps(
    research: ResearchResult, plan: QueryPlan, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    record_categories = {
        (record["query_type"], record["category"]) for record in records
    }
    result_by_type = {
        "company": research.company_result,
        "industry": research.industry_result,
    }
    gaps: list[dict[str, Any]] = []
    for request in plan.queries:
        result = result_by_type[request.query_type]
        reasons: list[EvidenceGapReason] = []
        if result.status == "FAILED":
            reasons.append("TOOL_FAILED")
        else:
            if (request.query_type, request.category) not in record_categories:
                reasons.append("NO_CANDIDATE")
            if result.content_fetch_status in _INCOMPLETE_CONTENT_STATUSES:
                reasons.append("CONTENT_FETCH_INCOMPLETE")
            if (
                result.verification_execution_status
                in _INCOMPLETE_VERIFICATION_STATUSES
            ):
                reasons.append("VERIFICATION_INCOMPLETE")
        for reason in reasons:
            payload = {
                "query_plan_id": plan.plan_id,
                "query_type": request.query_type,
                "category": request.category,
                "reason": reason,
            }
            gaps.append(
                {
                    "gap_id": _digest("gap", payload),
                    "query_type": request.query_type,
                    "category": request.category,
                    "reason": reason,
                }
            )
    return gaps


def _fallback_entry_summary(record: dict[str, Any]) -> str:
    status = record["verification_status"]
    if status in _VERIFIED_STATUSES:
        return (
            f"{status}：该条证据已形成可回查的结构化事实，"
            "仍应通过 Claim、Fact 与 Source ID 复核。"
        )
    if status == "CONFLICTING":
        return "CONFLICTING：存在相互冲突的核验信号，不作为确定事实使用。"
    return "UNVERIFIED：该候选尚未形成可用于确定结论的事实。"


def _fallback_gap_summary(reason: EvidenceGapReason) -> str:
    summaries = {
        "NO_CANDIDATE": "当前计划类别没有保留的候选证据，需要人工判断是否补充调查。",
        "TOOL_FAILED": "对应调查工具未完成，当前结果不能视为没有风险。",
        "CONTENT_FETCH_INCOMPLETE": "候选正文获取不完整，不能据此形成完整事实结论。",
        "VERIFICATION_INCOMPLETE": "正文核验不完整，候选不能升级为确定事实。",
    }
    return summaries[reason]


def _baseline_verified_summary(records: list[dict[str, Any]]) -> tuple[str, list[str]]:
    verified_ids = [
        record["evidence_id"]
        for record in records
        if record["verification_status"] in _VERIFIED_STATUSES
    ]
    if not verified_ids:
        return "确定性摘要：当前没有可纳入确定性结论的已核验事实。", []
    return (
        (
            "确定性摘要：已保留 "
            f"{len(verified_ids)} 条已核验事实；具体结论必须回查关联的 Evidence、Claim、Fact 与 Source ID。"
        ),
        verified_ids,
    )


def _fallback(
    research: ResearchResult,
    plan: QueryPlan,
    records: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    *,
    mode: str,
    execution_status: str,
    model_name: str,
    error_code: AnalysisErrorCode | None = None,
    error_message: str | None = None,
    attempts: int = 0,
) -> ResearchAnalysisArtifacts:
    verified_summary, summary_evidence_ids = _baseline_verified_summary(records)
    limitations = [
        "规则 Query Plan 是唯一执行基线；模型建议不会自动执行。",
        "候选和冲突证据不会被写入确定事实。",
    ]
    summary = EvidenceSummaryArtifact(
        intent_id=plan.intent.intent_id,
        query_plan_id=plan.plan_id,
        research_status=research.status,
        verification_status=research.verification_status,
        mode=mode,
        execution_status=execution_status,
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        verified_summary=verified_summary,
        summary_evidence_ids=summary_evidence_ids,
        entries=[
            EvidenceSummaryEntry(
                **record,
                summary=_fallback_entry_summary(record),
            )
            for record in records
        ],
        gaps=[
            EvidenceGap(
                **gap,
                summary=_fallback_gap_summary(gap["reason"]),
            )
            for gap in gaps
        ],
        limitations=limitations,
        attempts=attempts,
        error_code=error_code,
        error_message=error_message,
    )
    proposal = QueryProposalArtifact(
        intent_id=plan.intent.intent_id,
        query_plan_id=plan.plan_id,
        allowed_categories=plan.intent.categories,
        mode=mode,
        execution_status=execution_status,
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        proposals=[],
        limitations=limitations,
        attempts=attempts,
        error_code=error_code,
        error_message=error_message,
    )
    return ResearchAnalysisArtifacts(summary, proposal)


def _model_payload(
    plan: QueryPlan, records: list[dict[str, Any]], gaps: list[dict[str, Any]]
) -> dict[str, Any]:
    """Expose only IDs, verified statements, and deterministic query constraints."""

    return {
        "intent": {
            "intent_id": plan.intent.intent_id,
            "allowed_categories": plan.intent.categories,
        },
        "evidence_records": records,
        "evidence_gaps": gaps,
        "allowed_queries": [
            {
                "query_type": request.query_type,
                "category": request.category,
                "subject": request.subject,
                "baseline_query": request.query,
            }
            for request in plan.queries
        ],
    }


def _validate_summary_draft(
    draft: ResearchAnalysisDraft,
    records: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> None:
    records_by_id = {record["evidence_id"]: record for record in records}
    summaries_by_id = {
        summary.evidence_id: summary for summary in draft.evidence_summaries
    }
    if len(summaries_by_id) != len(draft.evidence_summaries) or set(
        summaries_by_id
    ) != set(records_by_id):
        raise StructuredModelError(
            "UNSUPPORTED_EVIDENCE",
            "model evidence summaries do not match the deterministic evidence index",
        )
    for evidence_id, summary in summaries_by_id.items():
        if (
            summary.verification_status
            != records_by_id[evidence_id]["verification_status"]
        ):
            raise StructuredModelError(
                "UNSUPPORTED_EVIDENCE",
                "model changed a deterministic evidence verification status",
            )
    verified_ids = {
        record["evidence_id"]
        for record in records
        if record["verification_status"] in _VERIFIED_STATUSES
    }
    if (
        len(draft.summary_evidence_ids) != len(set(draft.summary_evidence_ids))
        or set(draft.summary_evidence_ids) != verified_ids
    ):
        raise StructuredModelError(
            "UNSUPPORTED_EVIDENCE",
            "model summary must cite exactly the verified evidence ids",
        )
    gaps_by_id = {gap["gap_id"]: gap for gap in gaps}
    summaries_by_gap = {summary.gap_id: summary for summary in draft.gap_summaries}
    if len(summaries_by_gap) != len(draft.gap_summaries) or set(
        summaries_by_gap
    ) != set(gaps_by_id):
        raise StructuredModelError(
            "UNSUPPORTED_EVIDENCE",
            "model gap summaries do not match deterministic evidence gaps",
        )


def _normalized_query(value: str) -> str:
    return " ".join(value.split()).casefold()


def _proposal_id(plan: QueryPlan, index: int, proposal: QueryProposalDraft) -> str:
    return _digest(
        "query-proposal",
        {
            "query_plan_id": plan.plan_id,
            "index": index,
            "proposal": proposal.model_dump(mode="json"),
        },
    )


def _validate_proposals(
    drafts: list[QueryProposalDraft],
    plan: QueryPlan,
    records: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
) -> list[QueryProposal]:
    allowed_query_types = {
        request.category: {
            item.query_type
            for item in plan.queries
            if item.category == request.category
        }
        for request in plan.queries
    }
    subjects_by_type: dict[str, set[str]] = {"company": set(), "industry": set()}
    for request in plan.queries:
        subjects_by_type[request.query_type].add(request.subject)
    baseline_queries = {_normalized_query(request.query) for request in plan.queries}
    allowed_reference_ids = {
        *(record["evidence_id"] for record in records),
        *(record["source_id"] for record in records),
        *(gap["gap_id"] for gap in gaps),
    }
    seen_signatures: set[tuple[str, str, str]] = set()
    proposals: list[QueryProposal] = []
    for index, draft in enumerate(drafts, start=1):
        reasons: list[str] = []
        allowed_types = allowed_query_types.get(draft.category)
        if allowed_types is None:
            reasons.append("UNAUTHORIZED_CATEGORY")
        elif draft.query_type not in allowed_types:
            reasons.append("INVALID_QUERY_TYPE")
        query_lower = draft.query.casefold()
        if any(character in draft.query for character in ("\r", "\n")) or any(
            token in query_lower for token in ("http://", "https://")
        ):
            reasons.append("INVALID_QUERY_FORMAT")
        allowed_subjects = subjects_by_type.get(draft.query_type, set())
        if not any(subject.casefold() in query_lower for subject in allowed_subjects):
            reasons.append("SUBJECT_MISMATCH")
        if not set(draft.reference_ids).issubset(allowed_reference_ids):
            reasons.append("UNKNOWN_REFERENCE")
        normalized = _normalized_query(draft.query)
        if normalized in baseline_queries:
            reasons.append("DUPLICATE_BASELINE_QUERY")
        signature = (draft.query_type, draft.category, normalized)
        if signature in seen_signatures:
            reasons.append("DUPLICATE_PROPOSAL")
        seen_signatures.add(signature)
        rejection_reasons = list(dict.fromkeys(reasons))
        proposals.append(
            QueryProposal(
                proposal_id=_proposal_id(plan, index, draft),
                query_type=draft.query_type,
                category=draft.category,
                query=draft.query,
                reference_ids=draft.reference_ids,
                rationale=draft.rationale,
                decision=("REJECTED" if rejection_reasons else "ACCEPTED_FOR_REVIEW"),
                rejection_reasons=rejection_reasons,
            )
        )
    return proposals


def _completed_artifacts(
    research: ResearchResult,
    plan: QueryPlan,
    records: list[dict[str, Any]],
    gaps: list[dict[str, Any]],
    draft: ResearchAnalysisDraft,
    *,
    model_name: str,
    attempts: int,
    input_tokens: int | None,
    output_tokens: int | None,
) -> ResearchAnalysisArtifacts:
    summaries_by_id = {
        summary.evidence_id: summary for summary in draft.evidence_summaries
    }
    gaps_by_id = {summary.gap_id: summary for summary in draft.gap_summaries}
    limitations = list(
        dict.fromkeys(
            [
                *draft.limitations,
                "规则 Query Plan 是唯一执行基线；模型建议不会自动执行。",
            ]
        )
    )
    summary = EvidenceSummaryArtifact(
        intent_id=plan.intent.intent_id,
        query_plan_id=plan.plan_id,
        research_status=research.status,
        verification_status=research.verification_status,
        mode="llm",
        execution_status="COMPLETE",
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        verified_summary=(
            draft.verified_summary
            if draft.summary_evidence_ids
            else _baseline_verified_summary(records)[0]
        ),
        summary_evidence_ids=draft.summary_evidence_ids,
        entries=[
            EvidenceSummaryEntry(
                **record,
                summary=(
                    summaries_by_id[record["evidence_id"]].summary
                    or _fallback_entry_summary(record)
                ),
            )
            for record in records
        ],
        gaps=[
            EvidenceGap(
                **gap,
                summary=gaps_by_id[gap["gap_id"]].summary,
            )
            for gap in gaps
        ],
        limitations=limitations,
        attempts=attempts,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    proposal = QueryProposalArtifact(
        intent_id=plan.intent.intent_id,
        query_plan_id=plan.plan_id,
        allowed_categories=plan.intent.categories,
        mode="llm",
        execution_status="COMPLETE",
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        proposals=_validate_proposals(draft.query_proposals, plan, records, gaps),
        limitations=limitations,
        attempts=attempts,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return ResearchAnalysisArtifacts(summary, proposal)


def build_research_analysis(
    research: ResearchResult,
    plan: QueryPlan,
    *,
    analysis_mode: str,
    model: StructuredModel | None,
    model_name: str,
    initialization_error: StructuredModelError | None = None,
) -> ResearchAnalysisArtifacts:
    """Return bounded summaries and review-only proposals without changing QueryPlan."""

    records = _evidence_records(research)
    gaps = _evidence_gaps(research, plan, records)
    if analysis_mode == "deterministic":
        return _fallback(
            research,
            plan,
            records,
            gaps,
            mode="deterministic",
            execution_status="NOT_REQUESTED",
            model_name="deterministic-template-v1",
        )
    if not records and not gaps:
        return _fallback(
            research,
            plan,
            records,
            gaps,
            mode="llm",
            execution_status="NOT_NEEDED",
            model_name=model_name or "unconfigured",
        )
    if initialization_error is not None or model is None:
        error = initialization_error or StructuredModelError(
            "CONFIG_ERROR", "structured analysis model is unavailable"
        )
        return _fallback(
            research,
            plan,
            records,
            gaps,
            mode="llm",
            execution_status="DEGRADED",
            model_name=model_name or "unconfigured",
            error_code=error.code,
            error_message=str(error),
            attempts=error.attempts,
        )
    try:
        result = model.generate(
            output_schema=ResearchAnalysisDraft,
            purpose="evidence_summary_query_proposal",
            prompt_version=PROMPT_VERSION,
            system_prompt=_PROMPT_PATH.read_text(encoding="utf-8"),
            payload=_model_payload(plan, records, gaps),
        )
        _validate_summary_draft(result.output, records, gaps)
        return _completed_artifacts(
            research,
            plan,
            records,
            gaps,
            result.output,
            model_name=result.model_name,
            attempts=result.attempts,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
    except (OSError, StructuredModelError) as exc:
        error = (
            exc
            if isinstance(exc, StructuredModelError)
            else StructuredModelError(
                "CONFIG_ERROR", "research analysis prompt unavailable"
            )
        )
    except Exception:  # noqa: BLE001 - model failures must not abort Research
        error = StructuredModelError(
            "MODEL_ERROR", "structured research analysis failed"
        )
    return _fallback(
        research,
        plan,
        records,
        gaps,
        mode="llm",
        execution_status="DEGRADED",
        model_name=model.model_name,
        error_code=error.code,
        error_message=str(error),
        attempts=error.attempts,
    )
