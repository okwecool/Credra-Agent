"""Public result contract for natural-language execution orchestration."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from credra_agent.intent.models import IntentResult


class NaturalLanguageRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["natural_language_run_v1"] = "natural_language_run_v1"
    intent: IntentResult
    execution_mode: Literal["baseline", "shadow", "agentic"]
    outcome: Literal[
        "PARSED_ONLY",
        "AUTHORIZATION_REQUIRED",
        "CONFIGURATION_REQUIRED",
        "WAITING_CLARIFICATION",
        "CONTROL_UNAVAILABLE",
        "TASK_STATE",
    ]
    task: dict | None = None
    baseline_task: dict | None = None
    baseline_thread_id: str | None = None
    limitations: list[str] = Field(default_factory=list)
