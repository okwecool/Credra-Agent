"""Trusted UI policy, frozen per task before any model request."""

import hashlib
import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config import Settings
from credra_agent.planning.models import CoordinatorLimits, RunAuthorization


class UIPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["agent_ui_policy_v1"] = "agent_ui_policy_v1"
    policy_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    approval: Literal["UNCONFIRMED", "APPROVED"]
    allowed_subject_ids: list[str] = Field(min_length=1)
    external_request_limit: int | None = Field(default=None, ge=1)
    token_limit: int | None = Field(default=None, ge=1)
    active_seconds_limit: float | None = Field(default=None, gt=0)
    limits: CoordinatorLimits
    intent_attempt_reservation: int = Field(ge=1, le=6)
    intent_token_reservation: int = Field(ge=1)
    intent_max_output_tokens: int = Field(ge=200, le=8_000)

    @model_validator(mode="after")
    def valid_caps(self):
        if self.intent_max_output_tokens > self.intent_token_reservation:
            raise ValueError("intent output exceeds reserved tokens")
        if self.limits.decision_max_output_tokens < 100:
            raise ValueError("UI coordinator output must be at least 100 tokens")
        self.authorization("validation", 1)
        return self

    @property
    def reference(self) -> str:
        content = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        digest = hashlib.sha256(content.encode()).hexdigest()
        return f"{self.policy_id}:v{self.version}:sha256:{digest}"

    def authorization(self, task_id: str, spec_version: int, *, auth_id=None):
        return RunAuthorization(
            authorization_id=auth_id or f"ui-{uuid4().hex}",
            authorized_by="RUNTIME_POLICY",
            task_id=task_id,
            policy_ref=self.reference,
            task_spec_version=spec_version,
            approval=self.approval,
            external_request_limit=self.external_request_limit,
            token_limit=self.token_limit,
            active_seconds_limit=self.active_seconds_limit,
            limits=self.limits,
        )


def load_ui_policy(settings: Settings) -> UIPolicy:
    if settings.agent_ui_policy_path is None:
        raise ValueError("AGENT_UI_POLICY_PATH_REQUIRED")
    try:
        policy = UIPolicy.model_validate_json(
            Path(settings.agent_ui_policy_path).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        # Policy files are user maintained; never echo their contents or input values.
        raise ValueError(
            "AGENT_UI_POLICY_INVALID: 请检查策略路径、字段和预算上限"
        ) from exc
    if policy.approval != "APPROVED":
        raise ValueError("AGENT_UI_POLICY_UNCONFIRMED")
    return policy


def model_profile(settings: Settings) -> dict:
    """Freeze request-affecting configuration, excluding all secrets."""
    prefixes = (
        "model_",
        "analysis_",
        "intent_",
        "research_",
        "search_",
        "content_",
        "fact_",
        "verification_",
    )
    profile = {}
    for field in type(settings).model_fields:
        value = getattr(settings, field)
        if field.startswith(prefixes) and not hasattr(value, "get_secret_value"):
            profile[field] = str(value) if isinstance(value, Path) else value
    return profile
