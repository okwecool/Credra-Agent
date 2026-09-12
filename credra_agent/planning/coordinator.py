"""One-decision-at-a-time LLM Coordinator with deterministic safety gates."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.llm.gateway import StructuredModel, StructuredModelError
from app.tools.artifacts import ArtifactStore
from credra_agent.evidence.artifacts import write_evidence_artifacts
from credra_agent.execution.budget import (
    BudgetAuditRecord,
    BudgetError,
    BudgetExhausted,
    BudgetLedger,
    BudgetUnavailable,
)
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionContext,
    ExecutionOutcome,
)
from credra_agent.execution.model_budget import external_usage, model_reservation
from credra_agent.execution.models import Action, AskUserArgs
from credra_agent.execution.policy import ActionPolicy, PolicyViolation
from credra_agent.execution.registry import ActionRegistry, default_registry
from credra_agent.intent.models import TaskSpec
from credra_agent.observability.events import log_context
from credra_agent.observability.runtime import emit, require_logging
from credra_agent.planning.evidence_context import (
    assess_questions,
    coordinator_evidence_context,
    current_conflicts,
    store_proposals,
)
from credra_agent.planning.models import (
    CoordinatorLimits,
    CoordinatorResult,
    Coverage,
    DecisionDraft,
    HypothesisState,
    Observation,
    ObservationIndexItem,
    RunAuthorization,
)

PROMPT_VERSION = "coordinator-v2-p22"
PROMPT_PATH = Path(__file__).parents[1] / "prompts" / "coordinator.md"
DecisionFallback = Callable[
    [dict[str, Any], BaseException], DecisionDraft | dict[str, Any]
]


class Coordinator:
    """Runs an injected structured model and executor under explicit limits."""

    def __init__(
        self,
        *,
        model: StructuredModel,
        executor: ActionExecutor,
        authorization: RunAuthorization,
        run_dir: Path,
        registry: ActionRegistry | None = None,
        fallback: DecisionFallback | None = None,
    ) -> None:
        self.model = model
        self.executor = executor
        self.authorization = authorization
        self.budget = BudgetLedger(
            approval=authorization.approval,
            external_limit=authorization.external_request_limit,
            token_limit=authorization.token_limit,
            active_seconds_limit=authorization.active_seconds_limit,
        )
        self.limits: CoordinatorLimits = authorization.limits
        self.model_external_reservation, self.model_token_reservation = (
            model_reservation(model, self.limits)
        )
        self.registry = registry or default_registry()
        self.fallback = fallback
        self.policy = ActionPolicy(self.registry)
        self.artifacts = ArtifactStore(run_dir)
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8")

    def run(
        self,
        task_spec: TaskSpec,
        *,
        initial_refs: set[str] | None = None,
        hypotheses: list[HypothesisState] | None = None,
    ) -> CoordinatorResult:
        required = [
            item.question_id
            for item in task_spec.questions
            if item.priority == "REQUIRED"
        ]
        hypotheses = hypotheses or [
            HypothesisState(
                hypothesis_id=f"hyp-{item.question_id}",
                question_id=item.question_id,
                statement=f"需要核验：{item.text}",
            )
            for item in task_spec.questions
        ]
        hypothesis_map = {item.hypothesis_id: item for item in hypotheses}
        available_refs = set(initial_refs or set())
        signatures: set[str] = set()
        actions: list[dict] = []
        observations: list[Observation] = []
        observation_index: list[ObservationIndexItem] = []
        authorization_ref = self._write_versioned(
            "agent_run_authorization",
            None,
            self.authorization.model_dump(mode="json"),
        )
        artifact_refs: list[str] = [authorization_ref]
        rejections: list[dict[str, str]] = []
        no_progress_count = 0

        if self.authorization.task_spec_version != task_spec.version:
            coverage = Coverage(
                required_question_ids=required,
                gap_question_ids=required,
                review_required=True,
            )
            return self._result(
                status="FAILED",
                stop_reason="AUTHORIZATION_SCOPE_MISMATCH",
                actions=actions,
                observations=observations,
                hypotheses=list(hypothesis_map.values()),
                coverage=coverage,
                artifact_refs=artifact_refs,
                limitations=["授权绑定的 TaskSpec 版本与运行输入不一致"],
            )

        if task_spec.readiness != "READY":
            coverage = Coverage(
                required_question_ids=required,
                gap_question_ids=required,
                review_required=True,
            )
            return self._result(
                status="WAITING_CLARIFICATION",
                stop_reason="TASK_SPEC_NEEDS_CLARIFICATION",
                actions=actions,
                observations=observations,
                hypotheses=list(hypothesis_map.values()),
                coverage=coverage,
                artifact_refs=artifact_refs,
                limitations=list(task_spec.unresolved_fields),
            )

        executable = self.executor.executable_tools | {"ask_user"}
        catalog = [
            item
            for item in self.registry.catalog()
            if item["name"] in executable and item["available"]
        ]

        for decision_number in range(1, self.limits.max_decisions + 1):
            coverage = self._coverage(required, observations)
            payload = {
                "task_spec": task_spec.model_dump(mode="json"),
                "hypotheses": [
                    item.model_dump(mode="json") for item in hypothesis_map.values()
                ],
                "observation_index": [
                    item.model_dump(mode="json") for item in observation_index[-50:]
                ],
                "coverage": coverage.model_dump(mode="json"),
                "available_references": sorted(available_refs),
                "available_tools": catalog,
                "budget": self.budget.snapshot().model_dump(mode="json"),
                "evidence_context": coordinator_evidence_context(
                    self.artifacts, available_refs, task_spec
                ),
                "policy_rejections": rejections[-10:],
                "no_progress_locked": no_progress_count
                >= self.limits.no_progress_limit,
            }
            draft: DecisionDraft | None = None
            model_before = self.budget.snapshot()
            try:
                require_logging()
                reservation = self.budget.reserve(
                    external=self.model_external_reservation,
                    tokens=self.model_token_reservation,
                )
            except (BudgetUnavailable, BudgetExhausted) as exc:
                artifact_refs.append(
                    self._write_budget_audit(
                        phase="MODEL",
                        decision_number=decision_number,
                        action_id=None,
                        status="REJECTED",
                        requested_external=self.model_external_reservation,
                        requested_tokens=self.model_token_reservation,
                        actual_external=None,
                        actual_tokens=None,
                        error_code=str(exc),
                        before=model_before,
                        after=self.budget.snapshot(),
                    )
                )
                return self._limited(
                    str(exc),
                    required,
                    actions,
                    observations,
                    hypothesis_map,
                    artifact_refs,
                )

            try:
                model_result = self.model.generate(
                    output_schema=DecisionDraft,
                    purpose="coordinate_investigation",
                    prompt_version=PROMPT_VERSION,
                    system_prompt=self.system_prompt,
                    payload=payload,
                    max_output_tokens=self.limits.decision_max_output_tokens,
                )
            except StructuredModelError as exc:
                actual_attempts = (
                    exc.external_requests
                    if exc.external_requests is not None
                    else exc.attempts or None
                )
                failed_tokens = 0 if exc.external_requests == 0 else None
                self.budget.settle(
                    reservation,
                    actual_external=actual_attempts,
                    actual_tokens=failed_tokens,
                )
                artifact_refs.append(
                    self._write_budget_audit(
                        phase="MODEL",
                        decision_number=decision_number,
                        action_id=None,
                        status="SETTLED_UNCERTAIN",
                        requested_external=self.model_external_reservation,
                        requested_tokens=self.model_token_reservation,
                        actual_external=actual_attempts,
                        actual_tokens=failed_tokens,
                        error_code=exc.code,
                        before=model_before,
                        after=self.budget.snapshot(),
                    )
                )
                emit("DEGRADED", status="DEGRADED", error_code=exc.code)
                draft = self._fallback(payload, exc)
                if draft is None:
                    return self._limited(
                        "MODEL_UNAVAILABLE",
                        required,
                        actions,
                        observations,
                        hypothesis_map,
                        artifact_refs,
                    )
            except Exception as exc:  # noqa: BLE001 - model boundary degrades safely
                self.budget.settle(
                    reservation, actual_external=None, actual_tokens=None
                )
                artifact_refs.append(
                    self._write_budget_audit(
                        phase="MODEL",
                        decision_number=decision_number,
                        action_id=None,
                        status="SETTLED_UNCERTAIN",
                        requested_external=self.model_external_reservation,
                        requested_tokens=self.model_token_reservation,
                        actual_external=None,
                        actual_tokens=None,
                        error_code=type(exc).__name__,
                        before=model_before,
                        after=self.budget.snapshot(),
                    )
                )
                emit("DEGRADED", status="DEGRADED", error_code=type(exc).__name__)
                draft = self._fallback(payload, exc)
                if draft is None:
                    return self._limited(
                        "MODEL_UNAVAILABLE",
                        required,
                        actions,
                        observations,
                        hypothesis_map,
                        artifact_refs,
                    )
            else:
                reported_token_usage = (
                    model_result.input_tokens + model_result.output_tokens
                    if model_result.input_tokens is not None
                    and model_result.output_tokens is not None
                    else None
                )
                accounting_complete = (
                    model_result.accounting_complete
                    and reported_token_usage is not None
                )
                actual_external = external_usage(model_result)
                actual_tokens = reported_token_usage if accounting_complete else None
                self.budget.settle(
                    reservation,
                    actual_external=actual_external,
                    actual_tokens=actual_tokens,
                )
                artifact_refs.append(
                    self._write_budget_audit(
                        phase="MODEL",
                        decision_number=decision_number,
                        action_id=None,
                        status=(
                            "SETTLED" if accounting_complete else "SETTLED_UNCERTAIN"
                        ),
                        requested_external=self.model_external_reservation,
                        requested_tokens=self.model_token_reservation,
                        actual_external=actual_external,
                        actual_tokens=actual_tokens,
                        error_code=None,
                        before=model_before,
                        after=self.budget.snapshot(),
                    )
                )
                draft = model_result.output

            assert draft is not None
            emit(
                "PLAN_CHANGED",
                status="ACCEPTED",
                plan_version=decision_number,
                task_spec_version=task_spec.version,
            )
            try:
                proposal_ref = store_proposals(
                    self.artifacts,
                    draft.claim_proposals,
                    references=available_refs,
                    task=task_spec,
                    version=decision_number,
                )
            except ValueError:
                return self._limited(
                    "INVALID_CLAIM_PROPOSAL",
                    required,
                    actions,
                    observations,
                    hypothesis_map,
                    artifact_refs,
                )
            if proposal_ref:
                artifact_refs.append(proposal_ref)
                available_refs.add(proposal_ref)

            if draft.decision == "FINISH":
                coverage = self._coverage(required, observations, draft=draft)
                coverage.unresolved_conflict_ids = current_conflicts(
                    self.artifacts,
                    available_refs,
                    task_spec,
                    coverage.unresolved_conflict_ids,
                )
                coverage.review_required = (
                    bool(coverage.unresolved_conflict_ids) or draft.review_required
                )
                try:
                    answered, gaps = assess_questions(
                        self.artifacts,
                        draft.question_assessments,
                        references=available_refs,
                        task=task_spec,
                    )
                except ValueError:
                    answered, gaps = [], required
                coverage.answered_question_ids = sorted(
                    set(coverage.answered_question_ids) | set(answered)
                )
                coverage.gap_question_ids = sorted(
                    (set(coverage.gap_question_ids) - set(answered))
                    | set(gaps)
                    | set(draft.gap_question_ids)
                )
                artifact_refs.append(
                    self.artifacts.write_json(
                        f"artifacts/agent_question_assessment_v{decision_number}.json",
                        {
                            "schema_version": "question_assessment_v2_p22",
                            "accepted_answered_question_ids": answered,
                            "unresolved_question_ids": gaps,
                            "assessments": [
                                item.model_dump(mode="json")
                                for item in draft.question_assessments
                            ],
                        },
                    )
                )
                finish_ref = self._write_versioned(
                    "agent_coverage", None, coverage.model_dump(mode="json")
                )
                artifact_refs.append(finish_ref)
                if draft.finish_reason == "ANSWERED" and coverage.complete:
                    emit("STOP", status="SUCCESS", finish_reason="stop")
                    return self._result(
                        status="COMPLETED",
                        stop_reason="ANSWERED",
                        actions=actions,
                        observations=observations,
                        hypotheses=list(hypothesis_map.values()),
                        coverage=coverage,
                        artifact_refs=artifact_refs,
                        limitations=draft.limitations,
                    )
                reason = (
                    "FINISH_GATE_REJECTED"
                    if draft.finish_reason == "ANSWERED"
                    else draft.finish_reason or "NEEDS_REVIEW"
                )
                return self._result(
                    status="LIMITED",
                    stop_reason=reason,
                    actions=actions,
                    observations=observations,
                    hypotheses=list(hypothesis_map.values()),
                    coverage=coverage,
                    artifact_refs=artifact_refs,
                    limitations=draft.limitations,
                )

            if not set(draft.hypothesis_ids) <= set(hypothesis_map):
                rejections.append(
                    {"code": "UNKNOWN_HYPOTHESIS", "message": "unknown hypothesis id"}
                )
                continue
            if not set(draft.evidence_refs) <= available_refs:
                rejections.append(
                    {"code": "UNKNOWN_EVIDENCE", "message": "unknown evidence ref"}
                )
                continue
            try:
                authorized = self.policy.authorize(
                    tool=draft.tool or "",
                    arguments=draft.arguments,
                    task_spec=task_spec,
                    executable_tools=executable,
                    available_refs=available_refs,
                    prior_signatures=signatures,
                    no_progress_locked=no_progress_count
                    >= self.limits.no_progress_limit,
                )
            except PolicyViolation as exc:
                rejections.append({"code": exc.code, "message": str(exc)[:200]})
                emit("ACTION_STATE", status="REJECTED", error_code=exc.code)
                continue

            action_id = f"action-{decision_number}-{uuid4().hex[:12]}"
            action_budget_ref = f"artifacts/agent_action_budget_v{decision_number}.json"
            action = Action(
                action_id=action_id,
                task_spec_version=task_spec.version,
                plan_version=decision_number,
                tool=draft.tool,
                arguments=authorized.arguments.model_dump(mode="json"),
                hypothesis_ids=draft.hypothesis_ids,
                evidence_refs=draft.evidence_refs,
                expected_observation=draft.expected_observation,
                reason_summary=draft.reason_summary,
                budget_ref=action_budget_ref,
            )
            action_ref = self._write_versioned(
                "agent_action",
                None,
                action.model_dump(mode="json"),
                suffix=decision_number,
            )
            artifact_refs.append(action_ref)
            actions.append(action.model_dump(mode="json"))
            signatures.add(authorized.signature)
            available_refs.add(action_ref)

            with log_context(action_id=action_id):
                emit("ACTION_STATE", status="ACCEPTED", tool=action.tool)
                tool_before = self.budget.snapshot()
                if action.tool == "ask_user":
                    emit("ACTION_STATE", status="STARTED", tool=action.tool)
                    ask = authorized.arguments
                    assert isinstance(ask, AskUserArgs)
                    outcome = ExecutionOutcome(
                        status="MISSING_DATA",
                        summary=ask.question,
                        gap_question_ids=required,
                        error_code="USER_INPUT_REQUIRED",
                        actual_external_requests=0,
                    )
                    artifact_refs.append(
                        self._write_budget_audit(
                            phase="TOOL",
                            decision_number=decision_number,
                            action_id=action_id,
                            status="SETTLED",
                            requested_external=0,
                            requested_tokens=0,
                            actual_external=0,
                            actual_tokens=0,
                            error_code=None,
                            before=tool_before,
                            after=self.budget.snapshot(),
                        )
                    )
                else:
                    requested_external = self.executor.reservation_for(
                        action.tool, self.limits
                    )
                    requested_tokens = self.executor.token_reservation_for(
                        action.tool, self.limits
                    )
                    try:
                        require_logging()
                        tool_reservation = self.budget.reserve(
                            external=requested_external, tokens=requested_tokens
                        )
                    except (BudgetUnavailable, BudgetExhausted) as exc:
                        artifact_refs.append(
                            self._write_budget_audit(
                                phase="TOOL",
                                decision_number=decision_number,
                                action_id=action_id,
                                status="REJECTED",
                                requested_external=requested_external,
                                requested_tokens=requested_tokens,
                                actual_external=None,
                                actual_tokens=None,
                                error_code=str(exc),
                                before=tool_before,
                                after=self.budget.snapshot(),
                            )
                        )
                        return self._limited(
                            str(exc),
                            required,
                            actions,
                            observations,
                            hypothesis_map,
                            artifact_refs,
                        )
                    emit("ACTION_STATE", status="STARTED", tool=action.tool)
                    outcome = self.executor.execute(
                        action.tool,
                        authorized.arguments,
                        context=ExecutionContext(
                            artifacts=self.artifacts,
                            task_spec=task_spec,
                            limits=self.limits,
                            available_refs=available_refs,
                        ),
                    )
                    self.budget.settle(
                        tool_reservation,
                        actual_external=outcome.actual_external_requests,
                        actual_tokens=outcome.actual_tokens,
                    )
                    artifact_refs.append(
                        self._write_budget_audit(
                            phase="TOOL",
                            decision_number=decision_number,
                            action_id=action_id,
                            status=(
                                "SETTLED"
                                if outcome.actual_external_requests is not None
                                and outcome.actual_tokens is not None
                                else "SETTLED_UNCERTAIN"
                            ),
                            requested_external=requested_external,
                            requested_tokens=requested_tokens,
                            actual_external=outcome.actual_external_requests,
                            actual_tokens=outcome.actual_tokens,
                            error_code=outcome.error_code,
                            before=tool_before,
                            after=self.budget.snapshot(),
                        )
                    )

                observation = self._record_observation(
                    action_id,
                    decision_number,
                    outcome,
                    artifact_refs,
                    available_refs,
                    task_spec,
                )
                observations.append(observation)
                observation_index.append(
                    ObservationIndexItem(
                        observation_id=observation.observation_id,
                        action_id=observation.action_id,
                        status=observation.status,
                        summary=observation.summary,
                        artifact_refs=observation.artifact_refs,
                        novelty_keys=observation.novelty_keys,
                    )
                )
                emit("ACTION_STATE", status=observation.status, tool=action.tool)

            self._apply_hypothesis_updates(
                draft, hypothesis_map, available_refs | set(observation.artifact_refs)
            )
            prior_novelty = {
                key for item in observations[:-1] for key in item.novelty_keys
            }
            no_progress_count = (
                no_progress_count + 1
                if observation.status == "NO_RESULT"
                or not set(observation.novelty_keys) - prior_novelty
                else 0
            )
            if action.tool == "ask_user":
                return self._result(
                    status="WAITING_CLARIFICATION",
                    stop_reason="USER_INPUT_REQUIRED",
                    actions=actions,
                    observations=observations,
                    hypotheses=list(hypothesis_map.values()),
                    coverage=self._coverage(required, observations),
                    artifact_refs=artifact_refs,
                    limitations=["需要用户补充信息后继续"],
                )

        return self._limited(
            "DECISION_LIMIT_REACHED",
            required,
            actions,
            observations,
            hypothesis_map,
            artifact_refs,
        )

    def _record_observation(
        self,
        action_id: str,
        number: int,
        outcome: ExecutionOutcome,
        artifact_refs: list[str],
        available_refs: set[str],
        task_spec: TaskSpec,
    ) -> Observation:
        refs = list(outcome.artifact_refs)
        if outcome.evidence_bundle is not None:
            evidence_refs = write_evidence_artifacts(
                self.artifacts,
                outcome.evidence_bundle,
                number,
                as_of=task_spec.as_of,
                subject_id=task_spec.subject_id,
            )
            refs.extend(evidence_refs)
            artifact_refs.extend(evidence_refs)
        if outcome.payload:
            payload_ref = self._write_versioned(
                "agent_tool_result", None, outcome.payload, suffix=number
            )
            refs.append(payload_ref)
            artifact_refs.append(payload_ref)
        observation = Observation(
            observation_id=f"observation-{number}-{uuid4().hex[:12]}",
            action_id=action_id,
            status=outcome.status,
            summary=outcome.summary,
            artifact_refs=refs,
            novelty_keys=outcome.novelty_keys,
            answered_question_ids=outcome.answered_question_ids,
            gap_question_ids=outcome.gap_question_ids,
            conflict_ids=outcome.conflict_ids,
            error_code=outcome.error_code,
        )
        observation_ref = self._write_versioned(
            "agent_observation",
            None,
            observation.model_dump(mode="json"),
            suffix=number,
        )
        observation.artifact_refs.append(observation_ref)
        artifact_refs.append(observation_ref)
        available_refs.update(observation.artifact_refs)
        return observation

    @staticmethod
    def _coverage(
        required: list[str],
        observations: list[Observation],
        *,
        draft: DecisionDraft | None = None,
    ) -> Coverage:
        answered: set[str] = set()
        gaps: set[str] = set()
        conflicts: set[str] = set()
        for item in observations:
            answered.update(item.answered_question_ids)
            gaps.update(item.gap_question_ids)
            conflicts.update(item.conflict_ids)
        gaps.difference_update(answered)
        gaps.update(set(required) - answered)
        review_required = bool(conflicts)
        if draft is not None:
            gaps.update(draft.gap_question_ids)
            conflicts.update(draft.conflict_ids)
            review_required = review_required or draft.review_required
        return Coverage(
            required_question_ids=required,
            answered_question_ids=sorted(answered & set(required)),
            gap_question_ids=sorted(gaps),
            unresolved_conflict_ids=sorted(conflicts),
            review_required=review_required,
        )

    @staticmethod
    def _apply_hypothesis_updates(
        draft: DecisionDraft,
        hypothesis_map: dict[str, HypothesisState],
        available_refs: set[str],
    ) -> None:
        for update in draft.hypothesis_updates:
            current = hypothesis_map.get(update.hypothesis_id)
            if current is None or not set(update.evidence_refs) <= available_refs:
                continue
            hypothesis_map[update.hypothesis_id] = current.model_copy(
                update={
                    "status": update.status,
                    "evidence_refs": sorted(
                        set(current.evidence_refs) | set(update.evidence_refs)
                    ),
                }
            )

    def _write_versioned(
        self, base: str, current: str | None, value: dict, *, suffix: int | None = None
    ) -> str:
        reference = (
            f"artifacts/{base}_v{suffix}.json"
            if suffix is not None
            else self.artifacts.next_version_reference(base, current)
        )
        return self.artifacts.write_json(reference, value)

    def _write_budget_audit(
        self,
        *,
        phase: str,
        decision_number: int,
        action_id: str | None,
        status: str,
        requested_external: int,
        requested_tokens: int,
        actual_external: int | None,
        actual_tokens: int | None,
        error_code: str | None,
        before,
        after,
    ) -> str:
        record = BudgetAuditRecord(
            authorization_id=self.authorization.authorization_id,
            phase=phase,
            decision_number=decision_number,
            action_id=action_id,
            status=status,
            requested_external=requested_external,
            requested_tokens=requested_tokens,
            actual_external=actual_external,
            actual_tokens=actual_tokens,
            error_code=error_code,
            before=before,
            after=after,
        )
        emit(
            "BUDGET_STATE",
            phase=phase.lower(),
            status="REJECTED" if status == "REJECTED" else "SUCCESS",
            action_id=action_id,
            external_requests=actual_external
            if actual_external is not None
            else requested_external,
            token_units=actual_tokens
            if actual_tokens is not None
            else requested_tokens,
            error_code=error_code,
        )
        base = "agent_model_budget" if phase == "MODEL" else "agent_action_budget"
        return self._write_versioned(
            base,
            None,
            record.model_dump(mode="json"),
            suffix=decision_number,
        )

    def _fallback(
        self, payload: dict[str, Any], error: BaseException
    ) -> DecisionDraft | None:
        if self.fallback is None:
            return None
        try:
            draft = DecisionDraft.model_validate(self.fallback(payload, error))
        except Exception:  # noqa: BLE001 - injected adapter boundary
            emit("DEGRADED", status="DEGRADED", error_code="FALLBACK_INVALID")
            return None
        emit("DEGRADED", status="DEGRADED", error_code="MODEL_FALLBACK_USED")
        return draft

    def _limited(
        self,
        reason: str,
        required: list[str],
        actions: list[dict],
        observations: list[Observation],
        hypothesis_map: dict[str, HypothesisState],
        artifact_refs: list[str],
    ) -> CoordinatorResult:
        coverage = self._coverage(required, observations)
        coverage = coverage.model_copy(update={"review_required": True})
        coverage_ref = self._write_versioned(
            "agent_coverage", None, coverage.model_dump(mode="json")
        )
        artifact_refs.append(coverage_ref)
        emit("STOP", status="DEGRADED", error_code=reason)
        return self._result(
            status="LIMITED",
            stop_reason=reason,
            actions=actions,
            observations=observations,
            hypotheses=list(hypothesis_map.values()),
            coverage=coverage,
            artifact_refs=artifact_refs,
            limitations=[reason],
        )

    def _result(self, **kwargs: Any) -> CoordinatorResult:
        try:
            budget = self.budget.snapshot().model_dump(mode="json")
        except BudgetError:
            budget = {"approval": "UNAVAILABLE"}
        return CoordinatorResult(
            authorization_id=self.authorization.authorization_id,
            budget=budget,
            **kwargs,
        )
