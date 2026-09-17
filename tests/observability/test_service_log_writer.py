"""Archive isolation and uncertain disk-write behavior of the production writer."""

import json
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from credra_agent.observability.writer import ROTATION_BYTES, VolumeWriter


def read_records(directory):
    return [
        json.loads(line)
        for path in sorted(directory.glob("service.*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def test_rotates_before_utf8_record_crosses_exact_boundary(tmp_path):
    with patch(
        "credra_agent.observability.writer.utc_now",
        return_value="2026-09-08T00:00:00+00:00",
    ):
        writer = VolumeWriter(tmp_path)
        record = {
            "message": "",
            "startup_id": writer.startup_id,
            "sequence": 1,
            "received_at": "2026-09-08T00:00:00+00:00",
        }
        count, remainder = divmod(ROTATION_BYTES - len(writer.encode(record)), 3)
        writer.append({"message": "中" * count + "a" * remainder})
        assert writer.size == ROTATION_BYTES
        writer.append({"message": "next event"})
        writer.close(complete=True)
    assert [e["sequence"] for e in read_records(writer.directory)] == [1, 2]
    assert (writer.directory / "service.0001.jsonl").stat().st_size == ROTATION_BYTES
    manifest = json.loads((writer.directory / "startup.json").read_text())
    assert manifest["normal_shutdown"] and manifest["last_durable_sequence"] == 2


def test_new_batch_retains_old_archives_and_marks_truncation(tmp_path):
    first = VolumeWriter(tmp_path, max_bytes=1024)
    first.append({"message": "中文" * 2000})
    first.close(complete=True)
    original = (first.directory / "service.0001.jsonl").read_bytes()
    second = VolumeWriter(tmp_path, max_bytes=1024)
    second.append({"message": "new startup"})
    second.close(complete=True)
    assert first.directory != second.directory
    assert (first.directory / "service.0001.jsonl").read_bytes() == original
    assert read_records(first.directory)[0]["truncated_fields"] == ["message"]
    assert len(original) <= 1024


def test_uncertain_write_blocks_further_appends_and_marks_gap(tmp_path, monkeypatch):
    writer = VolumeWriter(tmp_path)
    with monkeypatch.context() as patcher:

        def fail_sync(_):
            raise OSError("simulated disk failure")

        patcher.setattr("credra_agent.observability.writer.os.fsync", fail_sync)
        with pytest.raises(OSError):
            writer.append({"message": "delivery uncertain"})
    with pytest.raises(OSError, match="unavailable"):
        writer.append({"message": "must not reuse sequence"})
    writer.close(complete=True)
    manifest = json.loads((writer.directory / "startup.json").read_text())
    assert not manifest["normal_shutdown"]
    assert manifest["gap"] == "LOG_DELIVERY_UNCERTAIN"
    assert len(read_records(writer.directory)) == 1


def test_windows_manifest_rename_retry_does_not_duplicate_event(tmp_path, monkeypatch):
    writer = VolumeWriter(tmp_path)
    original = Path.replace
    attempts, delays = [], []

    def replace(path, target):
        attempts.append(path)
        if len(attempts) < 3:
            error = PermissionError(13, "secret-sentinel")
            error.winerror = 5
            raise error
        return original(path, target)

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "replace", replace)
        patcher.setattr("credra_agent.observability.writer.sleep", delays.append)
        assert writer.append({"message": "one event"}) == 1
    writer.close(complete=True)
    assert len(attempts) == 3 and delays == [0.01, 0.02]
    assert len(read_records(writer.directory)) == 1
    assert (
        json.loads((writer.directory / "startup.json").read_text())[
            "last_durable_sequence"
        ]
        == 1
    )


def test_persistent_manifest_denial_retains_pause_and_gap(tmp_path, monkeypatch):
    writer = VolumeWriter(tmp_path)
    attempts = []

    def replace(path, target):
        attempts.append(path)
        error = PermissionError(13, "secret-sentinel")
        error.winerror = 5
        raise error

    with monkeypatch.context() as patcher:
        patcher.setattr(Path, "replace", replace)
        patcher.setattr("credra_agent.observability.writer.sleep", lambda seconds: None)
        with pytest.raises(PermissionError):
            writer.append({"message": "partial persistence"})
    assert len(attempts) == 5 and writer.failed
    with pytest.raises(OSError):
        writer.append({"message": "must not duplicate"})
    writer.close(complete=True)
    assert len(read_records(writer.directory)) == 1
    assert (
        json.loads((writer.directory / "startup.json").read_text())["gap"]
        == "LOG_DELIVERY_UNCERTAIN"
    )


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows rename sharing semantics")
def test_actual_windows_manifest_reader_releases_within_bounded_retry(tmp_path):
    writer = VolumeWriter(tmp_path)
    handle = (writer.directory / "startup.json").open("rb")

    def release():
        time.sleep(0.04)
        handle.close()

    reader = threading.Thread(target=release)
    reader.start()
    try:
        assert writer.append({"message": "reader-held metadata"}) == 1
    finally:
        reader.join(timeout=2)
        handle.close()
        writer.close(complete=True)
    assert len(read_records(writer.directory)) == 1
