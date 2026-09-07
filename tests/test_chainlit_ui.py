"""Non-server checks for the Chainlit workbench rendering boundary."""

import asyncio
import shutil
from pathlib import Path

import pytest

from app.chainlit_app import (
    _BACKGROUND_OPERATIONS,
    _audit_step_views,
    _case_catalog_markdown,
    _flow_element,
    _flow_view,
    _live_task_list,
    _node_states,
    _report_elements,
    _risk_markdown,
    _task_list,
    _track_background_operation,
    discover_cases,
    serialize_for_debug,
)
from app.workbench_live import project_live_progress

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

    flow = _flow_view(payload)
    failed = next(node for node in flow["nodes"] if node["id"] == "document")
    assert flow["workflowStatus"] == "FAILED"
    assert flow["currentNode"] == "document"
    assert failed["status"] == "failed"


def test_chainlit_flow_element_projects_waiting_review_without_raw_data() -> None:
    payload = {
        "thread_id": "flow-001",
        "state": {
            "case_id": "case_saic_600104",
            "run_id": "run-flow-001",
            "status": "WAITING_APPROVAL",
            "current_node": "risk",
            "risk_level": "MEDIUM",
            "company_artifact": "artifacts/company_profile_v1.json",
            "financial_artifact": "artifacts/financial_analysis_v1.json",
            "research_artifact": None,
            "risk_artifact": "artifacts/risk_analysis_v1.json",
        },
        "next": ["approval"],
        "interrupts": [{"value": {"risk_flags": []}}],
        "secret": "must-not-reach-flow-props",
    }

    flow = _flow_view(payload)
    statuses = {node["id"]: node["status"] for node in flow["nodes"]}
    element = _flow_element(payload)

    assert flow["caseId"] == "case_saic_600104"
    assert flow["threadId"] == "flow-001"
    assert flow["nextNodes"] == ["approval"]
    assert flow["progress"] == {"processed": 4, "total": 6, "percent": 67}
    assert statuses == {
        "document": "done",
        "financial": "done",
        "research": "skipped",
        "risk": "done",
        "approval": "waiting",
        "report": "pending",
    }
    assert element.name == "AgentFlow"
    assert element.display == "inline"
    assert element.props == flow
    assert "must-not-reach-flow-props" not in serialize_for_debug(element.props)

    task_list = _task_list(payload)
    assert task_list.status == "等待人工审核"
    assert [task.title for task in task_list.tasks] == [
        "材料解析 · 已完成",
        "财务分析 · 已完成",
        "补充调查 · 本轮跳过",
        "风险分析 · 已完成",
        "人工审核 · 等待操作",
        "报告生成 · 待执行",
    ]
    assert [task.status.value for task in task_list.tasks] == [
        "done",
        "done",
        "done",
        "done",
        "running",
        "ready",
    ]


def test_chainlit_flow_view_marks_low_risk_review_as_skipped() -> None:
    payload = {
        "thread_id": "flow-complete",
        "state": {
            "case_id": "case_normal",
            "status": "COMPLETED",
            "current_node": "report",
            "risk_level": "LOW",
            "company_artifact": "artifacts/company_profile_v1.json",
            "financial_artifact": "artifacts/financial_analysis_v1.json",
            "risk_artifact": "artifacts/risk_analysis_v1.json",
            "report_artifact": "artifacts/report_v1.json",
        },
        "next": [],
        "interrupts": [],
    }

    flow = _flow_view(payload)
    statuses = {node["id"]: node["status"] for node in flow["nodes"]}

    assert flow["progress"] == {"processed": 6, "total": 6, "percent": 100}
    assert statuses["research"] == "skipped"
    assert statuses["approval"] == "skipped"
    assert statuses["report"] == "done"


def test_chainlit_live_task_list_maps_trace_projection() -> None:
    projection = project_live_progress(
        [
            {
                "node": "document",
                "event_type": "NODE_START",
                "status": "SUCCESS",
                "latency_ms": 0,
            }
        ],
        thread_id="live-ui-001",
        case_id="case_normal",
    )

    task_list = _live_task_list(projection)

    assert task_list.thread_id == "live-ui-001"
    assert task_list.status == "实时执行中"
    assert task_list.tasks[0].title == "材料解析 · 执行中"
    assert task_list.tasks[0].status.value == "running"
    assert task_list.tasks[1].status.value == "ready"


@pytest.mark.asyncio
async def test_chainlit_background_operation_survives_handler_cancellation() -> None:
    finished = asyncio.Event()

    async def operation() -> dict:
        await asyncio.sleep(0.01)
        finished.set()
        return {"status": "done"}

    async def browser_handler() -> None:
        task = _track_background_operation(asyncio.create_task(operation()))
        await asyncio.sleep(60)
        await task

    handler = asyncio.create_task(browser_handler())
    await asyncio.sleep(0)
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler

    await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.sleep(0)
    assert not _BACKGROUND_OPERATIONS


def test_chainlit_audit_steps_are_bounded_filtered_and_redacted() -> None:
    events = [
        {
            "node": "runtime",
            "event_type": "TASK_START",
            "status": "SUCCESS",
            "end_time": "2026-09-06T01:00:00Z",
            "latency_ms": 0,
            "output_summary": "excluded noise",
        },
        {
            "node": "document",
            "event_type": "NODE_END",
            "status": "SUCCESS",
            "end_time": "2026-09-06T01:00:01Z",
            "latency_ms": 12,
            "output_summary": "company artifact ready",
        },
        {
            "node": "research",
            "event_type": "RETRY",
            "status": "RETRY",
            "end_time": "2026-09-06T01:00:02Z",
            "latency_ms": 50,
            "error": "api_key=top-secret timeout",
        },
        {
            "node": "risk",
            "event_type": "LLM_PROCESS_SUMMARY",
            "status": "SUCCESS",
            "end_time": "2026-09-06T01:00:02.500000Z",
            "latency_ms": 0,
            "output_summary": "public_summary=已核对风险项与允许引用。",
        },
        {
            "node": "approval",
            "event_type": "INTERRUPT",
            "status": "SUCCESS",
            "end_time": "2026-09-06T01:00:03Z",
            "latency_ms": 0,
            "output_summary": "waiting for approve or research",
        },
    ]

    views = _audit_step_views({"trace_events": events})

    assert [view["kind"] for view in views] == [
        "Workflow",
        "Retry",
        "LLM Process",
        "HITL",
    ]
    assert views[0]["name"] == "材料解析 · 节点执行"
    assert views[0]["icon"] == "Workflow"
    assert views[1]["defaultOpen"] is True
    assert views[2]["defaultOpen"] is False
    assert views[3]["defaultOpen"] is True
    serialized = serialize_for_debug(views)
    assert "top-secret" not in serialized
    assert "外部服务响应超时" in serialized
    assert "excluded noise" not in serialized
    assert "已核对风险项与允许引用" in serialized

    many_events = [
        {
            "node": "report",
            "event_type": "NODE_END",
            "status": "SUCCESS",
            "end_time": f"2026-09-06T01:01:{index:02d}Z",
            "latency_ms": index,
        }
        for index in range(20)
    ]
    bounded = _audit_step_views({"trace_events": many_events})
    assert len(bounded) == 12
    assert bounded[0]["latencyMs"] == 8


def test_chainlit_audit_steps_hide_resume_time_interrupt_replay() -> None:
    events = [
        {
            "node": "approval",
            "event_type": "INTERRUPT",
            "status": "SUCCESS",
            "end_time": "2026-09-06T01:00:00Z",
            "output_summary": "waiting for approve or research",
        },
        {
            "node": "runtime",
            "event_type": "TASK_STATE",
            "status": "SUCCESS",
            "end_time": "2026-09-06T01:00:01Z",
            "output_summary": "status=WAITING_APPROVAL",
        },
        {
            "node": "runtime",
            "event_type": "RESUME",
            "status": "SUCCESS",
            "end_time": "2026-09-06T01:01:00Z",
            "input_summary": "decision=approve",
        },
        {
            "node": "approval",
            "event_type": "INTERRUPT",
            "status": "SUCCESS",
            "end_time": "2026-09-06T01:01:01Z",
            "output_summary": "waiting for approve or research",
        },
        {
            "node": "approval",
            "event_type": "NODE_END",
            "status": "SUCCESS",
            "end_time": "2026-09-06T01:01:02Z",
            "output_summary": "keys=human_decision,status",
        },
    ]

    views = _audit_step_views({"trace_events": events})

    assert [view["kind"] for view in views] == ["HITL", "Resume", "Workflow"]
    assert sum(view["kind"] == "HITL" for view in views) == 1


def test_chainlit_discovers_and_preflights_cases(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    for case_id in ("case_normal", "case_risky", "case_byd_002594", "case_saic_600104"):
        shutil.copytree(
            PROJECT_ROOT / "data" / case_id / "source",
            data_dir / case_id / "source",
        )
    (data_dir / "not-a-case" / "source").mkdir(parents=True)

    cases = discover_cases(data_dir)
    markdown = _case_catalog_markdown(cases)

    assert [item["case_id"] for item in cases] == [
        "case_byd_002594",
        "case_saic_600104",
    ]
    assert all(item["valid"] for item in cases)
    assert "case_byd_002594" in markdown
    assert "case_saic_600104" in markdown
    assert "case_normal" not in markdown
    assert "case_risky" not in markdown
    assert "not-a-case" not in markdown

    all_cases = discover_cases(data_dir, include_hidden=True)
    assert [item["case_id"] for item in all_cases] == [
        "case_byd_002594",
        "case_normal",
        "case_risky",
        "case_saic_600104",
    ]
    assert all(item["valid"] for item in all_cases)


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
