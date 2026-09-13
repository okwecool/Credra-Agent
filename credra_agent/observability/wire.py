"""Reject non-metadata or malformed records at the authenticated IPC boundary."""

import re
from datetime import datetime

from .events import EVENTS, IDENTIFIERS, NUMBERS, STATES, STATUSES, TECHNICAL


def validate_issues(issues):
    from .text import ISSUE_LABELS

    if not isinstance(issues, list) or len(issues) > 20:
        raise ValueError("invalid validation diagnostics")
    for issue in issues:
        if (
            not isinstance(issue, dict)
            or set(issue) - {"path", "kind", "limit", "actual_length"}
            or issue.get("kind") not in ISSUE_LABELS
            or not isinstance(issue.get("path"), list)
            or len(issue["path"]) > 12
        ):
            raise ValueError("invalid validation issue")
        for part in issue["path"]:
            if not (
                type(part) is int
                and 0 <= part <= 10**15
                or isinstance(part, str)
                and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", part)
            ):
                raise ValueError("invalid diagnostic path")
        for key in ("limit", "actual_length"):
            if key in issue and (
                type(issue[key]) is not int or not 0 <= issue[key] <= 10**15
            ):
                raise ValueError("invalid diagnostic size")


def validate_event(record: dict) -> None:
    base = {
        "timestamp_utc",
        "level",
        "event_type",
        "event_id",
        "process_instance_id",
        "pid",
        "os_thread_id",
        "message",
        "exception_type_ref",
        "stack",
        "validation_issues",
    }
    if not isinstance(record, dict) or set(
        record
    ) - base - IDENTIFIERS - NUMBERS - TECHNICAL - {"status", "from_state", "to_state"}:
        raise ValueError("invalid event fields")
    if (
        record.get("event_type") not in EVENTS
        or record.get("schema_version") != "service_log_v1"
    ):
        raise ValueError("invalid event type")
    if record.get("level") not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("invalid level")
    if datetime.fromisoformat(record["timestamp_utc"]).tzinfo is None:
        raise ValueError("timestamp requires timezone")
    if record.get("message") != record["event_type"].lower().replace("_", " "):
        raise ValueError("untrusted message")
    for key in ("event_id", "process_instance_id"):
        if not re.fullmatch(r"[0-9a-f]{32}", record.get(key, "")):
            raise ValueError("invalid event identity")
    for key, value in record.items():
        if (
            key in IDENTIFIERS | {"exception_type_ref"}
            and value is not None
            and (
                not isinstance(value, str)
                or not re.fullmatch(r"ref_[0-9a-f]{64}", value)
            )
        ):
            raise ValueError("untrusted identifier")
        if (
            key in NUMBERS | {"pid", "os_thread_id"}
            and value is not None
            and (type(value) is not int or not 0 <= value <= 10**15)
        ):
            raise ValueError("invalid number")
        if (
            key in TECHNICAL
            and value is not None
            and (
                not isinstance(value, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,79}", value)
            )
        ):
            raise ValueError("invalid label")
        if key == "status" and value not in STATUSES:
            raise ValueError("invalid status")
        if (
            key in {"from_state", "to_state"}
            and value is not None
            and value not in STATES
        ):
            raise ValueError("invalid state")
    for frame in record.get("stack", []):
        if (
            set(frame) != {"file_ref", "line", "function_ref"}
            or type(frame["line"]) is not int
        ):
            raise ValueError("invalid stack")
        if any(
            not re.fullmatch(r"ref_[0-9a-f]{64}", frame[k])
            for k in ("file_ref", "function_ref")
        ):
            raise ValueError("invalid stack reference")
    if "validation_issues" in record:
        validate_issues(record["validation_issues"])
