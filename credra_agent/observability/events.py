"""Metadata-only service events with bounded, explicit serialization rules."""

import hashlib
import os
import re
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

PROCESS_INSTANCE_ID = uuid4().hex
CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar(
    "credra_log_context", default=None
)
CONTEXT_FIELDS = {"thread_id", "run_id", "case_id", "action_id", "call_id", "node"}
EVENTS = frozenset(
    {
        "SERVICE_START",
        "CONFIG_VALIDATED",
        "SERVICE_READY",
        "SERVICE_STOP",
        "PROCESS_START",
        "PROCESS_STOP",
        "UNCAUGHT_EXCEPTION",
        "SDK_DIAGNOSTIC",
        "REQUEST_ACCEPTED",
        "REQUEST_END",
        "REQUEST_REJECTED",
        "TASK_START",
        "TASK_STATE",
        "TASK_END",
        "STATUS_QUERY",
        "RESUME",
        "INTERRUPT",
        "NODE_START",
        "NODE_END",
        "NODE_SKIP",
        "ROUTE",
        "PLAN_CHANGED",
        "ACTION_STATE",
        "STOP",
        "RETRY",
        "DEGRADED",
        "LLM_ATTEMPT_START",
        "LLM_FIRST_TOKEN",
        "LLM_ATTEMPT_END",
        "LLM_VALIDATION",
        "MCP_CONNECT",
        "MCP_DISCONNECT",
        "TOOL_START",
        "TOOL_END",
        "SOURCE_RESULT",
        "CHECKPOINT_OPEN",
        "CHECKPOINT_READ",
        "CHECKPOINT_SAVE",
        "CHECKPOINT_CLOSE",
        "CHECKPOINT_ERROR",
        "ARTIFACT_READ",
        "ARTIFACT_WRITE",
        "ARTIFACT_ERROR",
        "REPORT_WRITE",
        "AUDIT_EXPORT",
        "TRACE_MIRROR",
        "LOG_RECOVERY",
        "LOG_GAP",
    }
)
STATUSES = frozenset(
    {
        "STARTED",
        "SUCCESS",
        "FAILED",
        "RETRY",
        "DEGRADED",
        "SKIPPED",
        "PAUSED",
        "UNKNOWN",
        "NO_RESULT",
        "UNAVAILABLE",
        "MISSING_DATA",
        "INVALID_JSON",
        "INVALID_SCHEMA",
        "INVALID_CITATION",
        "INVALID_BUSINESS",
        "TIMEOUT",
        "RATE_LIMITED",
        "INTERRUPTED",
        "ACCEPTED",
        "REJECTED",
        "REUSED",
    }
)
STATES = frozenset(
    {
        "CREATED",
        "RUNNING",
        "WAITING_APPROVAL",
        "COMPLETED",
        "FAILED",
        "WAITING_CLARIFICATION",
        "PAUSED_LOGGING",
        "NOT_STARTED",
    }
)
NUMBERS = {
    "attempt",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "http_status",
    "result_count",
    "trace_line",
    "plan_version",
    "task_spec_version",
}
TECHNICAL = {
    "node",
    "target_node",
    "service",
    "logger",
    "purpose",
    "phase",
    "tool",
    "error_code",
    "validation_stage",
    "finish_reason",
    "schema_version",
}
IDENTIFIERS = {
    "thread_id",
    "run_id",
    "case_id",
    "action_id",
    "call_id",
    "request_id",
    "artifact_ref",
    "trace_event_ref",
    "model",
    "provider",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def reference_id(value: object) -> str:
    """Stable correlation without persisting arbitrary user/provider identifiers."""
    return "ref_" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()


@contextmanager
def log_context(**fields: Any):
    token = CONTEXT.set(
        {
            **(CONTEXT.get() or {}),
            **{k: v for k, v in fields.items() if k in CONTEXT_FIELDS},
        }
    )
    try:
        yield
    finally:
        CONTEXT.reset(token)


def safe_stack(exc: BaseException) -> list[dict]:
    """Only locations; no exception text, source lines or local variables."""
    return [
        {
            "file_ref": reference_id(frame.filename),
            "line": frame.lineno,
            "function_ref": reference_id(frame.name),
        }
        for frame in traceback.extract_tb(exc.__traceback__)[-12:]
    ]


def event_record(
    event_type: str,
    *,
    service: str,
    level: str = "INFO",
    exception: BaseException | None = None,
    **fields: Any,
) -> dict:
    if event_type not in EVENTS or level not in {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }:
        raise ValueError("invalid diagnostic event")
    record = {
        "schema_version": "service_log_v1",
        "timestamp_utc": utc_now(),
        "level": level,
        "event_type": event_type,
        "event_id": uuid4().hex,
        "process_instance_id": PROCESS_INSTANCE_ID,
        "pid": os.getpid(),
        "os_thread_id": threading.get_ident(),
        "message": event_type.lower().replace("_", " "),
    }
    # Explicit unknowns distinguish absent provider usage from zero usage.
    if event_type.startswith("LLM_"):
        record.update(
            input_tokens=None,
            output_tokens=None,
            http_status=None,
            request_id=None,
            finish_reason=None,
        )
    values = {**(CONTEXT.get() or {}), **fields, "service": service}
    for name, value in values.items():
        if name in IDENTIFIERS:
            record[name] = reference_id(value) if value is not None else None
        elif name in NUMBERS:
            if value is None or (type(value) is int and 0 <= value <= 10**15):
                record[name] = value
        elif name == "finish_reason":
            record[name] = (
                value
                if isinstance(value, str)
                and value
                in {"stop", "length", "content_filter", "tool_calls", "function_call"}
                else "UNKNOWN"
            )
        elif name in TECHNICAL and isinstance(value, str):
            # Technical labels come from code, not model/provider text. Labels
            # violating the grammar are recorded as opaque references as well.
            record[name] = (
                value
                if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value)
                else reference_id(value)
            )
        elif (
            name == "status"
            and isinstance(value, str)
            and value in STATUSES
            or name in {"from_state", "to_state"}
            and (value is None or (isinstance(value, str) and value in STATES))
        ):
            record[name] = value
    if exception is not None:
        record["exception_type_ref"] = reference_id(type(exception).__name__)
        record["stack"] = safe_stack(exception)
    return record
