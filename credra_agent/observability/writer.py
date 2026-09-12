"""Single-owner, append-only UTF-8 volumes and an atomic startup manifest."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from .events import utc_now

ROTATION_BYTES = 10_000_000  # User-approved decimal MB, not MiB.


class VolumeWriter:
    def __init__(self, root: Path, *, max_bytes: int = ROTATION_BYTES) -> None:
        if max_bytes < 1024:
            raise ValueError("log volume capacity must be at least 1024 bytes")
        self.startup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ-") + uuid4().hex
        self.directory = root / self.startup_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.max_bytes = max_bytes
        self.sequence = 0
        self.segment = 1
        self.size = 0
        self.failed = False
        self.closed = False
        self.started_at = utc_now()
        self.file = (self.directory / "service.0001.jsonl").open("xb")
        try:
            self.manifest(normal_shutdown=False)
        except OSError:
            self.file.close()
            raise

    def manifest(self, *, normal_shutdown: bool, gap: str | None = None) -> None:
        payload = {
            "schema_version": "startup_manifest_v1",
            "startup_id": self.startup_id,
            "started_at": self.started_at,
            "updated_at": utc_now(),
            "max_bytes": self.max_bytes,
            "segments": self.segment,
            "last_durable_sequence": self.sequence,
            "normal_shutdown": normal_shutdown,
            "gap": gap,
        }
        target = self.directory / "startup.json"
        temporary = self.directory / "startup.json.tmp"
        with temporary.open("w", encoding="utf-8", newline="\n") as file:
            json.dump(payload, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(target)

    @staticmethod
    def encode(record: dict) -> bytes:
        return (
            json.dumps(
                record, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            )
            + "\n"
        ).encode("utf-8")

    def append(self, event: dict) -> int:
        if self.closed or self.failed:
            raise OSError("log writer is unavailable")
        record = {
            **event,
            "startup_id": self.startup_id,
            "sequence": self.sequence + 1,
            "received_at": utc_now(),
        }
        payload = self.encode(record)
        # Production ingress is metadata-only. This also bounds internal callers
        # and supports a deliberately small threshold in boundary tests.
        while len(payload) > self.max_bytes and len(record.get("message", "")) > 1:
            record["message"] = record["message"][: len(record["message"]) // 2]
            record["truncated_fields"] = ["message"]
            payload = self.encode(record)
        if len(payload) > self.max_bytes:
            raise ValueError("log event metadata exceeds volume capacity")
        try:
            if self.size + len(payload) > self.max_bytes:
                next_segment = self.segment + 1
                next_file = (self.directory / f"service.{next_segment:04d}.jsonl").open(
                    "xb"
                )
                self.file.close()
                self.file = next_file
                self.segment = next_segment
                self.size = 0
            self.file.write(payload)
            self.file.flush()
            os.fsync(self.file.fileno())
            self.size += len(payload)
            self.sequence += 1
            self.manifest(normal_shutdown=False)
        except OSError:
            # Some bytes may already exist. Never reuse a sequence or claim
            # non-delivery; a new startup is required after storage recovery.
            self.failed = True
            raise
        return self.sequence

    def close(self, *, complete: bool) -> None:
        if self.closed:
            return
        try:
            self.file.flush()
            os.fsync(self.file.fileno())
        except OSError:
            self.failed = True
        finally:
            self.file.close()
            self.closed = True
        self.manifest(
            normal_shutdown=complete and not self.failed,
            gap=None if complete and not self.failed else "LOG_DELIVERY_UNCERTAIN",
        )
