"""M4-C2 cited Executive Summary and Report Draft generation."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from app.agents.document import normalize_company_documents
from app.agents.financial import analyze_financials
from app.config import Settings
from app.llm.gateway import StructuredModelError, StructuredModelResult
from app.llm.report_draft import build_report_draft
from app.mcp.research_client import ResearchMCPClient
from app.mcp.research_server import mcp as research_mcp
from app.models.financial import FinancialStatement
from app.report_expression import build_report_expression
from app.runtime.tasks import resume_task, start_task
from app.runtime.tracing import read_trace
from tests.test_m4a_analysis import _risk
from tests.test_m4b_research_analysis import _MainChainModel

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _inputs():
    source = PROJECT_ROOT / "data/case_risky/source"
    company = normalize_company_documents(source)
    financial = analyze_financials(
        "case_risky",
        FinancialStatement.model_validate_json(
            (source / "financial_statement.json").read_text(encoding="utf-8")
        ),
    )
    return company, financial, _risk()


class _ReportDraftModel:
    model_name = "mock-qwen"

    def __init__(self, *, unknown_reference: bool = False, unsafe: bool = False):
        self.unknown_reference = unknown_reference
        self.unsafe = unsafe
        self.calls: list[str] = []

    def generate(self, *, output_schema: type, purpose: str, payload: dict, **_: Any):
        self.calls.append(purpose)
        sources = {
            item["reference_id"]: item["statement"] for item in payload["source_index"]
        }
        reference = "metric:unknown" if self.unknown_reference else "risk:summary"
        output = output_schema.model_validate(
            {
                "executive_summary": (
                    "详见 https://evil.example，预计损失 99%，建议批准授信。"
                    if self.unsafe
                    else sources["risk:summary"]
                ),
                "executive_summary_reference_ids": [reference],
                "sections": [
                    {
                        "section": "financial_analysis",
                        "text": sources["metric:current_ratio"],
                        "reference_ids": ["metric:current_ratio"],
                    },
                    {
                        "section": "risk_analysis",
                        "text": sources["risk:summary"],
                        "reference_ids": ["risk:summary"],
                    },
                ],
                "limitations": ["仅使用提供的确定性来源。"],
            }
        )
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            latency_ms=2,
            input_tokens=80,
            output_tokens=40,
        )


def test_deterministic_report_draft_never_calls_model() -> None:
    company, financial, risk = _inputs()
    model = _ReportDraftModel()

    artifact = build_report_draft(
        company,
        financial,
        risk,
        None,
        external_research_incomplete=False,
        analysis_mode="deterministic",
        model=model,
        model_name=model.model_name,
    )

    assert model.calls == []
    assert artifact.execution_status == "NOT_REQUESTED"
    assert artifact.mode == "deterministic"


def test_report_draft_accepts_only_known_references() -> None:
    company, financial, risk = _inputs()
    model = _ReportDraftModel()

    artifact = build_report_draft(
        company,
        financial,
        risk,
        None,
        external_research_incomplete=False,
        analysis_mode="llm",
        model=model,
        model_name=model.model_name,
    )

    assert model.calls == ["report_draft"]
    assert artifact.execution_status == "COMPLETE"
    assert artifact.executive_summary_reference_ids == ["risk:summary"]
    assert {item.section for item in artifact.sections} == {
        "financial_analysis",
        "risk_analysis",
    }


def test_unknown_report_reference_degrades_before_final_projection() -> None:
    company, financial, risk = _inputs()

    artifact = build_report_draft(
        company,
        financial,
        risk,
        None,
        external_research_incomplete=False,
        analysis_mode="llm",
        model=_ReportDraftModel(unknown_reference=True),
        model_name="mock-qwen",
    )

    assert artifact.execution_status == "DEGRADED"
    assert artifact.error_code == "UNSUPPORTED_EVIDENCE"
    assert artifact.sections == []


def test_unavailable_report_model_degrades_without_exposing_error_details() -> None:
    company, financial, risk = _inputs()

    artifact = build_report_draft(
        company,
        financial,
        risk,
        None,
        external_research_incomplete=False,
        analysis_mode="llm",
        model=None,
        model_name="mock-qwen",
        initialization_error=StructuredModelError(
            "CONFIG_ERROR", "structured analysis model is unavailable"
        ),
    )

    assert artifact.execution_status == "DEGRADED"
    assert artifact.error_code == "CONFIG_ERROR"
    assert artifact.sections == []
    assert artifact.executive_summary == risk.summary


def test_second_pass_removes_unsupported_report_url_and_number() -> None:
    company, financial, risk = _inputs()
    artifact = build_report_draft(
        company,
        financial,
        risk,
        None,
        external_research_incomplete=False,
        analysis_mode="llm",
        model=_ReportDraftModel(unsafe=True),
        model_name="mock-qwen",
    )

    expression = build_report_expression(
        risk,
        analysis_mode="llm",
        company=company,
        financial=financial,
        report_draft=artifact,
    )

    assert artifact.execution_status == "COMPLETE"
    assert expression.execution_status == "PARTIAL"
    assert expression.executive_summary is None
    assert expression.report_sections
    assert {claim.reason for claim in expression.unsupported_claims} == {
        "UNSUPPORTED_DECISION",
        "UNSUPPORTED_NUMBER",
        "UNSUPPORTED_URL",
    }
    serialized = expression.model_dump_json()
    assert "evil.example" not in serialized
    assert "99%" not in serialized
    assert "批准授信" not in serialized


def test_report_node_calls_third_bounded_purpose_and_persists_audit(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(
        PROJECT_ROOT / "data/case_risky/source",
        data_dir / "case_risky/source",
    )
    settings = Settings(
        _env_file=None,
        analysis_mode="llm",
        model_name="mock-qwen",
        model_api_key="test-key",
        data_dir=data_dir,
        checkpoint_db_path=tmp_path / "credra.db",
        trace_dir=tmp_path / "traces",
    )
    model = _MainChainModel()
    thread_id = "m4c2-main-chain"

    started = start_task(
        thread_id=thread_id,
        case_id="case_risky",
        settings=settings,
        research_client=ResearchMCPClient(research_mcp),
        analysis_model=model,
    )
    completed = resume_task(
        thread_id=thread_id,
        decision="approve",
        comment="已人工复核，继续生成报告。",
        settings=settings,
        research_client=ResearchMCPClient(research_mcp),
        analysis_model=model,
    )

    assert started["state"]["status"] == "WAITING_APPROVAL"
    assert completed["state"]["status"] == "COMPLETED"
    assert model.calls == [
        "evidence_summary_query_proposal",
        "risk_narrative",
        "report_draft",
    ]
    run_dir = data_dir / "case_risky/runs" / completed["state"]["run_id"]
    draft = json.loads(
        (run_dir / "artifacts/report_draft_v1.json").read_text(encoding="utf-8")
    )
    expression = json.loads(
        (run_dir / "artifacts/report_expression_v1.json").read_text(encoding="utf-8")
    )
    report = (run_dir / "output/credit_report.md").read_text(encoding="utf-8")
    assert draft["execution_status"] == "COMPLETE"
    assert expression["report_draft_status"] == "COMPLETE"
    assert expression["executive_summary"]
    assert "执行摘要（受约束模型表达）" in report
    assert "Report Draft 源状态：COMPLETE" in report
    events = read_trace(tmp_path / "traces/m4c2-main-chain.jsonl")
    report_calls = [
        event
        for event in events
        if event["event_type"] == "LLM_CALL"
        and event["input_summary"] == "purpose=report_draft"
    ]
    assert len(report_calls) == 1
    assert report_calls[0]["status"] == "SUCCESS"
    serialized_trace = json.dumps(events, ensure_ascii=False)
    assert "test-key" not in serialized_trace
    assert draft["executive_summary"] not in serialized_trace
