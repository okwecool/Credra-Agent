"""P10 CLI and Chainlit projections remain offline and reviewable."""

import json
from datetime import date
from pathlib import Path

from app.chainlit_app import _intent_markdown
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
    assert "当前不会因这条消息自动发起外部调用" in markdown
