"""Bounded single-writer service log collector, separate from MCP stdio."""

import hmac
import json
import queue
import secrets
import socket
import socketserver
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from .writer import ROTATION_BYTES, VolumeWriter

MAX_FRAME_BYTES = 65_536
ENDPOINT_ENV = "CREDRA_LOG_ENDPOINT"


class LoggingUnavailable(RuntimeError):
    """Raised only at safe dispatch boundaries; no provider text is included."""


@dataclass(frozen=True)
class LogConfig:
    directory: Path = Path("logs")
    max_bytes: int = ROTATION_BYTES
    queue_capacity: int = 1024
    enqueue_timeout: float = 2.0
    ack_timeout: float = 5.0
    shutdown_timeout: float = 10.0

    def __post_init__(self):
        if (
            self.queue_capacity < 1
            or min(self.enqueue_timeout, self.ack_timeout, self.shutdown_timeout) <= 0
        ):
            raise ValueError("invalid logging capacity or timeout")


@dataclass
class Receipt:
    record: dict
    ready: threading.Event = field(default_factory=threading.Event)
    sequence: int | None = None
    failed: bool = False


def diagnostic_failure() -> None:
    print(
        "CREDRA_LOG_UNAVAILABLE: new actions paused; delivery may be uncertain",
        file=sys.stderr,
    )


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = False

    def __init__(self, *args):
        self.slots = threading.BoundedSemaphore(64)
        super().__init__(*args)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        diagnostic_failure()


class Collector:
    def __init__(self, config: LogConfig):
        self.config = config
        self.writer = VolumeWriter(config.directory, max_bytes=config.max_bytes)
        self.queue: queue.Queue[Receipt] = queue.Queue(config.queue_capacity)
        self.stop = threading.Event()
        self.admission_lock = threading.Lock()
        self.healthy = True
        self.accepting = True
        self.token = secrets.token_hex(32)
        self.seen: dict[str, int] = {}
        collector = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(config.enqueue_timeout + config.ack_timeout)
                try:
                    line = self.rfile.readline(MAX_FRAME_BYTES + 1)
                    if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                        raise ValueError("invalid frame")
                    message = json.loads(line)
                    if not isinstance(message, dict) or not isinstance(
                        message.get("token"), str
                    ):
                        raise TypeError("invalid envelope")
                    if not hmac.compare_digest(message["token"], collector.token):
                        raise ValueError("unauthorized")
                    if message.get("startup_id") != collector.writer.startup_id:
                        raise ValueError("wrong startup")
                    record = message["event"]
                    # Authenticated clients send only event_record output. Reject
                    # arbitrary payload containers, rather than writing raw JSON.
                    from .wire import validate_event

                    validate_event(record)
                    sequence = collector.publish(record)
                    response = {"ok": True, "sequence": sequence}
                except (OSError, ValueError, TypeError, KeyError, LoggingUnavailable):
                    response = {"ok": False, "error": "LOG_DELIVERY_UNCERTAIN"}
                try:
                    self.wfile.write(json.dumps(response).encode() + b"\n")
                except OSError:
                    pass  # Lost ACK is not proof of non-delivery.

        try:
            self.server = _Server(("127.0.0.1", 0), Handler)
        except OSError:
            self.writer.close(complete=False)
            raise
        self.worker = threading.Thread(
            target=self._write_loop, name="credra-log-writer", daemon=True
        )
        self.listener = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.worker.start()
        self.listener.start()

    @property
    def endpoint(self) -> dict:
        return {
            "port": self.server.server_address[1],
            "token": self.token,
            "startup_id": self.writer.startup_id,
            "timeout": self.config.enqueue_timeout + self.config.ack_timeout + 0.5,
        }

    def fail(self) -> None:
        if self.healthy:
            self.healthy = False
            diagnostic_failure()

    def publish(self, record: dict) -> int:
        receipt = Receipt(record)
        deadline = monotonic() + self.config.enqueue_timeout
        if not self.admission_lock.acquire(timeout=self.config.enqueue_timeout):
            self.fail()
            raise LoggingUnavailable("LOGGING_UNAVAILABLE")
        try:
            if not self.accepting or not self.healthy:
                raise LoggingUnavailable("LOGGING_UNAVAILABLE")
            self.queue.put(receipt, timeout=max(0, deadline - monotonic()))
        except queue.Full as exc:
            self.fail()
            raise LoggingUnavailable("LOGGING_UNAVAILABLE") from exc
        finally:
            self.admission_lock.release()
        if not receipt.ready.wait(self.config.ack_timeout) or receipt.failed:
            self.fail()
            raise LoggingUnavailable("LOGGING_UNAVAILABLE")
        assert receipt.sequence is not None
        return receipt.sequence

    def _write_loop(self):
        try:
            while not self.stop.is_set() or not self.queue.empty():
                try:
                    receipt = self.queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    if not self.healthy:
                        raise LoggingUnavailable("LOGGING_UNAVAILABLE")
                    identity = receipt.record["event_id"]
                    sequence = self.seen.get(identity)
                    if sequence is None:
                        sequence = self.writer.append(receipt.record)
                        self.seen[identity] = sequence
                        if len(self.seen) > self.config.queue_capacity * 4:
                            self.seen.pop(next(iter(self.seen)))
                    receipt.sequence = sequence
                except (OSError, ValueError, KeyError, LoggingUnavailable):
                    receipt.failed = True
                    self.fail()
                finally:
                    receipt.ready.set()
                    self.queue.task_done()
        finally:
            try:
                self.writer.close(complete=self.healthy)
            except OSError:
                self.fail()

    def close(self):
        if not self.accepting:
            return
        deadline = monotonic() + self.config.shutdown_timeout
        acquired = self.admission_lock.acquire(timeout=self.config.shutdown_timeout)
        self.accepting = False
        if acquired:
            self.admission_lock.release()
        else:
            self.fail()
        self.server.shutdown()
        self.server.server_close()
        self.stop.set()
        self.worker.join(max(0, deadline - monotonic()))
        if self.worker.is_alive():
            # Never close a file while the owner may still be writing it. The
            # on-disk manifest remains non-normal until the worker can finish.
            self.fail()


def send(endpoint: dict, record: dict) -> int:
    message = {
        "token": endpoint["token"],
        "startup_id": endpoint["startup_id"],
        "event": record,
    }
    payload = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8"
    )
    if len(payload) > MAX_FRAME_BYTES:
        raise LoggingUnavailable("LOGGING_UNAVAILABLE")
    try:
        with socket.create_connection(
            ("127.0.0.1", int(endpoint["port"])), timeout=float(endpoint["timeout"])
        ) as connection:
            connection.sendall(payload)
            with connection.makefile("rb") as response:
                result = json.loads(response.readline(2048))
        if not result.get("ok"):
            raise LoggingUnavailable("LOGGING_UNAVAILABLE")
        return int(result["sequence"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LoggingUnavailable("LOGGING_UNAVAILABLE") from exc
