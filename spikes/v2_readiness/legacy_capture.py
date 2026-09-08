"""Capture two offline legacy task snapshots without model/search calls."""

import argparse
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

from app.config import Settings
from app.runtime.tasks import get_task_status, resume_task, start_task

ROOT = Path(__file__).resolve().parents[2]


def offline_settings(directory: Path) -> Settings:
    return Settings(
        _env_file=None,
        analysis_mode="deterministic",
        model_api_key="",
        analysis_llm_enable_thinking=False,
        research_provider="mock",
        tavily_api_key="",
        content_fetch_provider="disabled",
        fact_verifier="rules",
        data_dir=directory / "data",
        trace_dir=directory / "traces",
        checkpoint_db_path=directory / "checkpoints.sqlite",
    )


def write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def capture(directory: Path) -> dict:
    """Write new fixtures only; never overwrite an existing run or user data."""
    directory.mkdir(parents=True, exist_ok=False)
    settings = offline_settings(directory)
    for case in ("case_normal", "case_saic_600104"):
        # Both bypass Research in the established baseline, so no child/provider
        # is needed and public source values remain exactly the existing fixture.
        shutil.copytree(
            ROOT / "data" / case / "source", directory / "data" / case / "source"
        )
    waiting = start_task(
        thread_id="v2-legacy-waiting", case_id="case_saic_600104", settings=settings
    )
    completed = start_task(
        thread_id="v2-legacy-completed", case_id="case_normal", settings=settings
    )
    assert waiting["state"]["status"] == "WAITING_APPROVAL"
    assert completed["state"]["status"] == "COMPLETED"
    assert "graph_version" not in waiting["state"]
    write_json(directory / "waiting.json", waiting)
    write_json(directory / "completed.json", completed)
    # Keep an independent frozen SQLite backup, not a live WAL/shm sidecar.
    with (
        sqlite3.connect(settings.checkpoint_db_path) as source,
        sqlite3.connect(directory / "legacy_frozen.sqlite") as destination,
    ):
        source.backup(destination)
    fingerprints = {
        str(path.relative_to(directory)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }
    result = {
        "source_revision": "d6178c6",
        "graph_version_present": False,
        "external_call_count": 0,
        "threads": [waiting["thread_id"], completed["thread_id"]],
        "files": fingerprints,
    }
    write_json(directory / "manifest.json", result)
    return result


def verify(directory: Path) -> dict:
    """Run in a new process: inspect and resume only an isolated working copy."""
    settings = offline_settings(directory)
    waiting = get_task_status(thread_id="v2-legacy-waiting", settings=settings)
    completed = get_task_status(thread_id="v2-legacy-completed", settings=settings)
    assert waiting == json.loads(
        (directory / "waiting.json").read_text(encoding="utf-8")
    )
    assert completed == json.loads(
        (directory / "completed.json").read_text(encoding="utf-8")
    )
    store = (
        directory
        / "data"
        / "case_saic_600104"
        / "runs"
        / waiting["state"]["run_id"]
        / "artifacts"
    )
    before = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in store.iterdir()
    }
    resumed = resume_task(
        thread_id="v2-legacy-waiting",
        decision="approve",
        comment="V2 legacy compatibility fixture reviewed.",
        settings=settings,
    )
    assert resumed["state"]["status"] == "COMPLETED"
    assert all(
        hashlib.sha256((store / name).read_bytes()).hexdigest() == digest
        for name, digest in before.items()
    )
    return {
        "status": "PASS",
        "completed_status": completed["state"]["status"],
        "resumed_status": resumed["state"]["status"],
        "completed_artifacts_unchanged": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("capture", "verify"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = (
        capture(args.directory) if args.command == "capture" else verify(args.directory)
    )
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
