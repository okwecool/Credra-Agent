"""Constrained model gateway and main-chain analysis services."""

from app.llm.gateway import (
    OpenAICompatibleStructuredModel,
    StructuredModel,
    StructuredModelError,
    StructuredModelResult,
    build_analysis_model,
)
from app.llm.report_draft import build_report_draft
from app.llm.research_analysis import build_research_analysis
from app.llm.risk_narrative import build_risk_narrative

__all__ = [
    "OpenAICompatibleStructuredModel",
    "StructuredModel",
    "StructuredModelError",
    "StructuredModelResult",
    "build_analysis_model",
    "build_report_draft",
    "build_research_analysis",
    "build_risk_narrative",
]
