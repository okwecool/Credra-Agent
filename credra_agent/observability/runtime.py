"""Service lifetime, task-local correlation and dispatch-time health guard."""

import atexit
import inspect
import json
import logging
import os
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any

from .collector import (
    ENDPOINT_ENV,
    Collector,
    LogConfig,
    LoggingUnavailable,
    diagnostic_failure,
    send,
)
from .events import event_record, log_context

ACTIVE: ContextVar[Any] = ContextVar("credra_log_service", default=None)
_process_service = None
_process_lock = threading.RLock()
_emitting = threading.local()


class ServiceLog:
    def __init__(self, service: str, config: LogConfig, *, inherited: bool = True):
        self.service = service
        inherited_endpoint = os.environ.get(ENDPOINT_ENV) if inherited else None
        try:
            self.collector = None if inherited_endpoint else Collector(config)
        except OSError as exc:
            diagnostic_failure()
            raise LoggingUnavailable("LOGGING_INITIALIZATION_FAILED") from exc
        self.endpoint = (
            json.loads(inherited_endpoint)
            if inherited_endpoint
            else self.collector.endpoint
        )
        self.healthy = True

    def emit(self, kind: str, **fields) -> bool:
        if not self.healthy:
            return False
        try:
            record = event_record(kind, service=self.service, **fields)
            if self.collector is not None:
                self.collector.publish(record)
            else:
                send(self.endpoint, record)
            return True
        except (LoggingUnavailable, OSError, ValueError, TypeError):
            if self.healthy:
                self.healthy = False
                diagnostic_failure()
            return False

    def close(self):
        self.emit(
            "SERVICE_STOP" if self.collector else "PROCESS_STOP", status="SUCCESS"
        )
        if self.collector:
            self.collector.close()


def current() -> ServiceLog | None:
    return ACTIVE.get() or _process_service


def emit(kind: str, **fields) -> bool:
    service = current()
    return service.emit(kind, **fields) if service else True


def require_logging() -> None:
    service = current()
    if service and (
        not service.healthy or (service.collector and not service.collector.healthy)
    ):
        raise LoggingUnavailable("LOGGING_UNAVAILABLE_BEFORE_NODE")


def child_environment() -> dict[str, str]:
    service = current()
    return {ENDPOINT_ENV: json.dumps(service.endpoint)} if service else {}


def config_from_settings(settings) -> LogConfig:
    return LogConfig(
        directory=settings.service_log_dir,
        max_bytes=settings.service_log_max_bytes,
        queue_capacity=settings.service_log_queue_capacity,
        enqueue_timeout=settings.service_log_enqueue_timeout,
        ack_timeout=settings.service_log_ack_timeout,
        shutdown_timeout=settings.service_log_shutdown_timeout,
    )


class ServiceHandler(logging.Handler):
    def emit(self, record):
        if getattr(_emitting, "active", False):
            return
        _emitting.active = True
        try:
            # Never format the original LogRecord: URLs, SDK payloads, headers
            # and exception strings can be present in msg/args/exc_info.
            emit(
                "SDK_DIAGNOSTIC",
                level=record.levelname
                if record.levelname in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
                else "INFO",
                logger="python_logging",
                request_id=record.name,
                exception=record.exc_info[1] if record.exc_info else None,
            )
        finally:
            _emitting.active = False


@contextmanager
def service_session(service: str, config: LogConfig, *, inherited: bool = True):
    if current() is not None:
        yield current()
        return
    instance = ServiceLog(service, config, inherited=inherited)
    token = ACTIVE.set(instance)
    instance.emit(
        "SERVICE_START" if instance.collector else "PROCESS_START", status="STARTED"
    )
    instance.emit("CONFIG_VALIDATED", status="SUCCESS")
    instance.emit("SERVICE_READY", status="SUCCESS")
    try:
        yield instance
    except BaseException as exc:
        instance.emit(
            "UNCAUGHT_EXCEPTION", level="ERROR", exception=exc, status="FAILED"
        )
        raise
    finally:
        instance.close()
        ACTIVE.reset(token)


def start_process_service(service: str, config: LogConfig) -> ServiceLog:
    """One lifetime for a Chainlit/CLI/MCP process, not one per chat or task."""
    global _process_service
    with _process_lock:
        if _process_service is not None:
            return _process_service
        instance = ServiceLog(service, config)
        _process_service = instance
        handler = ServiceHandler()
        root = logging.getLogger()
        previous_handlers, previous_level = root.handlers[:], root.level
        root.handlers = [handler]
        root.setLevel(logging.INFO)
        old_hook, old_thread_hook = sys.excepthook, threading.excepthook

        def exception_hook(exc_type, exc, tb):
            instance.emit(
                "UNCAUGHT_EXCEPTION", level="ERROR", exception=exc, status="FAILED"
            )
            print(
                "Credra Agent stopped after an unhandled exception; inspect service logs.",
                file=sys.stderr,
            )

        def thread_hook(args):
            exception_hook(args.exc_type, args.exc_value, args.exc_traceback)

        sys.excepthook = exception_hook
        threading.excepthook = thread_hook

        def stop_service():
            global _process_service
            with _process_lock:
                if _process_service is not instance:
                    return
                instance.close()
                _process_service = None
                root.handlers, root.level = previous_handlers, previous_level
                sys.excepthook, threading.excepthook = old_hook, old_thread_hook

        instance.stop_process = stop_service
        atexit.register(stop_service)
        instance.emit(
            "SERVICE_START" if instance.collector else "PROCESS_START", status="STARTED"
        )
        instance.emit("CONFIG_VALIDATED", status="SUCCESS")
        instance.emit("SERVICE_READY", status="SUCCESS")
        return instance


def logged_operation(function):
    """Ensure direct Python runtime callers also have a logging lifetime."""
    signature = inspect.signature(function)

    @wraps(function)
    def wrapped(*args, **kwargs):
        arguments = signature.bind(*args, **kwargs).arguments
        settings = arguments["settings"]
        task_id = arguments.get("thread_id", arguments.get("task_id"))
        with (
            service_session("runtime", config_from_settings(settings)),
            log_context(thread_id=task_id),
        ):
            emit("REQUEST_ACCEPTED", purpose=function.__name__)
            try:
                result = function(*args, **kwargs)
            except LoggingUnavailable:
                if "thread_id" not in arguments:
                    raise  # Non-durable previews have no checkpoint to resume.
                # Only guards before node execution throw this type. The failed
                # LangGraph task persists that boundary and may safely resume.
                from app.runtime.tasks import get_task_status

                return get_task_status(thread_id=task_id, settings=settings)
            except Exception as exc:
                emit("REQUEST_REJECTED", status="FAILED", exception=exc)
                raise
            emit("REQUEST_END", status="SUCCESS", purpose=function.__name__)
            state = (
                result.get("state", {})
                if isinstance(result, dict)
                else getattr(result, "state", {})
            )
            if function.__name__ != "get_task_status" and state.get("status") in {
                "COMPLETED",
                "FAILED",
            }:
                emit(
                    "TASK_END",
                    status="FAILED" if state["status"] == "FAILED" else "SUCCESS",
                )
            return result

    return wrapped


def default_config() -> LogConfig:
    """Read only diagnostic fields, so startup does not need application secrets."""
    return LogConfig(
        directory=Path(os.environ.get("SERVICE_LOG_DIR", "logs")),
        max_bytes=int(os.environ.get("SERVICE_LOG_MAX_BYTES", "10000000")),
        queue_capacity=int(os.environ.get("SERVICE_LOG_QUEUE_CAPACITY", "1024")),
        enqueue_timeout=float(os.environ.get("SERVICE_LOG_ENQUEUE_TIMEOUT", "2")),
        ack_timeout=float(os.environ.get("SERVICE_LOG_ACK_TIMEOUT", "5")),
        shutdown_timeout=float(os.environ.get("SERVICE_LOG_SHUTDOWN_TIMEOUT", "10")),
    )


def entrypoint(service: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if current():
                return function(*args, **kwargs)
            from app.config import Settings

            try:
                settings = Settings()
                instance = start_process_service(
                    service, config_from_settings(settings)
                )
            except ValueError as exc:
                # Configuration errors can contain user input; never print the
                # ValidationError text. Bootstrap diagnostics with safe defaults.
                try:
                    instance = start_process_service(
                        service,
                        LogConfig(Path(os.environ.get("SERVICE_LOG_DIR", "logs"))),
                    )
                    instance.emit("CONFIG_VALIDATED", status="FAILED", exception=exc)
                    instance.stop_process()
                except (LoggingUnavailable, OSError):
                    diagnostic_failure()
                print(json.dumps({"error": "CONFIGURATION_INVALID"}))
                return 2
            except LoggingUnavailable:
                print(json.dumps({"error": "LOGGING_INITIALIZATION_FAILED"}))
                return 2
            try:
                return function(*args, **kwargs)
            except BaseException as exc:
                emit(
                    "UNCAUGHT_EXCEPTION", status="FAILED", level="ERROR", exception=exc
                )
                raise
            finally:
                instance.stop_process()

        return wrapped

    return decorate
