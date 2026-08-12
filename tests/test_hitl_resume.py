"""Cross-process acceptance tests for the full HITL durable workflow."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_MODULE = "app.task_cli"


def prepare_data_dir(tmp_path: Path, *case_ids: str) -> Path:
    data_dir = tmp_path / "data"
    for case_id in case_ids:
        shutil.copytree(
            PROJECT_ROOT / "data" / case_id / "source",
            data_dir / case_id / "source",
        )
    return data_dir


def run_cli(
    db_path: Path,
    data_dir: Path,
    *arguments: str,
    expected_code: int = 0,
) -> dict:
    process = subprocess.run(
        [
            sys.executable,
            "-m",
            CLI_MODULE,
            "--db",
            str(db_path),
            "--data-dir",
            str(data_dir),
            *arguments,
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    assert process.returncode == expected_code, process.stderr or process.stdout
    return json.loads(process.stdout.strip())


def test_risky_case_interrupts_then_resumes_in_new_process(tmp_path: Path) -> None:
    data_dir = prepare_data_dir(tmp_path, "case_risky")
    db_path = tmp_path / "checkpoints" / "credra.db"
    thread_id = "hitl-approve-001"

    started = run_cli(
        db_path,
        data_dir,
        "start",
        "--thread-id",
        thread_id,
        "--case-id",
        "case_risky",
    )

    assert started["state"]["status"] == "WAITING_APPROVAL"
    assert started["state"]["current_node"] == "risk"
    assert started["next"] == ["approval"]
    assert started["interrupts"][0]["value"]["risk_level"] == "HIGH"
    assert started["interrupts"][0]["value"]["allowed_decisions"] == [
        "approve",
        "research",
    ]

    # A separate status process proves the checkpoint is sufficient by itself.
    persisted = run_cli(
        db_path,
        data_dir,
        "status",
        "--thread-id",
        thread_id,
    )
    assert persisted == started

    resumed = run_cli(
        db_path,
        data_dir,
        "resume",
        "--thread-id",
        thread_id,
        "--decision",
        "approve",
        "--comment",
        "风险已由人工复核，继续形成报告。",
    )

    assert resumed["state"]["status"] == "COMPLETED"
    assert resumed["state"]["current_node"] == "report"
    assert resumed["state"]["human_decision"] == "approve"
    assert resumed["state"]["human_comment"] == "风险已由人工复核，继续形成报告。"
    assert resumed["next"] == []
    assert resumed["interrupts"] == []
    report_path = data_dir / "case_risky" / "output" / "credit_report.md"
    assert report_path.is_file()
    assert "风险已由人工复核" in report_path.read_text(encoding="utf-8")

    with sqlite3.connect(db_path) as connection:
        checkpoint_count = connection.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?", (thread_id,)
        ).fetchone()[0]
    assert checkpoint_count > 0


def test_research_decision_versions_artifacts_and_interrupts_again(
    tmp_path: Path,
) -> None:
    data_dir = prepare_data_dir(tmp_path, "case_risky")
    db_path = tmp_path / "credra.db"
    thread_id = "hitl-research-001"
    case_dir = data_dir / "case_risky"

    run_cli(
        db_path,
        data_dir,
        "start",
        "--thread-id",
        thread_id,
        "--case-id",
        "case_risky",
    )
    researched = run_cli(
        db_path,
        data_dir,
        "resume",
        "--thread-id",
        thread_id,
        "--decision",
        "research",
        "--comment",
        "需要补充调查后再审核。",
    )

    assert researched["state"]["status"] == "WAITING_APPROVAL"
    assert researched["state"]["human_decision"] == "research"
    assert researched["state"]["research_artifact"] == (
        "artifacts/research_result_v2.json"
    )
    assert researched["state"]["risk_artifact"] == "artifacts/risk_analysis_v2.json"
    assert researched["next"] == ["approval"]
    assert researched["interrupts"]
    assert not (case_dir / "output" / "credit_report.md").exists()

    for artifact_name in (
        "research_result_v1.json",
        "research_result_v2.json",
        "risk_analysis_v1.json",
        "risk_analysis_v2.json",
    ):
        assert (case_dir / "artifacts" / artifact_name).is_file()

    completed = run_cli(
        db_path,
        data_dir,
        "resume",
        "--thread-id",
        thread_id,
        "--decision",
        "approve",
        "--comment",
        "补充调查已完成。",
    )
    assert completed["state"]["status"] == "COMPLETED"
    assert completed["state"]["human_decision"] == "approve"
    assert completed["state"]["risk_artifact"] == "artifacts/risk_analysis_v2.json"
    assert (case_dir / "output" / "credit_report.md").is_file()


def test_low_risk_case_completes_without_interrupt(tmp_path: Path) -> None:
    data_dir = prepare_data_dir(tmp_path, "case_normal")
    db_path = tmp_path / "credra.db"

    completed = run_cli(
        db_path,
        data_dir,
        "start",
        "--thread-id",
        "normal-001",
        "--case-id",
        "case_normal",
    )

    assert completed["state"]["status"] == "COMPLETED"
    assert completed["state"]["risk_level"] == "LOW"
    assert completed["interrupts"] == []
    assert (data_dir / "case_normal" / "output" / "credit_report.md").is_file()


def test_cli_rejects_duplicate_start_and_non_waiting_resume(tmp_path: Path) -> None:
    data_dir = prepare_data_dir(tmp_path, "case_normal")
    db_path = tmp_path / "credra.db"
    thread_id = "normal-errors-001"

    run_cli(
        db_path,
        data_dir,
        "start",
        "--thread-id",
        thread_id,
        "--case-id",
        "case_normal",
    )
    duplicate = run_cli(
        db_path,
        data_dir,
        "start",
        "--thread-id",
        thread_id,
        "--case-id",
        "case_normal",
        expected_code=2,
    )
    assert duplicate == {"error": f"thread already exists: {thread_id}"}

    invalid_resume = run_cli(
        db_path,
        data_dir,
        "resume",
        "--thread-id",
        thread_id,
        "--decision",
        "approve",
        expected_code=2,
    )
    assert invalid_resume == {
        "error": f"thread is not waiting for approval: {thread_id}"
    }
