"""Versioned LangGraph with durable decisions, dispatch and result replay."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.llm.gateway import StructuredModel, StructuredModelError
from app.tools.artifacts import ArtifactStore
from credra_agent.evidence.artifacts import (
    validate_evidence_artifacts,
    write_evidence_artifacts,
)
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionContext,
    ExecutionOutcome,
)
from credra_agent.execution.ledger import (
    ActionLedger,
    DurableBudgetExhausted,
    ExecutionRecord,
)
from credra_agent.execution.model_budget import external_usage, model_reservation
from credra_agent.execution.models import Action, AskUserArgs
from credra_agent.execution.policy import ActionPolicy, PolicyViolation
from credra_agent.execution.registry import ActionRegistry, default_registry
from credra_agent.graph.state import AgenticState
from credra_agent.intent.models import TaskSpec
from credra_agent.observability.events import log_context
from credra_agent.observability.runtime import emit, require_logging
from credra_agent.planning.coordinator import (
    PROMPT_PATH,
    PROMPT_VERSION,
    DecisionFallback,
)
from credra_agent.planning.evidence_context import (
    assess_questions,
    coordinator_evidence_context,
    current_conflicts,
    store_proposals,
)
from credra_agent.planning.models import (
    Coverage,
    DecisionDraft,
    HypothesisState,
    Observation,
    ObservationIndexItem,
    RunAuthorization,
)

FaultHook = Callable[[str, Action], None]
NodeFunction = Callable[[AgenticState], dict]


def _fingerprint(value: dict) -> str:
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _observed_node(name: str, function: NodeFunction) -> NodeFunction:
    def wrapped(state: AgenticState) -> dict:
        with log_context(node=name):
            require_logging()
            before = state.get("status")
            emit("NODE_START", status="STARTED", node=name)
            try:
                result = function(state)
            except Exception as exc:
                emit("NODE_END", status="FAILED", node=name, exception=exc)
                raise
            after = result.get("status", before)
            emit("NODE_END", status="SUCCESS", node=name)
            if before != after:
                emit("TASK_STATE", from_state=before, to_state=after, status="SUCCESS")
            return result

    return wrapped


def _coverage(required: list[str], observations: list[Observation]) -> Coverage:
    answered: set[str] = set()
    gaps: set[str] = set()
    conflicts: set[str] = set()
    for item in observations:
        answered.update(item.answered_question_ids)
        gaps.update(item.gap_question_ids)
        conflicts.update(item.conflict_ids)
    gaps.difference_update(answered)
    gaps.update(set(required) - answered)
    return Coverage(
        required_question_ids=required,
        answered_question_ids=sorted(answered & set(required)),
        gap_question_ids=sorted(gaps),
        unresolved_conflict_ids=sorted(conflicts),
        review_required=bool(conflicts),
    )


class AgenticGraphRuntime:
    """Node implementation. Full documents remain in artifacts, not checkpoints."""

    def __init__(
        self,
        *,
        run_dir: Path,
        ledger: ActionLedger,
        model: StructuredModel,
        executor: ActionExecutor,
        registry: ActionRegistry | None = None,
        fallback: DecisionFallback | None = None,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.artifacts = ArtifactStore(run_dir)
        self.ledger = ledger
        self.model = model
        self.executor = executor
        self.registry = registry or default_registry()
        self.policy = ActionPolicy(self.registry)
        self.fallback = fallback
        self.fault_hook = fault_hook
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def _read_task(self, state: AgenticState) -> TaskSpec:
        return TaskSpec.model_validate(self.artifacts.read_json(state["task_spec_ref"]))

    def _read_authorization(self, state: AgenticState) -> RunAuthorization:
        return RunAuthorization.model_validate(
            self.artifacts.read_json(state["authorization_ref"])
        )

    def _read_hypotheses(self, state: AgenticState) -> list[HypothesisState]:
        payload = self.artifacts.read_json(state["hypotheses_ref"])
        return [HypothesisState.model_validate(item) for item in payload["items"]]

    def _read_observations(self, state: AgenticState) -> list[Observation]:
        payload = self.artifacts.read_json(state["observation_index_ref"])
        return [
            Observation.model_validate(
                self.artifacts.read_json(item["observation_ref"])
            )
            for item in payload["items"]
        ]

    def _write_next(self, base: str, current: str | None, value: dict) -> str:
        reference = self.artifacts.next_version_reference(base, current)
        while True:
            try:
                return self.artifacts.write_json(reference, value)
            except FileExistsError:
                reference = self.artifacts.next_version_reference(base, reference)

    def _budget_ref(self, state: AgenticState, authorization: RunAuthorization) -> str:
        snapshot = self.ledger.budget_snapshot(state["task_id"], authorization)
        emit(
            "BUDGET_STATE",
            phase="durable",
            status="UNKNOWN" if snapshot["usage_uncertain"] else "SUCCESS",
            external_requests=snapshot["external_spent"],
            token_units=snapshot["token_spent"],
        )
        return self._write_next(
            "agent_budget_ledger",
            state["budget_ledger_ref"],
            snapshot,
        )

    @staticmethod
    def _limited(state: AgenticState, reason: str, budget_ref: str) -> dict:
        emit("STOP", status="DEGRADED", error_code=reason)
        return {
            "status": "LIMITED",
            "current_node": "decide",
            "stop_reason": reason,
            "coverage_ref": state.get("coverage_ref"),
            "budget_ledger_ref": budget_ref,
            "limitations": [*state.get("limitations", []), reason],
        }

    def _active_from_record(self, record: ExecutionRecord) -> dict:
        emit(
            "ACTION_STATE",
            status="REUSED",
            action_id=record.action.action_id,
            tool=record.action.tool,
        )
        return {
            "status": "RUNNING",
            "current_node": "decide",
            "active_action_id": record.action.action_id,
            "active_action_ref": self._action_ref(record.action.plan_version),
            "active_decision_ref": record.decision_ref,
        }

    @staticmethod
    def _action_ref(plan_version: int) -> str:
        return f"artifacts/agent_action_v{plan_version}.json"

    def decide(self, state: AgenticState) -> dict:
        require_logging()
        task = self._read_task(state)
        authorization = self._read_authorization(state)
        model_external, model_tokens = model_reservation(
            self.model, authorization.limits
        )
        plan_version = state["iteration"] + 1
        if authorization.task_spec_version != task.version:
            return {
                "status": "FAILED",
                "current_node": "decide",
                "stop_reason": "AUTHORIZATION_SCOPE_MISMATCH",
                "limitations": ["授权绑定的 TaskSpec 版本与运行输入不一致"],
            }
        if task.readiness != "READY":
            return {
                "status": "WAITING_CLARIFICATION",
                "current_node": "decide",
                "stop_reason": "TASK_SPEC_NEEDS_CLARIFICATION",
                "pending_instruction_version": task.version,
                "limitations": list(task.unresolved_fields),
            }

        existing_action = self.ledger.action_for_plan(state["task_id"], plan_version)
        if existing_action is not None:
            return self._active_from_record(existing_action)
        if plan_version > authorization.limits.max_decisions:
            budget_ref = self._budget_ref(state, authorization)
            return self._limited(state, "DECISION_LIMIT_REACHED", budget_ref)

        hypotheses = self._read_hypotheses(state)
        observations = self._read_observations(state)
        required = [
            item.question_id for item in task.questions if item.priority == "REQUIRED"
        ]
        coverage = _coverage(required, observations)
        executable = self.executor.executable_tools | {"ask_user"}
        catalog = [
            item
            for item in self.registry.catalog()
            if item["name"] in executable and item["available"]
        ]
        payload = {
            "task_spec": task.model_dump(mode="json"),
            "hypotheses": [item.model_dump(mode="json") for item in hypotheses],
            "observation_index": [
                ObservationIndexItem(
                    observation_id=item.observation_id,
                    action_id=item.action_id,
                    status=item.status,
                    summary=item.summary,
                    artifact_refs=item.artifact_refs,
                    novelty_keys=item.novelty_keys,
                ).model_dump(mode="json")
                for item in observations[-50:]
            ],
            "coverage": coverage.model_dump(mode="json"),
            "available_references": sorted(state["artifact_refs"]),
            "available_tools": catalog,
            "budget": self.ledger.budget_snapshot(state["task_id"], authorization),
            "evidence_context": coordinator_evidence_context(
                self.artifacts, set(state["artifact_refs"]), task
            ),
            "policy_rejections": state["policy_rejections"][-10:],
            "no_progress_locked": state["no_progress_count"]
            >= authorization.limits.no_progress_limit,
        }

        operation_id = f"decision:{plan_version}"
        budget_record = self.ledger.budget_record(state["task_id"], operation_id)
        draft: DecisionDraft | None = None
        decision_ref: str | None = None
        if budget_record is not None and budget_record.status == "SETTLED":
            if budget_record.result_ref is None:
                budget_ref = self._budget_ref(state, authorization)
                return self._limited(state, "MODEL_RESULT_NOT_DURABLE", budget_ref)
            decision_ref = budget_record.result_ref
            draft = DecisionDraft.model_validate(self.artifacts.read_json(decision_ref))
        elif budget_record is not None and budget_record.status in {
            "DISPATCHED",
            "UNCERTAIN",
        }:
            if budget_record.status == "DISPATCHED":
                self.ledger.mark_budget_uncertain(state["task_id"], operation_id)
            budget_ref = self._budget_ref(state, authorization)
            return self._limited(state, "MODEL_REQUEST_RESULT_UNCERTAIN", budget_ref)
        else:
            if budget_record is not None and (
                budget_record.requested_external != model_external
                or budget_record.requested_tokens != model_tokens
            ):
                return self._limited(
                    state,
                    "MODEL_REQUEST_PROFILE_CHANGED",
                    self._budget_ref(state, authorization),
                )
            try:
                self.ledger.reserve_budget(
                    task_id=state["task_id"],
                    operation_id=operation_id,
                    phase="MODEL",
                    external=model_external,
                    tokens=model_tokens,
                    authorization=authorization,
                )
                self.ledger.mark_budget_dispatched(state["task_id"], operation_id)
                model_started = perf_counter()
                result = self.model.generate(
                    output_schema=DecisionDraft,
                    purpose="coordinate_investigation",
                    prompt_version=PROMPT_VERSION,
                    system_prompt=self.system_prompt,
                    payload=payload,
                    max_output_tokens=authorization.limits.decision_max_output_tokens,
                )
                draft = result.output
                decision_ref = self.artifacts.write_json(
                    f"artifacts/agent_decision_v{plan_version}.json",
                    draft.model_dump(mode="json"),
                )
                tokens = (
                    result.input_tokens + result.output_tokens
                    if result.accounting_complete
                    and result.input_tokens is not None
                    and result.output_tokens is not None
                    else None
                )
                self.ledger.settle_budget(
                    state["task_id"],
                    operation_id,
                    actual_external=external_usage(result),
                    actual_tokens=tokens,
                    result_ref=decision_ref,
                    active_seconds=perf_counter() - model_started,
                )
            except DurableBudgetExhausted as exc:
                budget_ref = self._budget_ref(state, authorization)
                return self._limited(state, str(exc), budget_ref)
            except StructuredModelError as exc:
                self.ledger.settle_budget(
                    state["task_id"],
                    operation_id,
                    actual_external=exc.external_requests
                    if exc.external_requests is not None
                    else exc.attempts or None,
                    actual_tokens=0 if exc.external_requests == 0 else None,
                    active_seconds=perf_counter() - model_started,
                )
                if self.fallback is not None:
                    try:
                        draft = DecisionDraft.model_validate(
                            self.fallback(payload, exc)
                        )
                    except Exception:  # noqa: BLE001 - injected fallback boundary
                        draft = None
                if draft is None:
                    budget_ref = self._budget_ref(state, authorization)
                    return self._limited(state, "MODEL_UNAVAILABLE", budget_ref)
                decision_ref = self.artifacts.write_json(
                    f"artifacts/agent_decision_v{plan_version}.json",
                    draft.model_dump(mode="json"),
                )
                self.ledger.attach_budget_result(
                    state["task_id"], operation_id, decision_ref
                )

        assert draft is not None and decision_ref is not None
        emit(
            "PLAN_CHANGED",
            status="ACCEPTED",
            plan_version=plan_version,
            task_spec_version=task.version,
        )
        budget_ref = self._budget_ref(state, authorization)
        refs = [*state["artifact_refs"], decision_ref, budget_ref]
        try:
            proposal_ref = store_proposals(
                self.artifacts,
                draft.claim_proposals,
                references=set(state["artifact_refs"]),
                task=task,
                version=plan_version,
            )
        except ValueError:
            return {
                **self._limited(state, "INVALID_CLAIM_PROPOSAL", budget_ref),
                "artifact_refs": refs,
            }
        if proposal_ref:
            refs.append(proposal_ref)

        if draft.decision == "FINISH":
            coverage.unresolved_conflict_ids = current_conflicts(
                self.artifacts, set(refs), task, coverage.unresolved_conflict_ids
            )
            coverage.review_required = bool(coverage.unresolved_conflict_ids)
            try:
                semantic_answered, semantic_gaps = assess_questions(
                    self.artifacts,
                    draft.question_assessments,
                    references=set(refs),
                    task=task,
                )
            except ValueError:
                semantic_answered, semantic_gaps = [], required
            answered = set(coverage.answered_question_ids) | set(semantic_answered)
            coverage = _coverage(required, observations).model_copy(
                update={
                    "answered_question_ids": sorted(answered),
                    "gap_question_ids": sorted(
                        (set(coverage.gap_question_ids) - answered)
                        | set(draft.gap_question_ids)
                        | set(semantic_gaps)
                    ),
                    "unresolved_conflict_ids": sorted(
                        set(coverage.unresolved_conflict_ids) | set(draft.conflict_ids)
                    ),
                    "review_required": coverage.review_required
                    or draft.review_required,
                }
            )
            assessment_ref = self.artifacts.write_json(
                f"artifacts/agent_question_assessment_v{plan_version}.json",
                {
                    "schema_version": "question_assessment_v2_p22",
                    "accepted_answered_question_ids": semantic_answered,
                    "unresolved_question_ids": semantic_gaps,
                    "assessments": [
                        item.model_dump(mode="json")
                        for item in draft.question_assessments
                    ],
                },
            )
            refs.append(assessment_ref)
            coverage_ref = self._write_next(
                "agent_coverage",
                state.get("coverage_ref"),
                coverage.model_dump(mode="json"),
            )
            completed = draft.finish_reason == "ANSWERED" and coverage.complete
            reason = (
                "ANSWERED"
                if completed
                else "FINISH_GATE_REJECTED"
                if draft.finish_reason == "ANSWERED"
                else draft.finish_reason or "NEEDS_REVIEW"
            )
            return {
                "status": "COMPLETED" if completed else "LIMITED",
                "current_node": "decide",
                "iteration": plan_version,
                "stop_reason": reason,
                "coverage_ref": coverage_ref,
                "budget_ledger_ref": budget_ref,
                "artifact_refs": [*refs, coverage_ref],
                "limitations": draft.limitations,
            }

        hypothesis_ids = {item.hypothesis_id for item in hypotheses}
        if not set(draft.hypothesis_ids) <= hypothesis_ids:
            rejection = {
                "code": "UNKNOWN_HYPOTHESIS",
                "message": "unknown hypothesis id",
            }
            return {
                "status": "RUNNING",
                "current_node": "decide",
                "iteration": plan_version,
                "budget_ledger_ref": budget_ref,
                "artifact_refs": refs,
                "policy_rejections": [*state["policy_rejections"], rejection],
            }
        if not set(draft.evidence_refs) <= set(state["artifact_refs"]):
            rejection = {"code": "UNKNOWN_EVIDENCE", "message": "unknown evidence ref"}
            return {
                "status": "RUNNING",
                "current_node": "decide",
                "iteration": plan_version,
                "budget_ledger_ref": budget_ref,
                "artifact_refs": refs,
                "policy_rejections": [*state["policy_rejections"], rejection],
            }
        try:
            authorized = self.policy.authorize(
                tool=draft.tool or "",
                arguments=draft.arguments,
                task_spec=task,
                executable_tools=executable,
                available_refs=set(state["artifact_refs"]),
                prior_signatures=self.ledger.signatures(state["task_id"]),
                no_progress_locked=state["no_progress_count"]
                >= authorization.limits.no_progress_limit,
            )
        except PolicyViolation as exc:
            emit("ACTION_STATE", status="REJECTED", error_code=exc.code)
            return {
                "status": "RUNNING",
                "current_node": "decide",
                "iteration": plan_version,
                "budget_ledger_ref": budget_ref,
                "artifact_refs": refs,
                "policy_rejections": [
                    *state["policy_rejections"],
                    {"code": exc.code, "message": str(exc)[:200]},
                ],
            }

        digest = hashlib.sha256(
            f"{state['task_id']}:{plan_version}:{authorized.signature}".encode()
        ).hexdigest()[:16]
        action_id = f"action-{plan_version}-{digest}"
        action = Action(
            action_id=action_id,
            task_spec_version=task.version,
            plan_version=plan_version,
            tool=draft.tool,
            arguments=authorized.arguments.model_dump(mode="json"),
            hypothesis_ids=draft.hypothesis_ids,
            evidence_refs=draft.evidence_refs,
            expected_observation=draft.expected_observation,
            reason_summary=draft.reason_summary,
            budget_ref=f"artifacts/agent_action_budget_v{plan_version}.json",
        )
        action_ref = self.artifacts.write_json(
            self._action_ref(plan_version), action.model_dump(mode="json")
        )
        requested_external = (
            0
            if action.tool == "ask_user"
            else self.executor.reservation_for(action.tool, authorization.limits)
        )
        self.ledger.reserve_action(
            task_id=state["task_id"],
            action=action,
            signature=authorized.signature,
            decision_ref=decision_ref,
            requested_external=requested_external,
        )
        emit("ACTION_STATE", status="ACCEPTED", tool=action.tool, action_id=action_id)
        if self.fault_hook is not None:
            self.fault_hook("after_action_reserved", action)
        return {
            "status": "RUNNING",
            "current_node": "decide",
            "iteration": plan_version - 1,
            "active_action_id": action_id,
            "active_action_ref": action_ref,
            "active_decision_ref": decision_ref,
            "budget_ledger_ref": budget_ref,
            "artifact_refs": [*refs, action_ref],
        }

    def _replay_observation(self, state: AgenticState, record: ExecutionRecord) -> dict:
        assert record.observation_ref is not None
        observation = Observation.model_validate(
            self.artifacts.read_json(record.observation_ref)
        )
        try:
            task = self._read_task(state)
            validate_evidence_artifacts(
                self.artifacts,
                observation.artifact_refs,
                as_of=task.as_of,
                subject_id=task.subject_id,
            )
        except (OSError, ValueError, TypeError):
            return self._limited(
                state, "EVIDENCE_ARTIFACT_INVALID", state["budget_ledger_ref"]
            )
        return self._project_observation(state, record, observation, replayed=True)

    def _project_observation(
        self,
        state: AgenticState,
        record: ExecutionRecord,
        observation: Observation,
        *,
        replayed: bool,
    ) -> dict:
        payload = self.artifacts.read_json(state["observation_index_ref"])
        items = list(payload["items"])
        if not any(item["action_id"] == observation.action_id for item in items):
            items.append(
                {
                    "observation_id": observation.observation_id,
                    "action_id": observation.action_id,
                    "observation_ref": record.observation_ref,
                }
            )
        index_ref = self._write_next(
            "agent_observation_index",
            state["observation_index_ref"],
            {"items": items},
        )
        hypotheses = self._read_hypotheses(state)
        draft = DecisionDraft.model_validate(
            self.artifacts.read_json(record.decision_ref)
        )
        available_refs = set(state["artifact_refs"]) | set(observation.artifact_refs)
        updated: list[HypothesisState] = []
        updates = {item.hypothesis_id: item for item in draft.hypothesis_updates}
        for item in hypotheses:
            change = updates.get(item.hypothesis_id)
            if change is not None and set(change.evidence_refs) <= available_refs:
                item = item.model_copy(
                    update={
                        "status": change.status,
                        "evidence_refs": sorted(
                            set(item.evidence_refs) | set(change.evidence_refs)
                        ),
                    }
                )
            updated.append(item)
        hypotheses_ref = self._write_next(
            "agent_hypotheses",
            state["hypotheses_ref"],
            {"items": [item.model_dump(mode="json") for item in updated]},
        )
        observations = self._read_observations(
            {**state, "observation_index_ref": index_ref}
        )
        required = [
            item.question_id
            for item in self._read_task(state).questions
            if item.priority == "REQUIRED"
        ]
        coverage_ref = self._write_next(
            "agent_coverage",
            state.get("coverage_ref"),
            _coverage(required, observations).model_dump(mode="json"),
        )
        prior_novelty = {
            key
            for item in observations
            if item.action_id != observation.action_id
            for key in item.novelty_keys
        }
        no_progress = (
            state["no_progress_count"] + 1
            if observation.status == "NO_RESULT"
            or not set(observation.novelty_keys) - prior_novelty
            else 0
        )
        status = (
            "WAITING_CLARIFICATION" if record.action.tool == "ask_user" else "RUNNING"
        )
        emit(
            "ACTION_STATE",
            status="REUSED" if replayed else observation.status,
            tool=record.action.tool,
            action_id=record.action.action_id,
        )
        return {
            "status": status,
            "current_node": "execute",
            "iteration": record.action.plan_version,
            "active_action_id": None,
            "active_action_ref": None,
            "active_decision_ref": None,
            "observation_index_ref": index_ref,
            "hypotheses_ref": hypotheses_ref,
            "coverage_ref": coverage_ref,
            "no_progress_count": no_progress,
            "stop_reason": "USER_INPUT_REQUIRED"
            if status == "WAITING_CLARIFICATION"
            else None,
            "pending_instruction_version": (
                record.action.task_spec_version
                if status == "WAITING_CLARIFICATION"
                else None
            ),
            "artifact_refs": list(
                dict.fromkeys(
                    [
                        *state["artifact_refs"],
                        record.observation_ref,
                        *observation.artifact_refs,
                        index_ref,
                        hypotheses_ref,
                        coverage_ref,
                    ]
                )
            ),
        }

    def execute(self, state: AgenticState) -> dict:
        require_logging()
        action_id = state.get("active_action_id")
        if not action_id:
            return {
                "status": "FAILED",
                "current_node": "execute",
                "stop_reason": "ACTIVE_ACTION_MISSING",
            }
        record = self.ledger.get_action(state["task_id"], action_id)
        authorization = self._read_authorization(state)
        action = record.action
        with log_context(action_id=action.action_id):
            if record.status == "RESULT_STORED":
                budget_ref = self._budget_ref(state, authorization)
                return self._replay_observation(
                    {
                        **state,
                        "budget_ledger_ref": budget_ref,
                        "artifact_refs": [*state["artifact_refs"], budget_ref],
                    },
                    record,
                )
            if record.status in {"DISPATCHED", "UNCERTAIN"}:
                if record.status == "DISPATCHED":
                    record = self.ledger.mark_uncertain(state["task_id"], action_id)
                    budget = self.ledger.budget_record(
                        state["task_id"], record.budget_operation_id
                    )
                    if budget is not None and budget.status == "DISPATCHED":
                        self.ledger.mark_budget_uncertain(
                            state["task_id"], record.budget_operation_id
                        )
                budget_ref = self._budget_ref(state, authorization)
                return self._limited(state, "REQUEST_RESULT_UNCERTAIN", budget_ref)
            if record.status == "FAILED":
                budget_ref = self._budget_ref(state, authorization)
                return self._limited(
                    state, record.error_code or "ACTION_FAILED", budget_ref
                )

            is_shadow = state["execution_mode"] == "shadow"
            if is_shadow:
                emit("ACTION_STATE", status="SKIPPED", tool=action.tool)
                outcome = ExecutionOutcome(
                    status="UNAVAILABLE",
                    summary="shadow 模式仅记录已通过策略校验的计划，不执行模型选择的工具。",
                    error_code="SHADOW_ACTION_NOT_EXECUTED",
                    actual_external_requests=0,
                )
            elif action.tool == "ask_user":
                ask = self.registry.validate(action.tool, action.arguments)
                assert isinstance(ask, AskUserArgs)
                outcome = ExecutionOutcome(
                    status="MISSING_DATA",
                    summary=ask.question,
                    gap_question_ids=[
                        item.question_id
                        for item in self._read_task(state).questions
                        if item.priority == "REQUIRED"
                    ],
                    error_code="USER_INPUT_REQUIRED",
                    actual_external_requests=0,
                )
            else:
                if (
                    self.executor.reservation_for(action.tool, authorization.limits)
                    > record.requested_external
                ):
                    self.ledger.mark_failed(
                        state["task_id"], action_id, "MODEL_REQUEST_PROFILE_CHANGED"
                    )
                    return self._limited(
                        state,
                        "MODEL_REQUEST_PROFILE_CHANGED",
                        self._budget_ref(state, authorization),
                    )
                try:
                    self.ledger.reserve_budget(
                        task_id=state["task_id"],
                        operation_id=record.budget_operation_id,
                        phase="TOOL",
                        external=record.requested_external,
                        tokens=self.executor.token_reservation_for(
                            action.tool, authorization.limits
                        ),
                        authorization=authorization,
                    )
                except DurableBudgetExhausted as exc:
                    self.ledger.mark_failed(state["task_id"], action_id, str(exc))
                    budget_ref = self._budget_ref(state, authorization)
                    return self._limited(state, str(exc), budget_ref)
                self.ledger.mark_budget_dispatched(
                    state["task_id"], record.budget_operation_id
                )
                self.ledger.mark_dispatched(state["task_id"], action_id)
                emit("ACTION_STATE", status="STARTED", tool=action.tool)
                arguments = self.registry.validate(action.tool, action.arguments)
                tool_started = perf_counter()
                emit("TOOL_START", status="STARTED", tool=action.tool)
                outcome = self.executor.execute(
                    action.tool,
                    arguments,
                    context=ExecutionContext(
                        artifacts=self.artifacts,
                        task_spec=self._read_task(state),
                        limits=authorization.limits,
                        available_refs=set(state["artifact_refs"]),
                    ),
                )
                emit(
                    "TOOL_END",
                    status=outcome.status,
                    tool=action.tool,
                    duration_ms=round((perf_counter() - tool_started) * 1000),
                    error_code=outcome.error_code,
                )
                if self.fault_hook is not None:
                    self.fault_hook("after_request", action)

            if outcome.payload:
                payload_ref = self.artifacts.write_json(
                    f"artifacts/agent_tool_result_v{action.plan_version}.json",
                    outcome.payload,
                )
                outcome.artifact_refs.append(payload_ref)
            if outcome.evidence_bundle is not None:
                task = self._read_task(state)
                outcome.artifact_refs.extend(
                    write_evidence_artifacts(
                        self.artifacts,
                        outcome.evidence_bundle,
                        action.plan_version,
                        as_of=task.as_of,
                        subject_id=task.subject_id,
                    )
                )
            observation = Observation(
                observation_id=f"observation-{action.plan_version}-{action.action_id[-8:]}",
                action_id=action.action_id,
                status=outcome.status,
                summary=outcome.summary,
                artifact_refs=outcome.artifact_refs,
                novelty_keys=outcome.novelty_keys,
                answered_question_ids=outcome.answered_question_ids,
                gap_question_ids=outcome.gap_question_ids,
                conflict_ids=outcome.conflict_ids,
                error_code=outcome.error_code,
            )
            observation_ref = self.artifacts.write_json(
                f"artifacts/agent_observation_v{action.plan_version}.json",
                observation.model_dump(mode="json"),
            )
            observation.artifact_refs.append(observation_ref)
            result_fingerprint = _fingerprint(observation.model_dump(mode="json"))
            record = self.ledger.store_result(
                state["task_id"],
                action_id,
                observation_ref=observation_ref,
                result_fingerprint=result_fingerprint,
            )
            if action.tool != "ask_user" and not is_shadow:
                self.ledger.settle_budget(
                    state["task_id"],
                    record.budget_operation_id,
                    actual_external=outcome.actual_external_requests,
                    actual_tokens=outcome.actual_tokens,
                    result_ref=observation_ref,
                    active_seconds=perf_counter() - tool_started,
                )
            budget_ref = self._budget_ref(state, authorization)
            budget_artifact = self.artifacts.write_json(
                action.budget_ref
                or f"artifacts/agent_action_budget_v{action.plan_version}.json",
                self.ledger.budget_snapshot(state["task_id"], authorization),
            )
            state = {
                **state,
                "budget_ledger_ref": budget_ref,
                "artifact_refs": [*state["artifact_refs"], budget_ref, budget_artifact],
            }
            if self.fault_hook is not None:
                self.fault_hook("after_result_stored", action)
            projected = self._project_observation(
                state, record, observation, replayed=False
            )
            if is_shadow:
                projected.update(
                    status="COMPLETED",
                    stop_reason="SHADOW_PLAN_RECORDED",
                    pending_instruction_version=None,
                )
            return projected


def _route_after_decision(state: AgenticState) -> str:
    if state["status"] in {
        "COMPLETED",
        "LIMITED",
        "FAILED",
        "WAITING_CLARIFICATION",
        "PAUSED_LOGGING",
    }:
        return END
    return "execute" if state.get("active_action_id") else "decide"


def _route_after_execution(state: AgenticState) -> str:
    if state["status"] in {
        "COMPLETED",
        "LIMITED",
        "FAILED",
        "WAITING_CLARIFICATION",
        "PAUSED_LOGGING",
    }:
        return END
    return "decide"


def build_agentic_workflow(
    *,
    run_dir: Path,
    ledger: ActionLedger,
    model: StructuredModel,
    executor: ActionExecutor,
    checkpointer: Any | None = None,
    registry: ActionRegistry | None = None,
    fallback: DecisionFallback | None = None,
    fault_hook: FaultHook | None = None,
) -> CompiledStateGraph:
    runtime = AgenticGraphRuntime(
        run_dir=run_dir,
        ledger=ledger,
        model=model,
        executor=executor,
        registry=registry,
        fallback=fallback,
        fault_hook=fault_hook,
    )
    builder = StateGraph(AgenticState)
    builder.add_node("decide", _observed_node("agentic_decide", runtime.decide))
    builder.add_node("execute", _observed_node("agentic_execute", runtime.execute))
    builder.add_edge(START, "decide")
    builder.add_conditional_edges("decide", _route_after_decision)
    builder.add_conditional_edges("execute", _route_after_execution)
    return builder.compile(checkpointer=checkpointer)
