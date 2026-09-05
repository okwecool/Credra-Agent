"""M5-B acceptance tests for portable, bounded audit exports."""

import hashlib
import json
import shutil
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from app import audit_cli
from app.audit import export_audit_bundle
from app.config import Settings
from app.runtime.tasks import resume_task, start_task

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "case_byd_002594"


def _settings(tmp_path: Path) -> Settings:
    data_dir = tmp_path / "data"
    shutil.copytree(
        PROJECT_ROOT / "data" / CASE_ID / "source",
        data_dir / CASE_ID / "source",
    )
    return Settings(
        _env_file=None,
        analysis_mode="deterministic",
        model_api_key="model-secret-sentinel",
        research_provider="mock",
        tavily_api_key="search-secret-sentinel",
        content_fetch_provider="disabled",
        fact_verifier="rules",
        data_dir=data_dir,
        checkpoint_db_path=tmp_path / "checkpoints" / "credra.db",
        trace_dir=tmp_path / "traces",
    )


def _complete_researched_task(settings: Settings, thread_id: str) -> dict[str, object]:
    started = start_task(thread_id=thread_id, case_id=CASE_ID, settings=settings)
    assert started["state"]["status"] == "WAITING_APPROVAL"
    researched = resume_task(
        thread_id=thread_id,
        decision="research",
        comment="使用离线检索提供方生成可审计调查记录。",
        settings=settings,
    )
    assert researched["state"]["status"] == "WAITING_APPROVAL"
    completed = resume_task(
        thread_id=thread_id,
        decision="approve",
        comment="已人工核验调查结果，同意形成报告。",
        settings=settings,
    )
    assert completed["state"]["status"] == "COMPLETED"
    return completed


def _archive_members(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as bundle:
        return {name: bundle.read(name) for name in bundle.namelist()}


def test_completed_task_exports_portable_checksummed_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _settings(tmp_path)
    thread_id = "audit-byd-completed"
    completed = _complete_researched_task(settings, thread_id)

    result = export_audit_bundle(
        thread_id=thread_id,
        settings=settings,
        output_dir=tmp_path / "exports",
    )
    archive = Path(result.archive_path)
    members = _archive_members(archive)
    expected = {
        "report.md",
        "report.html",
        "source_manifest.json",
        "evidence.json",
        "artifact_manifest.json",
        "trace_summary.json",
        "runtime_metrics.json",
        "manifest.json",
    }

    assert archive.is_file()
    assert expected <= members.keys()
    assert result.file_count == len(members)
    assert result.archive_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert not any(
        name.endswith((".db", ".jsonl"))
        or "snapshot" in name.lower()
        or name.lower().endswith(".env")
        for name in members
    )
    for name in members:
        normalized = PurePosixPath(name)
        assert not normalized.is_absolute()
        assert ".." not in normalized.parts

    manifest = json.loads(members["manifest.json"])
    assert manifest["case_id"] == CASE_ID
    assert manifest["thread_id"] == thread_id
    assert manifest["run_id"] == completed["state"]["run_id"]
    for entry in manifest["files"]:
        payload = members[entry["path"]]
        assert entry["byte_size"] == len(payload)
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()

    artifact_manifest = json.loads(members["artifact_manifest.json"])
    assert artifact_manifest["artifact_count"] == result.artifact_count
    for entry in artifact_manifest["artifacts"]:
        payload = members[entry["path"]]
        assert entry["sha256"] == hashlib.sha256(payload).hexdigest()

    evidence = json.loads(members["evidence.json"])
    assert evidence["schema_version"] == "audit_evidence_v1"
    assert all(
        "fact" not in item and "query" not in item for item in evidence["evidence"]
    )
    trace = json.loads(members["trace_summary.json"])
    assert "input_summary" not in trace and "output_summary" not in trace
    metrics = json.loads(members["runtime_metrics.json"])
    assert metrics["artifact_count"] == result.artifact_count
    assert metrics["interrupt_count"] >= 2
    assert metrics["resume_count"] == 2
    observed = metrics["observed_execution"]
    assert observed["analysis_mode"] == "deterministic"
    assert observed["analysis_models"] == []
    assert observed["search_sources"] == []
    assert observed["content_fetch_provider"] == "NOT_RECORDED"
    assert (
        metrics["artifact_statuses"]["artifacts/research_result_v1.json"][
            "execution_status"
        ]
        == "INCOMPLETE"
    )
    assert metrics["degradation_used"] is True
    joined = b"\n".join(members.values())
    assert b"model-secret-sentinel" not in joined
    assert b"search-secret-sentinel" not in joined

    monkeypatch.setattr(audit_cli, "PROJECT_ROOT", tmp_path)
    assert (
        audit_cli.main(
            [
                "--db",
                str(settings.checkpoint_db_path),
                "--data-dir",
                str(settings.data_dir),
                "--trace-dir",
                str(settings.trace_dir),
                "export",
                "--thread-id",
                thread_id,
                "--output-dir",
                "cli-exports",
            ]
        )
        == 0
    )
    cli_result = json.loads(capsys.readouterr().out)
    assert cli_result["thread_id"] == thread_id
    assert (tmp_path / cli_result["archive_path"]).is_file()

    with pytest.raises(FileExistsError, match="already exists"):
        export_audit_bundle(
            thread_id=thread_id,
            settings=settings,
            output_dir=tmp_path / "exports",
        )

    run_dir = settings.data_dir / CASE_ID / "runs" / str(completed["state"]["run_id"])
    report = run_dir / "output" / "credit_report.md"
    original_report = report.read_text(encoding="utf-8")
    report.write_text(
        original_report + "\nmodel-secret-sentinel\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="configured credential"):
        export_audit_bundle(
            thread_id=thread_id,
            settings=settings,
            output_dir=tmp_path / "secret-exports",
        )
    assert not list((tmp_path / "secret-exports").glob("*.zip"))

    report.write_text(
        original_report + "\nC:\\Users\\analyst\\notes.txt\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="absolute local path"):
        export_audit_bundle(
            thread_id=thread_id,
            settings=settings,
            output_dir=tmp_path / "local-path-exports",
        )
    assert not list((tmp_path / "local-path-exports").glob("*.zip"))


def test_export_rejects_incomplete_unsafe_and_corrupt_tasks(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    incomplete_thread = "audit-byd-incomplete"
    start_task(thread_id=incomplete_thread, case_id=CASE_ID, settings=settings)

    with pytest.raises(ValueError, match="COMPLETED"):
        export_audit_bundle(
            thread_id=incomplete_thread,
            settings=settings,
            output_dir=tmp_path / "incomplete-exports",
        )
    with pytest.raises(ValueError, match="unsafe"):
        export_audit_bundle(
            thread_id="../escape",
            settings=settings,
            output_dir=tmp_path / "unsafe-exports",
        )

    completed_thread = "audit-byd-corrupt"
    completed = _complete_researched_task(settings, completed_thread)
    run_dir = settings.data_dir / CASE_ID / "runs" / str(completed["state"]["run_id"])
    (run_dir / "artifacts" / "corrupt_v1.json").write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="valid UTF-8 JSON"):
        export_audit_bundle(
            thread_id=completed_thread,
            settings=settings,
            output_dir=tmp_path / "corrupt-exports",
        )
    assert not list((tmp_path / "corrupt-exports").glob("*.zip"))
