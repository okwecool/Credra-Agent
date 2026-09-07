"""Citation-bound Report Draft generation with deterministic fallback."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.llm.gateway import (
    ModelProgressCallback,
    StructuredModel,
    StructuredModelError,
)
from app.llm.research_analysis import build_research_evidence_index
from app.models.analysis import (
    AnalysisErrorCode,
    ReportDraft,
    ReportDraftArtifact,
)
from app.models.company import CompanyProfile
from app.models.financial import FinancialAnalysis
from app.models.research import ResearchResult
from app.models.risk import RiskAnalysis

PROMPT_VERSION = "m4c2-report-draft-v1"
_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "report_draft.md"


def _metric_statement(metric_id: str, metric: Any) -> str:
    values = "；".join(
        f"{year}: {value:.4f}" for year, value in zip(metric.years, metric.values)
    )
    return f"{metric_id}：{values} {metric.unit}。"


def build_report_source_index(
    company: CompanyProfile,
    financial: FinancialAnalysis,
    risk: RiskAnalysis,
    research: ResearchResult | None,
    *,
    external_research_incomplete: bool,
) -> dict[str, str]:
    """Build the only deterministic statements the Report Draft may cite."""

    sources: dict[str, str] = {
        "company:profile": (
            f"企业名称为{company.company_name}，行业为{company.industry}，"
            f"注册资本为{company.registered_capital}，成立日期为"
            f"{company.established_date}，主营范围为{company.business_scope}。"
        ),
        "risk:summary": (f"确定性风险等级为{risk.risk_level.value}；{risk.summary}"),
        "research:status": (
            "外部调查未全部完成，存在证据缺口。"
            if external_research_incomplete
            else "外部调查执行状态正常。"
        ),
    }
    for metric_id, metric in sorted(financial.metrics.items()):
        sources[f"metric:{metric_id}"] = _metric_statement(metric_id, metric)
    for index, flag in enumerate(risk.risk_flags, start=1):
        sources[f"risk:{index}:{flag.type}"] = (
            f"风险严重度为{flag.severity.value}；{flag.description}"
        )
    if research is not None:
        for record in build_research_evidence_index(research):
            if record["verification_status"] not in {"SUPPORTED", "CORROBORATED"}:
                continue
            statement = record.get("fact_statement")
            if not statement:
                continue
            sources[record["evidence_id"]] = statement
            if fact_id := record.get("fact_id"):
                sources[fact_id] = statement
    return dict(sorted(sources.items()))


def report_source_fingerprint(source_index: dict[str, str]) -> str:
    serialized = json.dumps(
        source_index, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return "sha256:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _fallback(
    risk: RiskAnalysis,
    *,
    mode: str,
    execution_status: str,
    model_name: str,
    source_fingerprint: str,
    error_code: AnalysisErrorCode | None = None,
    error_message: str | None = None,
    attempts: int = 0,
) -> ReportDraftArtifact:
    return ReportDraftArtifact(
        mode=mode,
        execution_status=execution_status,
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        source_fingerprint=source_fingerprint,
        executive_summary=risk.summary,
        executive_summary_reference_ids=["risk:summary"],
        limitations=["当前报告表达使用确定性模板，未采用 Report Draft 模型输出。"],
        attempts=attempts,
        error_code=error_code,
        error_message=error_message,
    )


def _validate_references(draft: ReportDraft, source_index: dict[str, str]) -> None:
    allowed = set(source_index)
    reference_groups = [
        draft.executive_summary_reference_ids,
        *(section.reference_ids for section in draft.sections),
    ]
    if any(
        len(references) != len(set(references)) or not set(references).issubset(allowed)
        for references in reference_groups
    ):
        raise StructuredModelError(
            "UNSUPPORTED_EVIDENCE",
            "report draft referenced a source outside the deterministic index",
        )


def build_report_draft(
    company: CompanyProfile,
    financial: FinancialAnalysis,
    risk: RiskAnalysis,
    research: ResearchResult | None,
    *,
    external_research_incomplete: bool,
    analysis_mode: str,
    model: StructuredModel | None,
    model_name: str,
    initialization_error: StructuredModelError | None = None,
    progress_callback: ModelProgressCallback | None = None,
) -> ReportDraftArtifact:
    """Generate a cited draft without granting tools or changing source Artifacts."""

    source_index = build_report_source_index(
        company,
        financial,
        risk,
        research,
        external_research_incomplete=external_research_incomplete,
    )
    fingerprint = report_source_fingerprint(source_index)
    if analysis_mode == "deterministic":
        return _fallback(
            risk,
            mode="deterministic",
            execution_status="NOT_REQUESTED",
            model_name="deterministic-template-v1",
            source_fingerprint=fingerprint,
        )
    if initialization_error is not None or model is None:
        error = initialization_error or StructuredModelError(
            "CONFIG_ERROR", "structured analysis model is unavailable"
        )
        return _fallback(
            risk,
            mode="llm",
            execution_status="DEGRADED",
            model_name=model_name or "unconfigured",
            source_fingerprint=fingerprint,
            error_code=error.code,
            error_message=str(error),
            attempts=error.attempts,
        )
    try:
        result = model.generate(
            output_schema=ReportDraft,
            purpose="report_draft",
            prompt_version=PROMPT_VERSION,
            system_prompt=_PROMPT_PATH.read_text(encoding="utf-8"),
            payload={
                "source_fingerprint": fingerprint,
                "source_index": [
                    {"reference_id": reference_id, "statement": statement}
                    for reference_id, statement in source_index.items()
                ],
            },
            progress_callback=progress_callback,
        )
        _validate_references(result.output, source_index)
    except (OSError, StructuredModelError) as exc:
        error = (
            exc
            if isinstance(exc, StructuredModelError)
            else StructuredModelError("CONFIG_ERROR", "report draft prompt unavailable")
        )
        return _fallback(
            risk,
            mode="llm",
            execution_status="DEGRADED",
            model_name=model.model_name,
            source_fingerprint=fingerprint,
            error_code=error.code,
            error_message=str(error),
            attempts=error.attempts,
        )
    return ReportDraftArtifact(
        mode="llm",
        execution_status="COMPLETE",
        model_name=result.model_name,
        prompt_version=PROMPT_VERSION,
        source_fingerprint=fingerprint,
        executive_summary=result.output.executive_summary,
        executive_summary_reference_ids=(result.output.executive_summary_reference_ids),
        sections=result.output.sections,
        limitations=result.output.limitations,
        attempts=result.attempts,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
