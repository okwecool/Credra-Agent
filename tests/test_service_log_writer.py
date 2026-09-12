"""Archive isolation and uncertain disk-write behavior of the production writer."""

import json
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
