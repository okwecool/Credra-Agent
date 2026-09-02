"""Citation-checked risk narrative generation with deterministic fallback."""

from __future__ import annotations

from pathlib import Path

from app.llm.gateway import StructuredModel, StructuredModelError
from app.models.analysis import (
    AnalysisErrorCode,
    RiskNarrativeArtifact,
    RiskNarrativeDraft,
    RiskNarrativeExplanation,
)
from app.models.risk import RiskAnalysis

PROMPT_VERSION = "m4a-risk-narrative-v1"
_PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "risk_narrative.md"


def _risk_entries(risk: RiskAnalysis) -> list[dict]:
    return [
        {
            "risk_id": f"risk:{index}:{flag.type}",
            "type": flag.type,
            "severity": flag.severity.value,
            "description": flag.description,
            "allowed_evidence_ids": flag.evidence,
        }
        for index, flag in enumerate(risk.risk_flags, start=1)
    ]


def _fallback(
    risk: RiskAnalysis,
    *,
    mode: str,
    execution_status: str,
    model_name: str,
    error_code: AnalysisErrorCode | None = None,
    error_message: str | None = None,
    attempts: int = 0,
) -> RiskNarrativeArtifact:
    entries = _risk_entries(risk)
    all_evidence = list(
        dict.fromkeys(
            evidence for entry in entries for evidence in entry["allowed_evidence_ids"]
        )
    )
    return RiskNarrativeArtifact(
        risk_level=risk.risk_level.value,
        mode=mode,
        execution_status=execution_status,
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
        overall_summary=risk.summary,
        summary_evidence_ids=all_evidence,
        explanations=[
            RiskNarrativeExplanation(
                risk_id=entry["risk_id"],
                explanation=entry["description"],
                evidence_ids=entry["allowed_evidence_ids"],
            )
            for entry in entries
        ],
        limitations=["当前内容使用确定性风险描述，未采用模型扩写。"],
        attempts=attempts,
        error_code=error_code,
        error_message=error_message,
    )


def _validate_citations(draft: RiskNarrativeDraft, entries: list[dict]) -> None:
    allowed_by_risk = {
        entry["risk_id"]: set(entry["allowed_evidence_ids"]) for entry in entries
    }
    explanations = {item.risk_id: item for item in draft.explanations}
    if len(explanations) != len(draft.explanations) or set(explanations) != set(
        allowed_by_risk
    ):
        raise StructuredModelError(
            "UNSUPPORTED_EVIDENCE",
            "model explanations do not match deterministic risk ids",
        )
    all_allowed = set().union(*allowed_by_risk.values())
    if not set(draft.summary_evidence_ids).issubset(all_allowed):
        raise StructuredModelError(
            "UNSUPPORTED_EVIDENCE",
            "model summary referenced evidence outside the risk artifact",
        )
    for risk_id, explanation in explanations.items():
        if not set(explanation.evidence_ids).issubset(allowed_by_risk[risk_id]):
            raise StructuredModelError(
                "UNSUPPORTED_EVIDENCE",
                "model explanation referenced evidence outside its risk flag",
            )


def build_risk_narrative(
    risk: RiskAnalysis,
    *,
    analysis_mode: str,
    model: StructuredModel | None,
    model_name: str,
    initialization_error: StructuredModelError | None = None,
) -> RiskNarrativeArtifact:
    if analysis_mode == "deterministic":
        return _fallback(
            risk,
            mode="deterministic",
            execution_status="NOT_REQUESTED",
            model_name="deterministic-template-v1",
        )
    if not risk.risk_flags:
        return _fallback(
            risk,
            mode="llm",
            execution_status="NOT_NEEDED",
            model_name=model_name or "unconfigured",
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
            error_code=error.code,
            error_message=str(error),
            attempts=error.attempts,
        )

    entries = _risk_entries(risk)
    try:
        result = model.generate(
            output_schema=RiskNarrativeDraft,
            purpose="risk_narrative",
            prompt_version=PROMPT_VERSION,
            system_prompt=_PROMPT_PATH.read_text(encoding="utf-8"),
            payload={
                "risk_level": risk.risk_level.value,
                "deterministic_summary": risk.summary,
                "risk_flags": entries,
            },
        )
        _validate_citations(result.output, entries)
    except (OSError, StructuredModelError) as exc:
        error = (
            exc
            if isinstance(exc, StructuredModelError)
            else StructuredModelError(
                "CONFIG_ERROR", "risk narrative prompt unavailable"
            )
        )
        return _fallback(
            risk,
            mode="llm",
            execution_status="DEGRADED",
            model_name=model.model_name,
            error_code=error.code,
            error_message=str(error),
            attempts=error.attempts,
        )
    return RiskNarrativeArtifact(
        risk_level=risk.risk_level.value,
        mode="llm",
        execution_status="COMPLETE",
        model_name=result.model_name,
        prompt_version=PROMPT_VERSION,
        overall_summary=result.output.overall_summary,
        summary_evidence_ids=result.output.summary_evidence_ids,
        explanations=result.output.explanations,
        limitations=result.output.limitations,
        attempts=result.attempts,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
