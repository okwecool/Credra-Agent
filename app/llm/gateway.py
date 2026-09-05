"""Provider-neutral structured model calls over an OpenAI-compatible API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from openai import OpenAI, OpenAIError
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.models.analysis import AnalysisErrorCode

OutputT = TypeVar("OutputT", bound=BaseModel)


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
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )

    def generate(
        self,
        *,
        output_schema: type[OutputT],
        purpose: str,
        prompt_version: str,
        system_prompt: str,
        payload: dict[str, Any],
        max_output_tokens: int | None = None,
    ) -> StructuredModelResult[OutputT]:
        serialized_input = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(serialized_input) > self._max_input_chars:
            raise StructuredModelError(
                "INPUT_TOO_LARGE",
                "structured model input exceeds configured limit",
            )
        user_payload = json.dumps(
            {
                "purpose": purpose,
                "prompt_version": prompt_version,
                "input": payload,
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
                    request["extra_body"] = {"enable_thinking": self._enable_thinking}
                response = self._client.chat.completions.create(
                    **request,
                )
                content = response.choices[0].message.content
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("model returned empty content")
                output = output_schema.model_validate_json(content)
                usage = getattr(response, "usage", None)
                return StructuredModelResult(
                    output=output,
                    model_name=self.model_name,
                    attempts=attempt,
                    latency_ms=max(0, round((perf_counter() - started) * 1000)),
                    input_tokens=getattr(usage, "prompt_tokens", None),
                    output_tokens=getattr(usage, "completion_tokens", None),
                )
            except (ValidationError, ValueError, IndexError, AttributeError) as exc:
                last_error = exc
                last_code = "INVALID_OUTPUT"
            except OpenAIError as exc:
                last_error = exc
                last_code = "MODEL_ERROR"
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
        client=client,
    )
