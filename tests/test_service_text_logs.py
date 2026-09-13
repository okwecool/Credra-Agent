"""Human log rotation, confidentiality, actual gateway failure and dual ACKs."""

import json
import re

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.llm.gateway import StructuredModelError
from app.models.analysis import ReportDraft
from credra_agent.observability.collector import (
    Collector,
    LogConfig,
    LoggingUnavailable,
)
from credra_agent.observability.events import event_record
from credra_agent.observability.export import export_text_logs
from credra_agent.observability.runtime import service_session
from credra_agent.observability.validation import schema_issues
from credra_agent.observability.wire import validate_event
from credra_agent.observability.writer import VolumeWriter
from tests.test_service_log_integration import model, read_events


def text_logs(directory):
    return "".join(
        p.read_text(encoding="utf-8") for p in sorted(directory.glob("service.*.log"))
    )


def text_sequences(directory):
    return [int(n) for n in re.findall(r" \| #(\d+) \|", text_logs(directory))]


def test_new_startups_write_both_formats_and_rotate_independently(tmp_path):
    writer = VolumeWriter(tmp_path, max_bytes=2048)
    for attempt in range(1, 25):
        writer.append(
            event_record(
                "LLM_ATTEMPT_END",
                service="gateway",
                node="report",
                purpose="report_draft",
                attempt=attempt,
                status="SUCCESS",
                duration_ms=3000,
                input_tokens=None,
                output_tokens=0,
            )
        )
    writer.close(complete=True)
    assert text_sequences(writer.directory) == list(range(1, 25))
    assert [e["sequence"] for e in read_events(writer.directory)] == list(range(1, 25))
    assert len(list(writer.directory.glob("*.log"))) > 1
    assert all(p.stat().st_size <= 2048 for p in writer.directory.glob("service.*"))
    assert "输入Token=未知" in text_logs(writer.directory)
    assert "输出Token=0" in text_logs(writer.directory)
    assert "传输返回成功；输出是否可用请查看后续校验" in text_logs(writer.directory)
    archive = {p.name: p.read_bytes() for p in writer.directory.iterdir()}
    second = VolumeWriter(tmp_path)
    second.close(complete=True)
    assert all(
        (writer.directory / name).read_bytes() == data for name, data in archive.items()
    )
    manifest = json.loads((writer.directory / "startup.json").read_text())
    assert manifest["formats"] == ["jsonl", "log"] and manifest["normal_shutdown"]
    assert manifest["text_segments"] == len(list(writer.directory.glob("*.log")))


def test_text_write_failure_rejects_ack_and_marks_gap(tmp_path):
    collector = Collector(LogConfig(tmp_path))
    original = collector.writer.text_file

    class FailedFile:
        def write(self, _):
            raise OSError("simulated text disk failure")

        def __getattr__(self, name):
            return getattr(original, name)

    collector.writer.text_file = FailedFile()
    try:
        with pytest.raises(LoggingUnavailable):
            collector.publish(event_record("NODE_START", service="test"))
        with pytest.raises(LoggingUnavailable):
            collector.publish(event_record("NODE_END", service="test"))
        assert not collector.healthy and collector.writer.sequence == 0
    finally:
        collector.writer.text_file = original
        collector.close()
    manifest = json.loads((collector.writer.directory / "startup.json").read_text())
    assert not manifest["normal_shutdown"]
    assert manifest["gap"] == "LOG_DELIVERY_UNCERTAIN"
    assert len(read_events(collector.writer.directory)) == 1  # partial, unacknowledged


def test_schema_diagnostics_identify_fields_without_leaking_values(tmp_path):
    sentinel = "secret-sentinel-DO-NOT-STORE"
    draft = {
        "executive_summary": "文" * 2001,
        "sections": [
            {"section": sentinel, "text": "说明", "reference_ids": ["risk:summary"]}
        ],
        sentinel: sentinel,
    }
    with pytest.raises(ValidationError) as error:
        ReportDraft.model_validate(draft)
    details = schema_issues(error.value, ReportDraft)
    event = event_record(
        "LLM_VALIDATION", service="test", status="INVALID_SCHEMA", **details
    )
    validate_event(event)
    writer = VolumeWriter(tmp_path)
    writer.append(event)
    writer.close(complete=True)
    text = text_logs(writer.directory)
    assert "executive_summary" in text and "约束长度=2000" in text
    assert "executive_summary_reference_ids" in text and "缺少必填字段" in text
    assert "sections.0.section" in text and "字段取值不在允许范围" in text
    assert sentinel not in text + json.dumps(read_events(writer.directory))
    assert details["validation_issue_count"] == 4


def test_custom_validation_message_and_context_never_leak(tmp_path):
    class PrivateOutput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: str = Field(min_length=1)

        @model_validator(mode="after")
        def reject(self):
            raise ValueError("secret-sentinel-DO-NOT-STORE")

    with pytest.raises(ValidationError) as error:
        PrivateOutput.model_validate({"value": "secret-sentinel-DO-NOT-STORE"})
    event = event_record(
        "LLM_VALIDATION", service="test", **schema_issues(error.value, PrivateOutput)
    )
    writer = VolumeWriter(tmp_path)
    writer.append(event)
    writer.close(complete=True)
    assert "自定义约束不满足" in text_logs(writer.directory)
    assert "secret-sentinel" not in text_logs(writer.directory)


def test_actual_report_gateway_retries_log_schema_details(tmp_path):
    outputs = ['{"sections":[]}', '{"sections":[]}']
    with service_session("test", LogConfig(tmp_path)) as service:
        with pytest.raises(StructuredModelError):
            model(outputs).generate(
                output_schema=ReportDraft,
                purpose="report_draft",
                prompt_version="offline",
                system_prompt="not logged",
                payload={"private": "secret-sentinel"},
            )
        directory = service.collector.writer.directory
    events = read_events(directory)
    failed = [e for e in events if e.get("status") == "INVALID_SCHEMA"]
    assert len(failed) == 2 and all(e["validation_issues"] for e in failed)
    text = text_logs(directory)
    assert "缺少必填字段" in text and "调用重试" in text
    assert "校验阶段=schema" in text
    assert "secret-sentinel" not in text
    assert text_sequences(directory) == [e["sequence"] for e in events]


def test_actual_json_error_has_position_and_safe_text(tmp_path):
    with service_session("test", LogConfig(tmp_path)) as service:
        with pytest.raises(StructuredModelError):
            model(['{"secret-sentinel":']).generate(
                output_schema=ReportDraft,
                purpose="report_draft",
                prompt_version="offline",
                system_prompt="not logged",
                payload={},
            )
        directory = service.collector.writer.directory
    text = text_logs(directory)
    assert "JSON解析位置=" in text and "行=1" in text
    assert "secret-sentinel" not in text


def test_existing_archive_export_keeps_originals_and_refuses_overwrite(tmp_path):
    source = VolumeWriter(tmp_path / "archives")
    for _ in range(10):
        source.append(event_record("NODE_START", service="test", node="report"))
    source.close(complete=True)
    originals = {p.name: p.read_bytes() for p in source.directory.iterdir()}
    target = tmp_path / "exports" / source.startup_id
    manifest = export_text_logs(source.directory, target, max_bytes=1024)
    assert manifest["complete"] and manifest["events"] == 10
    assert text_sequences(target) == list(range(1, 11))
    assert all(p.stat().st_size <= 1024 for p in target.glob("*.log"))
    assert all(
        (source.directory / name).read_bytes() == data
        for name, data in originals.items()
    )
    with pytest.raises(FileExistsError):
        export_text_logs(source.directory, target)


def test_export_rejects_untrusted_old_payload_and_marks_incomplete(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "service.0001.jsonl").write_text(
        json.dumps(
            {
                **event_record("NODE_START", service="test"),
                "prompt": "secret-sentinel",
                "sequence": 1,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    target = tmp_path / "export"
    with pytest.raises(ValueError):
        export_text_logs(source, target)
    assert "secret-sentinel" not in text_logs(target)
    assert not json.loads((target / "export.json").read_text(encoding="utf-8"))[
        "complete"
    ]
