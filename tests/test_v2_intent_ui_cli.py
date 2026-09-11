"""P10 CLI and Chainlit projections remain offline and reviewable."""

import json
from datetime import date
from pathlib import Path

from app.chainlit_app import _flow_view, _intent_markdown, _risk_markdown
from app.task_cli import main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_task_cli_parse_persists_ready_task_spec(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "--db",
            str(tmp_path / "cli.db"),
            "--data-dir",
            str(PROJECT_ROOT / "data"),
            "parse",
            "--thread-id",
            "natural-cli",
            "--message-id",
            "natural-cli-1",
            "--as-of",
            "2026-09-08",
            "--text",
            "调查比亚迪2025年的现金质量",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["task_spec"]["subject_id"] == "002594"
    assert payload["task_spec"]["readiness"] == "READY"
    assert payload["execution_status"] == "NOT_STARTED"


def test_chainlit_projection_discloses_deferred_execution(tmp_path) -> None:
    from credra_agent.intent.service import interpret_message

    result = interpret_message(
        thread_id="ui-projection",
        source_message_id="ui-projection-1",
        text="比较去年和今年的现金流",
        as_of=date(2026, 9, 8),
        data_dir=PROJECT_ROOT / "data",
        database_path=tmp_path / "ui.db",
    )

    markdown = _intent_markdown(result)
    assert "NEEDS_CLARIFICATION" in markdown
    assert "subject_id" in markdown
    assert "comparable_periods" in markdown
    assert "不会自行创建运行授权或发起付费调用" in markdown


def test_task_cli_agent_requires_explicit_authorization(tmp_path, capsys) -> None:
    exit_code = main(
        [
            "--db",
            str(tmp_path / "agent-cli.db"),
            "--data-dir",
            str(PROJECT_ROOT / "data"),
            "agent",
            "--thread-id",
            "natural-agent-cli",
            "--message-id",
            "natural-agent-cli-1",
            "--as-of",
            "2026-09-08",
            "--text",
            "调查比亚迪2025年的监管消息",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["outcome"] == "AUTHORIZATION_REQUIRED"
    assert payload["task"] is None


def test_chainlit_projects_agentic_waiting_and_limited_states() -> None:
    waiting = {
        "thread_id": "agentic-ui",
        "next": [],
        "state": {
            "graph_version": "agentic_v2",
            "execution_mode": "agentic",
            "case_id": "case_byd_002594",
            "run_id": "a" * 20,
            "status": "WAITING_CLARIFICATION",
            "current_node": "execute",
            "iteration": 1,
            "task_spec_ref": "artifacts/agent_task_spec_v1.json",
            "authorization_ref": "artifacts/agent_run_authorization_v1.json",
            "hypotheses_ref": "artifacts/agent_hypotheses_v1.json",
            "observation_index_ref": "artifacts/agent_observation_index_v2.json",
            "coverage_ref": "artifacts/agent_coverage_v1.json",
            "budget_ledger_ref": "artifacts/agent_budget_ledger_v2.json",
        },
    }

    flow = _flow_view(waiting)
    assert flow["workflowStatusLabel"] == "🟠 等待用户澄清"
    assert [item["id"] for item in flow["nodes"]] == ["decide", "execute"]
    assert flow["nodes"][1]["status"] == "waiting"
    assert "Run Authorization" in _risk_markdown(waiting)

    limited = {
        **waiting,
        "state": {**waiting["state"], "status": "LIMITED"},
    }
    assert _flow_view(limited)["workflowStatusLabel"] == "🟡 受限结束"
