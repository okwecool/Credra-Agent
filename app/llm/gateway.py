"""Provider-neutral structured model calls over an OpenAI-compatible API."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.models.analysis import AnalysisErrorCode
from credra_agent.observability.events import CONTEXT
from credra_agent.observability.instrumentation import (
    model_attempt,
    model_call,
    transport_metadata,
)
from credra_agent.observability.runtime import emit

OutputT = TypeVar("OutputT", bound=BaseModel)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PROCESS_PROMPT_VERSION = "m4e-public-process-v1"
_PROCESS_SYSTEM_PROMPT = """你是受约束的分析过程助手。请在内部完成分析，但不要在最终可见文本中输出逐步思维链。分析结束后，只输出一段简短的公开过程摘要：说明核对了哪些输入类别或引用 ID、发现了哪些需要关注的方向以及有哪些限制。不得新增输入中不存在的事实、数字、URL、证据 ID 或授信决定。不要输出 JSON、Markdown 标题或代码块。"""


@dataclass(frozen=True)
class ModelProgressEvent:
    """Bounded public progress metadata; never carries raw reasoning content."""

    event_type: str
    purpose: str
    attempt: int
    status: str = "SUCCESS"
    elapsed_ms: int = 0
    reasoning_chars: int = 0
    output_chars: int = 0
    summary: str | None = None
    error_code: AnalysisErrorCode | None = None


ModelProgressCallback = Callable[[ModelProgressEvent], None]


class StructuredModelError(RuntimeError):
    """Safe model failure suitable for a degraded Artifact and small Trace."""

    def __init__(
        self,
        code: AnalysisErrorCode,
        message: str,
        *,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.attempts = attempts


@dataclass(frozen=True)
class StructuredModelResult(Generic[OutputT]):
    output: OutputT
    model_name: str
    attempts: int
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    call_id: str | None = None
    accounting_complete: bool = False


@runtime_checkable
class StructuredModel(Protocol):
    model_name: str

    def generate(
        self,
        *,
        output_schema: type[OutputT],
        purpose: str,
        prompt_version: str,
        system_prompt: str,
        payload: dict[str, Any],
        max_output_tokens: int | None = None,
        progress_callback: ModelProgressCallback | None = None,
    ) -> StructuredModelResult[OutputT]: ...


class OpenAICompatibleStructuredModel:
    """JSON-only model adapter with bounded retries and no tool access."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model_name: str,
        timeout_seconds: float,
        max_attempts: int,
        max_input_chars: int,
        max_output_tokens: int,
        enable_thinking: bool | None = None,
        thinking_ttft_seconds: float = 30.0,
        thinking_budget_tokens: int = 800,
        process_summary_max_chars: int = 400,
        client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise StructuredModelError(
                "CONFIG_ERROR",
                "MODEL_API_KEY is required when ANALYSIS_MODE=llm",
            )
        if not model_name.strip():
            raise StructuredModelError(
                "CONFIG_ERROR",
                "ANALYSIS_MODEL or MODEL_NAME is required when ANALYSIS_MODE=llm",
            )
        self.model_name = model_name
        self._max_attempts = max_attempts
        self._max_input_chars = max_input_chars
        self._max_output_tokens = max_output_tokens
        self._enable_thinking = enable_thinking
        self._thinking_ttft_seconds = thinking_ttft_seconds
        self._thinking_budget_tokens = thinking_budget_tokens
        self._process_summary_max_chars = process_summary_max_chars
        self._structured_timeout_seconds = timeout_seconds
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    @staticmethod
    def _emit(
        callback: ModelProgressCallback | None, event: ModelProgressEvent
    ) -> None:
        if event.event_type.endswith("RETRY"):
            emit(
                "RETRY",
                purpose=event.purpose,
                attempt=event.attempt,
                status="RETRY",
                error_code=event.error_code,
            )
        elif event.event_type.endswith("FAILED"):
            emit(
                "DEGRADED",
                purpose=event.purpose,
                attempt=event.attempt,
                status="DEGRADED",
                error_code=event.error_code,
            )
        if callback is not None:
            callback(event)

    @staticmethod
    def _client_with_timeout(client: Any, timeout_seconds: float) -> Any:
        with_options = getattr(client, "with_options", None)
        return (
            with_options(timeout=timeout_seconds) if callable(with_options) else client
        )

    @model_attempt
    def _stream_completion(
        self,
        *,
        request: dict[str, Any],
        timeout_seconds: float,
        purpose: str,
        attempt: int,
        phase: str,
        progress_callback: ModelProgressCallback | None,
    ) -> tuple[str, Any | None, int]:
        started = perf_counter()
        client = self._client_with_timeout(self._client, timeout_seconds)
        stream = client.chat.completions.create(
            **request,
            stream=True,
            stream_options={"include_usage": True},
        )
        response = getattr(stream, "response", None)
        headers = getattr(response, "headers", {})
        transport_metadata(
            http_status=getattr(response, "status_code", None),
            request_id=headers.get("x-request-id"),
        )
        first_token_seen = False
        reasoning_chars = 0
        emitted_reasoning_chars = 0
        content_parts: list[str] = []
        usage: Any | None = None
        for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage = chunk_usage
            choices = getattr(chunk, "choices", None)
            if choices and getattr(choices[0], "finish_reason", None):
                transport_metadata(finish_reason=choices[0].finish_reason)
            if not choices:
                continue
            delta = choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            content = getattr(delta, "content", None)
            if isinstance(reasoning, str) and reasoning:
                reasoning_chars += len(reasoning)
            if isinstance(content, str) and content:
                content_parts.append(content)
            if not first_token_seen and (reasoning or content):
                elapsed_ms = max(0, round((perf_counter() - started) * 1000))
                if elapsed_ms > round(timeout_seconds * 1000):
                    raise TimeoutError("model first token timeout")
                first_token_seen = True
                emit(
                    "LLM_FIRST_TOKEN",
                    purpose=purpose,
                    attempt=attempt,
                    phase=phase,
                    status="SUCCESS",
                    duration_ms=elapsed_ms,
                )
                self._emit(
                    progress_callback,
                    ModelProgressEvent(
                        event_type=f"LLM_{phase}_FIRST_TOKEN",
                        purpose=purpose,
                        attempt=attempt,
                        elapsed_ms=elapsed_ms,
                        reasoning_chars=reasoning_chars,
                        output_chars=sum(len(item) for item in content_parts),
                    ),
                )
            if phase == "PROCESS" and reasoning_chars - emitted_reasoning_chars >= 256:
                emitted_reasoning_chars = reasoning_chars
                self._emit(
                    progress_callback,
                    ModelProgressEvent(
                        event_type="LLM_PROCESS_PROGRESS",
                        purpose=purpose,
                        attempt=attempt,
                        elapsed_ms=max(0, round((perf_counter() - started) * 1000)),
                        reasoning_chars=reasoning_chars,
                        output_chars=sum(len(item) for item in content_parts),
                    ),
                )
        return "".join(content_parts), usage, reasoning_chars

    def _public_process_summary(
        self,
        *,
        purpose: str,
        prompt_version: str,
        payload: dict[str, Any],
        progress_callback: ModelProgressCallback | None,
    ) -> str | None:
        if self._enable_thinking is not True:
            return None
        process_messages = [
            {"role": "system", "content": _PROCESS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "purpose": purpose,
                        "prompt_version": prompt_version,
                        "process_prompt_version": _PROCESS_PROMPT_VERSION,
                        "input": payload,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
        ]
        for attempt in range(1, self._max_attempts + 1):
            self._emit(
                progress_callback,
                ModelProgressEvent(
                    event_type="LLM_PROCESS_START",
                    purpose=purpose,
                    attempt=attempt,
                ),
            )
            try:
                content, _, reasoning_chars = self._stream_completion(
                    request={
                        "model": self.model_name,
                        "messages": process_messages,
                        "temperature": 0,
                        "max_tokens": max(
                            self._max_output_tokens,
                            self._thinking_budget_tokens + 400,
                        ),
                        "extra_body": {
                            "enable_thinking": True,
                            "thinking_budget": self._thinking_budget_tokens,
                        },
                    },
                    timeout_seconds=self._thinking_ttft_seconds,
                    purpose=purpose,
                    attempt=attempt,
                    phase="PROCESS",
                    progress_callback=progress_callback,
                )
                summary = " ".join(_CONTROL_CHARS.sub("", content).split())
                summary = summary[: self._process_summary_max_chars]
                if not summary:
                    raise ValueError("model returned empty public process summary")
                self._emit(
                    progress_callback,
                    ModelProgressEvent(
                        event_type="LLM_PROCESS_SUMMARY",
                        purpose=purpose,
                        attempt=attempt,
                        reasoning_chars=reasoning_chars,
                        output_chars=len(content),
                        summary=summary,
                    ),
                )
                return summary
            except (OpenAIError, TimeoutError, ValueError, IndexError, AttributeError):
                retrying = attempt < self._max_attempts
                self._emit(
                    progress_callback,
                    ModelProgressEvent(
                        event_type=(
                            "LLM_PROCESS_RETRY" if retrying else "LLM_PROCESS_FAILED"
                        ),
                        purpose=purpose,
                        attempt=attempt,
                        status="RETRY" if retrying else "FAILED",
                        error_code="MODEL_ERROR",
                    ),
                )
        return None

    @model_call
    def generate(
        self,
        *,
        output_schema: type[OutputT],
        purpose: str,
        prompt_version: str,
        system_prompt: str,
        payload: dict[str, Any],
        max_output_tokens: int | None = None,
        progress_callback: ModelProgressCallback | None = None,
    ) -> StructuredModelResult[OutputT]:
        serialized_input = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(serialized_input) > self._max_input_chars:
            raise StructuredModelError(
                "INPUT_TOO_LARGE",
                "structured model input exceeds configured limit",
            )
        process_summary = self._public_process_summary(
            purpose=purpose,
            prompt_version=prompt_version,
            payload=payload,
            progress_callback=progress_callback,
        )
        user_payload = json.dumps(
            {
                "purpose": purpose,
                "prompt_version": prompt_version,
                "input": payload,
                "public_process_summary": process_summary,
                "output_json_schema": output_schema.model_json_schema(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_payload},
        ]
        started = perf_counter()
        last_error: Exception | None = None
        last_code: AnalysisErrorCode = "MODEL_ERROR"
        for attempt in range(1, self._max_attempts + 1):
            self._emit(
                progress_callback,
                ModelProgressEvent(
                    event_type="LLM_STRUCTURED_START",
                    purpose=purpose,
                    attempt=attempt,
                ),
            )
            try:
                request: dict[str, Any] = {
                    "model": self.model_name,
                    "messages": messages,
                    "temperature": 0,
                    "max_tokens": (
                        max_output_tokens
                        if max_output_tokens is not None
                        else self._max_output_tokens
                    ),
                    "response_format": {"type": "json_object"},
                }
                if self._enable_thinking is not None:
                    request["extra_body"] = {"enable_thinking": False}
                content, usage, _ = self._stream_completion(
                    request=request,
                    timeout_seconds=self._structured_timeout_seconds,
                    purpose=purpose,
                    attempt=attempt,
                    phase="STRUCTURED",
                    progress_callback=progress_callback,
                )
                if not isinstance(content, str) or not content.strip():
                    emit(
                        "LLM_VALIDATION",
                        validation_stage="json",
                        status="INVALID_JSON",
                        purpose=purpose,
                        attempt=attempt,
                    )
                    raise ValueError("model returned empty content")
                try:
                    json.loads(content)
                except (ValueError, TypeError):
                    emit(
                        "LLM_VALIDATION",
                        validation_stage="json",
                        status="INVALID_JSON",
                        purpose=purpose,
                        attempt=attempt,
                    )
                    raise
                emit(
                    "LLM_VALIDATION",
                    validation_stage="json",
                    status="SUCCESS",
                    purpose=purpose,
                    attempt=attempt,
                )
                try:
                    output = output_schema.model_validate_json(content)
                except ValidationError:
                    emit(
                        "LLM_VALIDATION",
                        validation_stage="schema",
                        status="INVALID_SCHEMA",
                        purpose=purpose,
                        attempt=attempt,
                    )
                    raise
                emit(
                    "LLM_VALIDATION",
                    validation_stage="schema",
                    status="SUCCESS",
                    purpose=purpose,
                    attempt=attempt,
                )
                return StructuredModelResult(
                    output=output,
                    model_name=self.model_name,
                    attempts=attempt,
                    latency_ms=max(0, round((perf_counter() - started) * 1000)),
                    call_id=(CONTEXT.get() or {}).get("call_id"),
                    input_tokens=getattr(usage, "prompt_tokens", None),
                    output_tokens=getattr(usage, "completion_tokens", None),
                    accounting_complete=self._enable_thinking is not True
                    and attempt == 1,
                )
            except (ValidationError, ValueError, IndexError, AttributeError) as exc:
                last_error = exc
                last_code = "INVALID_OUTPUT"
            except (OpenAIError, TimeoutError) as exc:
                last_error = exc
                last_code = "MODEL_ERROR"
            if attempt < self._max_attempts:
                self._emit(
                    progress_callback,
                    ModelProgressEvent(
                        event_type="LLM_STRUCTURED_RETRY",
                        purpose=purpose,
                        attempt=attempt,
                        status="RETRY",
                        error_code=last_code,
                    ),
                )
        message = (
            "structured model returned invalid output"
            if last_code == "INVALID_OUTPUT"
            else "structured model request failed"
        )
        raise StructuredModelError(
            last_code,
            message,
            attempts=self._max_attempts,
        ) from last_error


def build_analysis_model(
    settings: Settings, *, client: Any | None = None
) -> StructuredModel | None:
    if settings.analysis_mode == "deterministic":
        return None
    return OpenAICompatibleStructuredModel(
        api_key=settings.model_api_key.get_secret_value(),
        base_url=settings.model_base_url,
        model_name=settings.analysis_model or settings.model_name,
        timeout_seconds=settings.analysis_llm_timeout_seconds,
        max_attempts=settings.analysis_llm_max_retry + 1,
        max_input_chars=settings.analysis_llm_max_input_chars,
        max_output_tokens=settings.analysis_llm_max_output_tokens,
        enable_thinking=settings.analysis_llm_enable_thinking,
        thinking_ttft_seconds=settings.analysis_llm_thinking_ttft_seconds,
        thinking_budget_tokens=settings.analysis_llm_thinking_budget_tokens,
        process_summary_max_chars=settings.analysis_llm_process_summary_max_chars,
        client=client,
    )
