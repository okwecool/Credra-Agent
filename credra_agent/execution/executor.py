"""Injected action handlers and bounded execution outcomes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.tools.artifacts import ArtifactStore
from credra_agent.evidence.models import EvidenceBundle
from credra_agent.execution.model_budget import model_reservation
from credra_agent.intent.models import TaskSpec
from credra_agent.observability.runtime import emit

if TYPE_CHECKING:
    from credra_agent.planning.models import CoordinatorLimits


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
    actual_tokens: int | None = Field(default=0, ge=0)


ActionHandler = Callable[[BaseModel], ExecutionOutcome]


@dataclass(frozen=True)
class ExecutionContext:
    artifacts: ArtifactStore
    task_spec: TaskSpec
    limits: CoordinatorLimits
    available_refs: set[str]


@dataclass(frozen=True)
class HandlerDefinition:
    handler: ActionHandler
    external_request_reservation: int = 0
    contextual: bool = False
    uses_model: bool = False
    model: object | None = None


class ActionExecutor:
    def __init__(self, handlers: dict[str, HandlerDefinition] | None = None) -> None:
        self._handlers = handlers or {}

    @property
    def executable_tools(self) -> set[str]:
        return set(self._handlers)

    def reservation_for(
        self, tool: str, limits: CoordinatorLimits | None = None
    ) -> int:
        try:
            definition = self._handlers[tool]
            if definition.uses_model and limits is None:
                raise ValueError("model tool requires explicit per-run limits")
            return definition.external_request_reservation + (
                model_reservation(definition.model, limits)[0]
                if definition.uses_model
                else 0
            )
        except KeyError as exc:
            raise KeyError(f"no execution handler for {tool}") from exc

    def token_reservation_for(self, tool: str, limits: CoordinatorLimits) -> int:
        return (
            model_reservation(self._handlers[tool].model, limits)[1]
            if self._handlers[tool].uses_model
            else 0
        )

    def execute(
        self,
        tool: str,
        arguments: BaseModel,
        *,
        context: ExecutionContext | None = None,
    ) -> ExecutionOutcome:
        try:
            definition = self._handlers[tool]
        except KeyError as exc:
            raise KeyError(f"no execution handler for {tool}") from exc
        try:
            if definition.contextual:
                if context is None:
                    return ExecutionOutcome(
                        status="UNAVAILABLE",
                        summary="工具需要运行范围和 Artifact 上下文。",
                        error_code="EXECUTION_CONTEXT_REQUIRED",
                        actual_external_requests=0,
                    )
                return definition.handler(arguments, context)
            return definition.handler(arguments)
        except Exception as exc:  # noqa: BLE001 - boundary converts to safe outcome
            emit("TOOL_END", tool=tool, status="FAILED", exception=exc)
            return ExecutionOutcome(
                status="FAILED",
                summary="工具执行失败；详细异常仅记录在服务日志中。",
                error_code=type(exc).__name__,
                actual_external_requests=None
                if definition.uses_model
                else definition.external_request_reservation,
                actual_tokens=None if definition.uses_model else 0,
            )
