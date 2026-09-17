"""Deterministic authorization gates for model-proposed actions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256

from pydantic import BaseModel, ValidationError

from credra_agent.execution.models import (
    ComputeMetricsArgs,
    ReferenceArgs,
    SearchEvidenceArgs,
    VerifyClaimArgs,
)
from credra_agent.execution.registry import ActionRegistry, ToolUnavailableError
from credra_agent.intent.models import TaskSpec


class PolicyViolation(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_action_signature(tool: str, arguments: dict) -> str:
    if tool == "verify_claim":
        arguments = {
            "claim_id": arguments.get("claim_id"),
            "document_ids": sorted(set(arguments.get("document_ids", []))),
        }
    elif tool == "read_document":
        arguments = {"document_id": arguments.get("document_id")}
    body = json.dumps(
        {"tool": tool, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthorizedAction:
    arguments: BaseModel
    signature: str


class ActionPolicy:
    def __init__(self, registry: ActionRegistry) -> None:
        self.registry = registry

    def authorize(
        self,
        *,
        tool: str,
        arguments: dict,
        task_spec: TaskSpec,
        executable_tools: set[str],
        available_refs: set[str],
        prior_signatures: set[str],
        no_progress_locked: bool = False,
    ) -> AuthorizedAction:
        if tool not in executable_tools:
            raise PolicyViolation("TOOL_NOT_EXECUTABLE", tool)
        if no_progress_locked and tool not in {"ask_user", "finish"}:
            raise PolicyViolation("NO_PROGRESS_LOCK", tool)
        try:
            typed = self.registry.validate(tool, arguments)
        except ToolUnavailableError as exc:
            raise PolicyViolation("TOOL_UNAVAILABLE", tool) from exc
        except ValidationError as exc:
            raise PolicyViolation("INVALID_ARGUMENTS", tool) from exc

        if isinstance(typed, SearchEvidenceArgs):
            self._validate_search(typed, task_spec)
        self._validate_references(typed, available_refs)
        self._validate_reference_types(typed)

        signature = canonical_action_signature(tool, typed.model_dump(mode="json"))
        if signature in prior_signatures:
            if tool == "read_document":
                raise PolicyViolation(
                    "DOCUMENT_ALREADY_READ",
                    "use the retained document_read and propose verify_claim",
                )
            raise PolicyViolation("DUPLICATE_ACTION", tool)
        return AuthorizedAction(arguments=typed, signature=signature)

    @staticmethod
    def _validate_search(args: SearchEvidenceArgs, task_spec: TaskSpec) -> None:
        if (
            args.subject_id != task_spec.subject_id
            or args.subject_name != task_spec.subject_name
        ):
            raise PolicyViolation("SUBJECT_SCOPE_VIOLATION", args.subject_id)
        if args.period.end > task_spec.as_of or not any(
            allowed.start <= args.period.start and args.period.end <= allowed.end
            for allowed in task_spec.periods
        ):
            raise PolicyViolation("PERIOD_SCOPE_VIOLATION", str(args.period))

        task_policy = task_spec.source_policy
        action_policy = args.source_policy
        canonical = task_policy.canonical_tags
        task_denied = canonical(task_policy.denied)
        action_denied = canonical(action_policy.denied)
        if not task_denied <= action_denied:
            raise PolicyViolation("SOURCE_DENYLIST_WIDENED", "denied")
        if canonical(action_policy.preferred) & task_denied:
            raise PolicyViolation("SOURCE_POLICY_VIOLATION", "preferred")
        if not canonical(task_policy.preferred) <= canonical(action_policy.preferred):
            raise PolicyViolation("SOURCE_PREFERENCE_DROPPED", "preferred")
        if task_policy.allowed is not None and (
            action_policy.allowed is None
            or not canonical(action_policy.allowed) <= canonical(task_policy.allowed)
        ):
            raise PolicyViolation("SOURCE_ALLOWLIST_WIDENED", "allowed")

    @staticmethod
    def _validate_references(arguments: BaseModel, available_refs: set[str]) -> None:
        refs: list[str] = []
        if isinstance(arguments, ReferenceArgs):
            refs = [arguments.reference_id]
        elif isinstance(arguments, VerifyClaimArgs):
            refs = arguments.source_refs
        elif isinstance(arguments, ComputeMetricsArgs):
            refs = arguments.input_refs
        missing = sorted(set(refs) - available_refs)
        if missing:
            raise PolicyViolation("UNKNOWN_REFERENCE", missing[0])

    @staticmethod
    def _validate_reference_types(arguments: BaseModel) -> None:
        if isinstance(arguments, VerifyClaimArgs) and any(
            not reference.startswith("artifacts/agent_evidence_v")
            for reference in arguments.source_refs
        ):
            raise PolicyViolation(
                "INVALID_REFERENCE_TYPE",
                "verify_claim requires an agent_evidence bundle reference",
            )
