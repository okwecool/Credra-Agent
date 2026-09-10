"""Small adapters that preserve existing return values and exception semantics."""

import inspect
from contextvars import ContextVar
from functools import wraps
from time import perf_counter
from uuid import uuid4

from .events import log_context
from .runtime import emit

_ATTEMPT_METADATA: ContextVar[dict | None] = ContextVar(
    "credra_attempt_metadata", default=None
)


def transport_metadata(**fields):
    metadata = _ATTEMPT_METADATA.get()
    if metadata is not None:
        metadata.update(
            {
                k: v
                for k, v in fields.items()
                if k in {"http_status", "request_id", "finish_reason"} and v is not None
            }
        )


def model_call(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with log_context(call_id=uuid4().hex):
            return function(*args, **kwargs)

    return wrapped


def model_attempt(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        fields = {
            "attempt": kwargs["attempt"],
            "purpose": kwargs["purpose"],
            "phase": kwargs["phase"],
            "model": self.model_name,
            "provider": "openai_compatible",
        }
        started = perf_counter()
        emit("LLM_ATTEMPT_START", status="STARTED", **fields)
        metadata = {}
        token = _ATTEMPT_METADATA.set(metadata)
        try:
            result = function(self, *args, **kwargs)
        except Exception as exc:
            http_status = getattr(exc, "status_code", None)
            transport_metadata(
                http_status=http_status, request_id=getattr(exc, "request_id", None)
            )
            status = "RATE_LIMITED" if http_status == 429 else "FAILED"
            if isinstance(exc, TimeoutError) or type(exc).__name__ in {
                "APITimeoutError",
                "ReadTimeout",
            }:
                status = "TIMEOUT"
            emit(
                "LLM_ATTEMPT_END",
                status=status,
                exception=exc,
                **metadata,
                duration_ms=round((perf_counter() - started) * 1000),
                **fields,
            )
            raise
        finally:
            _ATTEMPT_METADATA.reset(token)
        usage = result[1]
        emit(
            "LLM_ATTEMPT_END",
            status="SUCCESS",
            validation_stage="transport",
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            duration_ms=round((perf_counter() - started) * 1000),
            **metadata,
            **fields,
        )
        return result

    return wrapped


def validation(stage: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                emit(
                    "LLM_VALIDATION",
                    validation_stage=stage,
                    status="INVALID_CITATION"
                    if stage == "citation"
                    else "INVALID_BUSINESS",
                    exception=exc,
                )
                raise
            emit("LLM_VALIDATION", validation_stage=stage, status="SUCCESS")
            return result

        return wrapped

    return decorate


def artifact_operation(event: str):
    def decorate(function):
        @wraps(function)
        def wrapped(self, reference, *args, **kwargs):
            try:
                result = function(self, reference, *args, **kwargs)
            except Exception as exc:
                emit(
                    "ARTIFACT_ERROR",
                    artifact_ref=reference,
                    status="FAILED",
                    exception=exc,
                )
                raise
            emit(event, artifact_ref=reference, status="SUCCESS")
            if (
                event == "ARTIFACT_WRITE"
                and args
                and getattr(args[0], "execution_status", None) == "DEGRADED"
            ):
                emit(
                    "DEGRADED",
                    artifact_ref=reference,
                    status="DEGRADED",
                    error_code=getattr(args[0], "error_code", None),
                )
            return result

        return wrapped

    return decorate


def tool_call(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with log_context(call_id=uuid4().hex):
            emit("MCP_CONNECT", tool=function.__name__, status="STARTED")
            emit("TOOL_START", tool=function.__name__, status="STARTED")
            start = perf_counter()
            try:
                result = await function(*args, **kwargs)
            except Exception as exc:
                emit(
                    "TOOL_END",
                    tool=function.__name__,
                    status="FAILED",
                    exception=exc,
                    duration_ms=round((perf_counter() - start) * 1000),
                )
                raise
            else:
                emit(
                    "TOOL_END",
                    tool=function.__name__,
                    status="SUCCESS",
                    duration_ms=round((perf_counter() - start) * 1000),
                )
                return result
            finally:
                emit("MCP_DISCONNECT", tool=function.__name__)

    assert inspect.iscoroutinefunction(function)
    return wrapped


def source_call(function):
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        started = perf_counter()
        emit("TOOL_START", tool=function.__name__, status="STARTED")
        try:
            result = function(self, *args, **kwargs)
        except Exception as exc:
            emit(
                "SOURCE_RESULT",
                tool=function.__name__,
                status="FAILED",
                exception=exc,
                duration_ms=round((perf_counter() - started) * 1000),
            )
            raise
        raw_status = getattr(result, "status", "SUCCESS")
        status = (
            raw_status
            if raw_status in {"SUCCESS", "FAILED", "UNAVAILABLE", "NO_RESULT"}
            else "UNKNOWN"
        )
        emit(
            "SOURCE_RESULT",
            tool=function.__name__,
            status=status,
            duration_ms=round((perf_counter() - started) * 1000),
        )
        return result

    return wrapped
