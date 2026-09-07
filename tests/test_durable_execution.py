"""Cross-process acceptance tests for the Day 0 durable execution spike."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_MODULE = "spikes.durable_execution.cli"


def run_cli(db_path: Path, *arguments: str, expected_code: int = 0) -> dict:
    process = subprocess.run(
        [sys.executable, "-m", CLI_MODULE, "--db", str(db_path), *arguments],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert process.returncode == expected_code, process.stderr or process.stdout
    return json.loads(process.stdout.strip())


def test_interrupt_survives_process_exit_and_resumes(tmp_path: Path) -> None:
    db_path = tmp_path / "checkpoints" / "spike.db"
    thread_id = "cross-process-001"

    started = run_cli(db_path, "start", "--thread-id", thread_id)

    assert db_path.is_file()
    assert started["exists"] is True
    assert started["state"]["status"] == "WAITING_APPROVAL"
    assert started["state"]["prepare_runs"] == 1
    assert started["next"] == ["approval"]
    assert started["interrupts"][0]["value"]["task_id"] == thread_id

    # This command runs in another Python process and reads only from SQLite.
    persisted = run_cli(db_path, "status", "--thread-id", thread_id)
    assert persisted == started

    resumed = run_cli(
        db_path,
        "resume",
        "--thread-id",
        thread_id,
        "--decision",
        "approve",
    )

    assert resumed["state"]["status"] == "COMPLETED"
    assert resumed["state"]["decision"] == "approve"
    assert resumed["state"]["result"] == "approved"
    assert resumed["state"]["prepare_runs"] == 1
    assert resumed["next"] == []
    assert resumed["interrupts"] == []

    with sqlite3.connect(db_path) as connection:
        checkpoint_count = connection.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
    assert checkpoint_count > 0


def test_cli_rejects_unknown_resume_and_duplicate_start(tmp_path: Path) -> None:
    db_path = tmp_path / "spike.db"

    missing = run_cli(
        db_path,
        "resume",
        "--thread-id",
        "missing-thread",
        "--decision",
        "approve",
        expected_code=2,
    )
    assert missing == {"error": "thread does not exist: missing-thread"}

    run_cli(db_path, "start", "--thread-id", "existing-thread")
    duplicate = run_cli(
        db_path,
        "start",
        "--thread-id",
        "existing-thread",
        expected_code=2,
    )
    assert duplicate == {"error": "thread already exists: existing-thread"}
