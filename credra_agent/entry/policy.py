"""User-maintained conversation authorization, independent of task authorization."""

import hashlib
import json
from typing import Literal

from pydantic import Field, model_validator

from credra_agent.entry.models import EntryContext, EntryDecision, EntryModel


class EntryPolicy(EntryModel):
    schema_version: Literal["agent_entry_policy_v1"] = "agent_entry_policy_v1"
    policy_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    approval: Literal["UNCONFIRMED", "APPROVED"]
    allowed_subject_ids: list[str] = Field(min_length=1)
    allowed_control_tools: list[
        Literal["prepare_investigation", "submit_clarification", "resume_investigation"]
    ] = Field(default_factory=list, max_length=3)
    external_request_limit: int | None = Field(default=None, ge=1)
    token_limit: int | None = Field(default=None, ge=1)
    active_seconds_limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_model_rounds: int = Field(default=4, ge=1, le=12)
    max_tool_calls: int = Field(default=3, ge=1, le=20)
    model_attempt_reservation: int = Field(default=2, ge=1, le=6)
    model_token_reservation: int = Field(default=6000, ge=1)
    max_output_tokens: int = Field(default=1200, ge=200, le=8000)
    max_input_chars: int = Field(default=30000, ge=1000, le=200000)

    @model_validator(mode="after")
    def coherent(self):
        if self.max_output_tokens > self.model_token_reservation:
            raise ValueError("ENTRY_OUTPUT_EXCEEDS_RESERVATION")
        if self.approval == "APPROVED" and any(
            value is None
            for value in (
                self.external_request_limit,
                self.token_limit,
                self.active_seconds_limit,
            )
        ):
            raise ValueError("ENTRY_APPROVED_CAPS_REQUIRED")
        return self

    @property
    def reference(self):
        return (
            "entry-policy:"
            + hashlib.sha256(self.model_dump_json().encode()).hexdigest()
        )


def load_entry_policy(settings):
    if settings.agent_entry_policy_path is None:
        raise ValueError("AGENT_ENTRY_POLICY_PATH_REQUIRED")
    try:
        policy = EntryPolicy.model_validate_json(
            settings.agent_entry_policy_path.read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ValueError("AGENT_ENTRY_POLICY_INVALID") from exc
    if policy.approval != "APPROVED":
        raise ValueError("AGENT_ENTRY_POLICY_UNCONFIRMED")
    return policy


def request_profile(settings, policy, prompt_version):
    values = {
        "model": settings.intent_model
        or settings.analysis_model
        or settings.model_name,
        "base_url": settings.model_base_url,
        "timeout": settings.analysis_llm_timeout_seconds,
        "attempts": min(
            settings.analysis_llm_max_retry + 1, policy.model_attempt_reservation
        ),
        "max_input": min(settings.analysis_llm_max_input_chars, policy.max_input_chars),
        "max_output": policy.max_output_tokens,
        "prompt": prompt_version,
        "schema": hashlib.sha256(
            json.dumps(
                [EntryDecision.model_json_schema(), EntryContext.model_json_schema()],
                sort_keys=True,
            ).encode()
        ).hexdigest(),
    }
    # Store only a digest. Neither URL credentials nor API keys reach a profile dump.
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
