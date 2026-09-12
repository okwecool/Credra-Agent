"""Single registry for tool availability and argument validation."""

from dataclasses import dataclass
from functools import lru_cache

from pydantic import BaseModel

from credra_agent.execution.models import (
    AskUserArgs,
    ComparePeersArgs,
    ComputeMetricsArgs,
    FinishArgs,
    ReferenceArgs,
    RunScenarioArgs,
    SearchEvidenceArgs,
    ToolName,
    VerifyClaimArgs,
)


class ToolUnavailableError(ValueError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    name: ToolName
    arguments_model: type[BaseModel]
    available: bool
    read_only: bool
    unavailable_reason: str | None = None


class ActionRegistry:
    def __init__(self, definitions: list[ToolDefinition]) -> None:
        self._definitions = {item.name: item for item in definitions}

    def definition(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]  # type: ignore[index]
        except KeyError as exc:
            raise ToolUnavailableError(f"UNREGISTERED_TOOL: {name}") from exc

    def validate(
        self, name: str, arguments: dict, *, require_available: bool = True
    ) -> BaseModel:
        definition = self.definition(name)
        if require_available and not definition.available:
            raise ToolUnavailableError(
                f"UNAVAILABLE: {name}: {definition.unavailable_reason or 'not implemented'}"
            )
        return definition.arguments_model.model_validate(arguments)

    def catalog(self) -> list[dict]:
        return [
            {
                "name": item.name,
                "available": item.available,
                "read_only": item.read_only,
                "unavailable_reason": item.unavailable_reason,
                "arguments_schema": item.arguments_model.model_json_schema(),
            }
            for item in self._definitions.values()
        ]


@lru_cache(maxsize=1)
def default_registry() -> ActionRegistry:
    return ActionRegistry(
        [
            ToolDefinition("read_document", ReferenceArgs, True, True),
            ToolDefinition("search_evidence", SearchEvidenceArgs, True, True),
            ToolDefinition("fetch_content", ReferenceArgs, True, True),
            ToolDefinition("verify_claim", VerifyClaimArgs, True, True),
            ToolDefinition("compute_metrics", ComputeMetricsArgs, True, True),
            ToolDefinition(
                "compare_peers", ComparePeersArgs, False, True, "planned for V2-3"
            ),
            ToolDefinition(
                "run_scenario", RunScenarioArgs, False, True, "planned for V2-3"
            ),
            ToolDefinition("ask_user", AskUserArgs, True, True),
            ToolDefinition("finish", FinishArgs, True, True),
        ]
    )
