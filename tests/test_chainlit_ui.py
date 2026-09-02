"""Non-server checks for the Chainlit workbench rendering boundary."""

import shutil
from pathlib import Path

from app.chainlit_app import (
    _case_catalog_markdown,
    _node_states,
    _report_elements,
    _risk_markdown,
    discover_cases,
    serialize_for_debug,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_chainlit_risk_payload_renders_status_and_evidence() -> None:
    payload = {
        "thread_id": "ui-001",
        "state": {
            "case_id": "case_risky",
            "run_id": "run-001",
            "status": "WAITING_APPROVAL",
            "current_node": "risk",
            "risk_level": "HIGH",
            "company_artifact": "artifacts/company_profile_v1.json",
            "financial_artifact": "artifacts/financial_analysis_v1.json",
            "research_artifact": "artifacts/research_result_v1.json",
            "risk_artifact": "artifacts/risk_analysis_v1.json",
        },
        "next": ["approval"],
        "interrupts": [
            {
                "value": {
                    "risk_flags": [
                        {
                            "severity": "HIGH",
                            "description": "资产负债率上升。",
                            "evidence": ["metric:debt_ratio", "source-001"],
                        }
                    ]
                }
            }
        ],
    }

    markdown = _risk_markdown(payload)

    assert "ui-001" in markdown
    assert "WAITING_APPROVAL" in markdown
    assert "run-001" in markdown
    assert "approval" in markdown
    assert "执行进度" in markdown
    assert "Artifact 引用" in markdown
    assert "资产负债率上升" in markdown
    assert "source-001" in markdown
    assert '"thread_id": "ui-001"' in serialize_for_debug(payload)


def test_chainlit_failed_payload_explains_terminal_state() -> None:
    payload = {
        "thread_id": "failed-001",
        "state": {
            "case_id": "case_broken",
            "run_id": "broken-run",
            "status": "FAILED",
            "current_node": "document",
            "failed_node": "document",
            "error_summary": "FileNotFoundError: source missing",
        },
        "next": [],
        "interrupts": [],
    }

    markdown = _risk_markdown(payload)

    assert "执行失败" in markdown
    assert "失败节点" in markdown
    assert "FileNotFoundError" in markdown
    assert _node_states(payload)["document"] == "失败"


def test_chainlit_discovers_and_preflights_cases(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    for case_id in ("case_normal", "case_byd_002594"):
        shutil.copytree(
            PROJECT_ROOT / "data" / case_id / "source",
            data_dir / case_id / "source",
        )
    (data_dir / "not-a-case" / "source").mkdir(parents=True)

    cases = discover_cases(data_dir)
    markdown = _case_catalog_markdown(cases)

    assert [item["case_id"] for item in cases] == [
        "case_byd_002594",
        "case_normal",
    ]
    assert all(item["valid"] for item in cases)
    assert "case_byd_002594" in markdown
    assert "not-a-case" not in markdown


def test_chainlit_report_elements_use_in_memory_content() -> None:
    payload = {"state": {"case_id": "case_demo"}}
    elements = _report_elements(
        payload,
        {
            "report_markdown": "# Report\n",
            "report_html": "<!doctype html><p>Report</p>",
        },
    )

    assert [element.name for element in elements] == [
        "case_demo_credit_report.md",
        "case_demo_credit_report.html",
    ]
    assert elements[0].content == b"# Report\n"
    assert elements[1].mime == "text/html"
    assert all(element.path is None for element in elements)
