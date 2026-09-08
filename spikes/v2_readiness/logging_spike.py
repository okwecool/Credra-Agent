"""P04-only authenticated JSON logging collector with one durable file writer.

No production imports or integration. The ACK means a record reached fsync,
not that every possible process failure is lossless or exactly-once.
"""

import hmac
import json
import os
import re
import secrets
import socket
import socketserver
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from uuid import uuid4

MAX_BYTES = 10_000_000
MAX_FRAME = 262_144
MESSAGE = {
    "SERVICE_START": "service started",
    "SERVICE_STOP": "service stopped",
    "PROCESS_START": "process started",
    "NODE_START": "node entered",
    "NODE_END": "node finished",
    "TASK_STATE": "task state changed",
    "LLM_RESULT": "model attempt ended",
    "TRACE_MIRROR": "legacy trace reference recorded",
}
STATES = {"CREATED", "RUNNING", "WAITING_APPROVAL", "COMPLETED", "FAILED"}
STATUSES = {"SUCCESS", "FAILED", "RETRY", "DEGRADED", "INVALID_JSON", "INVALID_SCHEMA"}
CONTEXT_KEYS = (
    "thread_id",
    "run_id",
    "action_id",
    "call_id",
    "trace_event_ref",
    "node",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def safe_record(raw: dict) -> dict:
    """Allowlisted diagnostic fields; do not forward arbitrary model/provider text."""
    kind = raw.get("event_type")
    if kind not in MESSAGE:
        raise ValueError("unknown event type")
    event_id = str(raw.get("event_id", uuid4().hex))
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", event_id):
        raise ValueError("invalid event id")
    result = {"event_type": kind, "event_id": event_id, "message": MESSAGE[kind]}
    for name in (*CONTEXT_KEYS, "process_instance_id"):
        if name in raw:
            value = raw[name]
            if not isinstance(value, str) or not re.fullmatch(
                r"[a-zA-Z0-9_.-]{1,100}", value
            ):
                raise ValueError("invalid context")
            result[name] = value
    for name in ("pid", "attempt", "input_tokens", "output_tokens", "duration_ms"):
        if name in raw:
            value = raw[name]
            if type(value) is not int or value < 0:
                raise ValueError("invalid numeric metadata")
            result[name] = value
    for name, allowed in (
        ("status", STATUSES),
        ("from_state", STATES),
        ("to_state", STATES),
    ):
        if name in raw:
            if raw[name] not in allowed:
                raise ValueError("invalid status")
            result[name] = raw[name]
    return result


class SegmentWriter:
    """Only the collector invokes this writer, protected by its single lock."""

    def __init__(self, root: Path, max_bytes: int = MAX_BYTES) -> None:
        if max_bytes < 1024:
            raise ValueError("threshold must fit a bounded event")
        self.startup_id = (
            datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ-") + uuid4().hex[:12]
        )
        self.directory = root / self.startup_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.max_bytes = max_bytes
        self.sequence = 0
        self.segment = 1
        self.size = 0
        self.file = (self.directory / "service.0001.jsonl").open("xb")
        self.closed = False
        self.failed = False
        self._manifest(False)

    def _manifest(self, closed: bool) -> None:
        path = self.directory / "startup.json"
        payload = {
            "schema_version": "logging_spike_v1",
            "startup_id": self.startup_id,
            "max_bytes": self.max_bytes,
            "normal_shutdown": closed,
            "last_sequence": self.sequence,
            "segments": self.segment,
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(path)

    def append(self, event: dict) -> dict:
        if self.failed:
            raise OSError("writer requires recovery after uncertain persistence")
        try:
            return self._append(event)
        except OSError:
            self.failed = True
            raise

    def _append(self, event: dict) -> dict:
        if self.closed:
            raise ValueError("writer is closed")
        record = {
            **event,
            "startup_id": self.startup_id,
            "sequence": self.sequence + 1,
            "received_at": utc_now(),
        }
        payload = (
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        # Internal writer contract also tests UTF-8 truncation; public ingress
        # already replaces free-form messages with a fixed safe event template.
        while len(payload) > self.max_bytes and len(record.get("message", "")) > 1:
            record["message"] = record["message"][: len(record["message"]) // 2]
            record["truncated_fields"] = ["message"]
            payload = (
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
        if len(payload) > self.max_bytes:
            raise ValueError("event metadata exceeds segment capacity")
        if self.size + len(payload) > self.max_bytes:
            self.file.close()
            self.segment += 1
            self.file = (self.directory / f"service.{self.segment:04d}.jsonl").open(
                "xb"
            )
            self.size = 0
        self.file.write(payload)
        self.file.flush()
        os.fsync(self.file.fileno())
        self.size += len(payload)
        self.sequence += 1
        self._manifest(False)
        return record

    def close(self) -> None:
        if not self.closed:
            self.file.flush()
            os.fsync(self.file.fileno())
            self.file.close()
            self.closed = True
            self._manifest(not self.failed)


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = False
    block_on_close = True


class Collector:
    def __init__(self, root: Path, max_bytes: int = MAX_BYTES) -> None:
        self.writer = SegmentWriter(root, max_bytes)
        self.token = secrets.token_hex(24)
        self.lock = threading.Lock()
        self.seen: dict[str, int] = {}
        self.failures = 0
        collector = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                self.connection.settimeout(2)
                try:
                    frame = self.rfile.readline(MAX_FRAME + 1)
                    if len(frame) > MAX_FRAME or not frame.endswith(b"\n"):
                        raise ValueError("invalid frame")
                    message = json.loads(frame)
                    token = message.get("token", "")
                    if not isinstance(token, str) or not hmac.compare_digest(
                        token, collector.token
                    ):
                        raise ValueError("unauthorized")
                    record = safe_record(message["event"])
                    with collector.lock:
                        identity = record["event_id"]
                        sequence = collector.seen.get(identity)
                        if sequence is None:
                            written = collector.writer.append(record)
                            sequence = written["sequence"]
                            collector.seen[identity] = sequence
                    response = {"ok": True, "sequence": sequence}
                except (OSError, ValueError, KeyError, TypeError, AttributeError):
                    collector.failures += 1
                    print(
                        "LOG_DELIVERY_FAILED: spike collector rejected or could not persist event",
                        file=sys.stderr,
                    )
                    response = {"ok": False, "error": "LOG_DELIVERY_FAILED"}
                try:
                    self.wfile.write(json.dumps(response).encode() + b"\n")
                except OSError:
                    # A lost ACK is an uncertain delivery, not a safe retry signal.
                    pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def environment(self) -> dict[str, str]:
        return {
            "CREDRA_SPIKE_LOG_PORT": str(self.server.server_address[1]),
            "CREDRA_SPIKE_LOG_TOKEN": self.token,
            "CREDRA_SPIKE_STARTUP_ID": self.writer.startup_id,
        }

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        with self.lock:
            self.writer.close()


def send_event(environment: dict[str, str], event: dict) -> dict:
    """Bounded synchronous JSON channel, separate from MCP stdio."""
    message = {"token": environment["CREDRA_SPIKE_LOG_TOKEN"], "event": event}
    payload = json.dumps(message).encode() + b"\n"
    if len(payload) > MAX_FRAME:
        raise ValueError("input frame too large")
    with socket.create_connection(
        ("127.0.0.1", int(environment["CREDRA_SPIKE_LOG_PORT"])), timeout=2
    ) as connection:
        connection.sendall(payload)
        with connection.makefile("rb") as response:
            frame = response.readline(2048)
    result = json.loads(frame)
    if not result.get("ok"):
        raise RuntimeError("LOG_DELIVERY_FAILED")
    return result
