"""P04 real IPC/MCP, durability boundaries, rotation and metadata-only logging."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from app.runtime.tracing import TraceWriter
from spikes.v2_readiness.logging_spike import (
    MAX_BYTES,
    Collector,
    SegmentWriter,
    send_event,
)


def records(directory: Path) -> list[dict]:
    return [
        json.loads(line)
        for path in sorted(directory.glob("service.*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


@pytest.mark.asyncio
async def test_two_real_mcp_children_share_batch_and_preserve_stdio(tmp_path):
    async def child(environment, task):
        transport = StdioTransport(
            command=sys.executable,
            args=["-m", "spikes.v2_readiness.mcp_server"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=environment,
        )
        async with Client(transport) as client:
            assert "log_probe" in {tool.name for tool in await client.list_tools()}
            result = await client.call_tool(
                "log_probe", {"thread_id": task, "count": 8}
            )
            assert not result.is_error
            return json.loads(result.content[0].text)

    trace = TraceWriter(tmp_path / "traces")
    trace.instant(task_id="parent-task", node="document", event_type="NODE_START")
    original = (tmp_path / "traces/parent-task.jsonl").read_bytes()
    with Collector(tmp_path / "logs", max_bytes=2048) as collector:
        environment = collector.environment
        send_event(environment, {"event_type": "SERVICE_START", "pid": os.getpid()})
        send_event(
            environment,
            {
                "event_type": "TRACE_MIRROR",
                "thread_id": "parent-task",
                "trace_event_ref": "parent-task.1",
            },
        )
        children = await asyncio.gather(
            child(environment, "task-a"), child(environment, "task-b")
        )
        restarted = await child(environment, "task-a")
        assert restarted["process_instance_id"] not in {
            item["process_instance_id"] for item in children
        }
        send_event(
            environment,
            {
                "event_type": "TASK_STATE",
                "thread_id": "parent-task",
                "from_state": "RUNNING",
                "to_state": "WAITING_APPROVAL",
            },
        )
        send_event(environment, {"event_type": "SERVICE_STOP"})
        directory = collector.writer.directory
        assert collector.failures == 0
    events = records(directory)
    assert len(events) == 28
    assert [event["sequence"] for event in events] == list(range(1, 29))
    assert {event["startup_id"] for event in events} == {directory.name}
    assert len({item["pid"] for item in children} | {os.getpid()}) == 3
    assert len({item["process_instance_id"] for item in children}) == 2
    assert sum(event.get("status") == "INVALID_SCHEMA" for event in events) == 12
    volumes = list(directory.glob("service.*.jsonl"))
    assert len(volumes) > 1 and all(path.stat().st_size <= 2048 for path in volumes)
    assert "sentinel" not in json.dumps(events)
    assert (tmp_path / "traces/parent-task.jsonl").read_bytes() == original
    assert json.loads((directory / "startup.json").read_text())["normal_shutdown"]


def test_exact_ten_mb_boundary_utf8_and_restart(tmp_path):
    writer = SegmentWriter(tmp_path)
    # A complete UTF-8 JSON record exactly at the production threshold.
    event = {"event_id": "boundary", "message": ""}
    first = writer.append(event)
    # Calculate deterministic overhead using a fresh writer with fixed timestamp.
    from unittest.mock import patch

    writer.close()
    with patch(
        "spikes.v2_readiness.logging_spike.utc_now", return_value=first["received_at"]
    ):
        second = SegmentWriter(tmp_path)
        probe = {
            **event,
            "startup_id": second.startup_id,
            "sequence": 1,
            "received_at": first["received_at"],
        }
        overhead = len(
            (
                json.dumps(probe, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode()
        )
        count, rest = divmod(MAX_BYTES - overhead, 3)
        second.append({**event, "message": "中" * count + "a" * rest})
        assert second.size == MAX_BYTES
        second.append({"event_id": "next", "message": "next"})
        assert second.segment == 2
        second.close()
    assert writer.directory != second.directory
    assert writer.directory.exists()
    assert [event["event_id"] for event in records(second.directory)] == [
        "boundary",
        "next",
    ]
    bounded = SegmentWriter(tmp_path, max_bytes=1024)
    result = bounded.append({"message": "中文" * 2000})
    bounded.close()
    assert result["truncated_fields"] == ["message"]
    assert bounded.size <= 1024


def test_invalid_ingress_duplicate_ack_and_disk_failure(tmp_path, monkeypatch, capsys):
    with Collector(tmp_path) as collector:
        env = collector.environment
        event = {
            "event_type": "LLM_RESULT",
            "event_id": "stable-id",
            "status": "FAILED",
            "message": "secret-sentinel",
            "api_key": "secret-sentinel",
        }
        assert send_event(env, event) == send_event(env, event)
        wrong = {**env, "CREDRA_SPIKE_LOG_TOKEN": "wrong"}
        with pytest.raises(RuntimeError, match="LOG_DELIVERY_FAILED"):
            send_event(wrong, event)
        with socket.create_connection(
            ("127.0.0.1", int(env["CREDRA_SPIKE_LOG_PORT"]))
        ) as client:
            client.sendall(b"[]\n")
            assert json.loads(client.makefile("rb").readline())["ok"] is False
        with monkeypatch.context() as patcher:

            def disk_full(*args):
                raise OSError("secret-sentinel simulated disk full")

            patcher.setattr("spikes.v2_readiness.logging_spike.os.fsync", disk_full)
            with pytest.raises(RuntimeError, match="LOG_DELIVERY_FAILED"):
                send_event(env, {"event_type": "NODE_END", "event_id": "uncertain"})
        assert collector.failures == 3
        directory = collector.writer.directory
    # The failed fsync may have written bytes. An ACK failure is not non-delivery.
    assert "secret-sentinel" not in directory.joinpath("service.0001.jsonl").read_text()
    assert "secret-sentinel" not in capsys.readouterr().err
    assert len([r for r in records(directory) if r["event_id"] == "stable-id"]) == 1
    with pytest.raises(OSError):
        send_event(env, event)


def test_abnormal_process_exit_and_concurrent_startups(tmp_path):
    code = (
        "import os,sys; from pathlib import Path; "
        "from spikes.v2_readiness.logging_spike import SegmentWriter; "
        "w=SegmentWriter(Path(sys.argv[1])); "
        "w.append({'event_type':'NODE_START','thread_id':'recoverable-task'}); "
        "os._exit(7)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[1],
        timeout=20,
        check=False,
        capture_output=True,
    )
    assert result.returncode == 7
    abandoned = next(tmp_path.iterdir())
    assert not json.loads((abandoned / "startup.json").read_text())["normal_shutdown"]
    assert records(abandoned)[0]["thread_id"] == "recoverable-task"

    def start_new(_):
        writer = SegmentWriter(tmp_path)
        writer.append({"event_type": "NODE_START", "thread_id": "recoverable-task"})
        writer.close()
        return writer.directory

    with ThreadPoolExecutor(max_workers=2) as pool:
        directories = list(pool.map(start_new, range(2)))
    assert len(set(directories) | {abandoned}) == 3
    assert all(records(p)[0]["thread_id"] == "recoverable-task" for p in directories)
