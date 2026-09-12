"""Injected action handlers and bounded execution outcomes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from credra_agent.evidence.models import EvidenceBundle


class ExecutionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["SUCCESS", "NO_RESULT", "UNAVAILABLE", "MISSING_DATA", "FAILED"]
    summary: str = Field(min_length=1, max_length=2000)
    payload: dict = Field(default_factory=dict)
    evidence_bundle: EvidenceBundle | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    novelty_keys: list[str] = Field(default_factory=list)
    answered_question_ids: list[str] = Field(default_factory=list)
    gap_question_ids: list[str] = Field(default_factory=list)
    conflict_ids: list[str] = Field(default_factory=list)
    error_code: str | None = None
    actual_external_requests: int | None = Field(default=None, ge=0)


ActionHandler = Callable[[BaseModel], ExecutionOutcome]


@dataclass(frozen=True)
class HandlerDefinition:
    handler: ActionHandler
    external_request_reservation: int = 0


class ActionExecutor:
    def __init__(self, handlers: dict[str, HandlerDefinition] | None = None) -> None:
        self._handlers = handlers or {}

    @property
    def executable_tools(self) -> set[str]:
        return set(self._handlers)

    def reservation_for(self, tool: str) -> int:
        try:
            return self._handlers[tool].external_request_reservation
        except KeyError as exc:
            raise KeyError(f"no execution handler for {tool}") from exc

    def execute(self, tool: str, arguments: BaseModel) -> ExecutionOutcome:
        try:
            definition = self._handlers[tool]
        except KeyError as exc:
            raise KeyError(f"no execution handler for {tool}") from exc
        try:
            return definition.handler(arguments)
        except Exception as exc:  # noqa: BLE001 - boundary converts to safe outcome
            return ExecutionOutcome(
                status="FAILED",
                summary="工具执行失败；详细异常仅记录在服务日志中。",
                error_code=type(exc).__name__,
                actual_external_requests=definition.external_request_reservation,
            )
