"""Persist one investigation command, acknowledge it, run the shared Runtime off-chat."""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from app.cases import validate_case
from app.runtime.tasks import (
    get_task_status,
    graph_config,
    open_checkpointer,
    resume_agentic_task,
)
from credra_agent.entry.store import EntryConflict, EntryStore, now
from credra_agent.execution.ledger import ActionLedger
from credra_agent.execution.model_budget import model_reservation
from credra_agent.intent.catalog import load_subject_catalog, match_subjects
from credra_agent.intent.models import IntentResult
from credra_agent.intent.parser import build_task_spec
from credra_agent.intent.store import IntentStore
from credra_agent.observability.collector import LoggingUnavailable
from credra_agent.observability.events import log_context
from credra_agent.observability.runtime import (
    config_from_settings,
    current,
    emit,
    require_logging,
    service_session,
)
from credra_agent.runtime.service import execute_intent
from credra_agent.runtime.ui_policy import load_ui_policy, model_profile
from credra_agent.runtime.ui_service import (
    _validate_dependencies,
    build_investigation_model,
    build_ui_executor,
)
from credra_agent.runtime.ui_store import UIRequestBlocked, UITaskStore, task_lock

_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="credra-investigation")
_futures = {}
_worker_lock = threading.RLock()


def command_is_live(database_path, command_id):
    with _worker_lock:
        future = _futures.get((str(database_path.resolve()), command_id))
        return future is not None and not future.done()


def wait_for_command(database_path, command_id, *, timeout=30):
    """Local integration/test wait; dialogue never waits for a whole investigation."""
    with _worker_lock:
        future = _futures.get((str(database_path.resolve()), command_id))
    if future:
        future.result(timeout=timeout)
    return EntryStore(database_path).command(command_id)


class InvestigationDelegation:
    def __init__(
        self,
        *,
        settings,
        query,
        conversation_id=None,
        workspace_ref=None,
        message_id=None,
        text="",
        as_of=None,
        policy=None,
        model_factory=None,
        executor_factory=None,
    ):
        self.settings = settings
        self.query = query
        self.store = EntryStore(settings.checkpoint_db_path)
        self.conversation_id = conversation_id
        self.workspace_ref = workspace_ref
        self.message_id = message_id
        self.text = text
        self.as_of = as_of
        self.policy = policy
        self.model_factory = model_factory or build_investigation_model
        self.executor_factory = executor_factory or build_ui_executor

    def unavailable_reason(self, tool, permissions):
        if not self.policy or tool not in self.policy.allowed_control_tools:
            return "会话策略未允许此调查控制"
        if tool not in permissions.allowed_control_tools:
            return "当前轮次未允许此调查控制；选择已有任务不授权新建调查"
        if not permissions.logging_healthy:
            return "日志不可用"
        try:
            _validate_dependencies(self.settings)
            if tool == "prepare_investigation":
                load_ui_policy(self.settings)
        except ValueError:
            return "调查配置或批准的任务策略尚未就绪"
        return None

    def selected_executor(self, task_id):
        if not task_id:
            return None
        try:
            view = self.query.get(task_id)
            frozen = UITaskStore(self.settings.checkpoint_db_path).policy(task_id)
            if not frozen or not view.task_spec or view.task_spec.readiness != "READY":
                return None
            if frozen[2] != model_profile(self.settings):
                return None
            model = self.model_factory(self.settings, frozen[0])
            return self.executor_factory(self.settings, view.task_spec, model)
        except (ValueError, UIRequestBlocked):
            return None

    def execute(self, tool, args, *, call_id):
        from credra_agent.entry.models import EntryToolResult

        command_id = "entry-command:" + hashlib.sha256(call_id.encode()).hexdigest()
        existing = self.store.command(command_id)
        payload = {
            "arguments": args.model_dump(mode="json"),
            "text": self.text,
            "as_of": self.as_of.isoformat(),
            "message_id": self.message_id,
        }
        if existing:
            if (
                existing["conversation_id"] != self.conversation_id
                or existing["tool"] != tool
                or existing["payload_json"]
                != json.dumps(payload, ensure_ascii=False, sort_keys=True)
            ):
                raise EntryConflict("ENTRY_COMMAND_ID_CONFLICT")
            if existing["status"] != "RESERVED":
                if existing["status"] == "QUEUED":
                    self.schedule(command_id)
                return self._receipt(existing, call_id)
        task_id = (
            existing["task_id"]
            if existing
            else (
                "agentic-entry-" + hashlib.sha256(command_id.encode()).hexdigest()[:24]
                if tool == "prepare_investigation"
                else args.task_id
            )
        )
        try:
            with task_lock(self.settings.checkpoint_db_path, task_id):
                require_logging()
                if tool == "prepare_investigation":
                    frozen = UITaskStore(self.settings.checkpoint_db_path).policy(
                        task_id
                    )
                    task_policy = frozen[0] if frozen else load_ui_policy(self.settings)
                    previous = None
                    view = None
                else:
                    view = self.query.get(task_id)
                    previous = view.task_spec
                    frozen = UITaskStore(self.settings.checkpoint_db_path).policy(
                        task_id
                    )
                    if not frozen:
                        raise EntryConflict("ENTRY_ORIGINAL_TASK_POLICY_REQUIRED")
                    task_policy = frozen[0]
                    if tool == "resume_investigation":
                        if args.expected_state_ref != view.summary.state_ref:
                            raise EntryConflict("ENTRY_TASK_STATE_CHANGED")
                        if view.summary.status == "WAITING_CLARIFICATION":
                            raise EntryConflict("ENTRY_TASK_NEEDS_CLARIFICATION")
                        if (
                            self._has_graph(task_id)
                            and view.summary.graph_version != "agentic_v2"
                        ):
                            raise EntryConflict(
                                "ENTRY_LEGACY_RESUME_REQUIRES_ORIGINAL_RUNTIME"
                            )
                        if self._has_graph(task_id) and not self._has_next(task_id):
                            self.store.select(
                                self.conversation_id, self.workspace_ref, task_id
                            )
                            return EntryToolResult(
                                call_id=call_id,
                                tool=tool,
                                status="SUCCESS",
                                data={
                                    "task_id": task_id,
                                    "status": view.summary.status,
                                    "terminal": True,
                                    "budget": view.budget,
                                    "state_ref": view.summary.state_ref,
                                },
                                observed_at=now(),
                                fact_refs=[view.summary.state_ref],
                                state_ref=view.summary.state_ref,
                            )
                        if previous is None or previous.readiness != "READY":
                            raise EntryConflict("ENTRY_TASK_NEEDS_CLARIFICATION")
                    else:
                        if (
                            previous is None
                            or args.expected_spec_version != previous.version
                        ):
                            raise EntryConflict("ENTRY_TASK_SPEC_VERSION_CHANGED")
                        if (
                            self._has_graph(task_id)
                            and view.summary.status != "WAITING_CLARIFICATION"
                        ):
                            raise EntryConflict("ENTRY_RUNNING_TASK_EDIT_UNAVAILABLE")
                if frozen and frozen[2] != model_profile(self.settings):
                    raise EntryConflict("ENTRY_ORIGINAL_TASK_CONFIGURATION_CHANGED")
                _validate_dependencies(self.settings)
                pending = self.store.latest_command(task_id)
                if (
                    pending
                    and pending["command_id"] != command_id
                    and pending["status"] in {"RESERVED", "QUEUED", "DISPATCHED"}
                ):
                    if pending["status"] == "DISPATCHED" and not command_is_live(
                        self.settings.checkpoint_db_path, pending["command_id"]
                    ):
                        self.store.set_command(
                            pending["command_id"],
                            "UNCERTAIN",
                            expected="DISPATCHED",
                            result={
                                "outcome": "UNKNOWN",
                                "limitations": ["原命令结果不确定，未重发。"],
                            },
                        )
                    else:
                        raise EntryConflict("ENTRY_TASK_COMMAND_BUSY")
                row = self.store.reserve_command(
                    command_id=command_id,
                    conversation_id=self.conversation_id,
                    message_id=self.message_id,
                    task_id=task_id,
                    tool=tool,
                    payload=payload,
                )
                source_id = "entry-intent:" + command_id
                intent = IntentStore(self.settings.checkpoint_db_path).find_message(
                    source_id
                )
                if intent is None:
                    if tool == "resume_investigation":
                        intent = IntentResult(
                            source_message_id=source_id,
                            thread_id=task_id,
                            operation="resume",
                            bound_task_spec_version=previous.version,
                            parser_mode="LLM",
                            warnings=["ENTRY_SEPARATE_BUDGET_V1"],
                        )
                    else:
                        spec = self._build(args, task_policy, previous, source_id)
                        intent = IntentResult(
                            source_message_id=source_id,
                            thread_id=task_id,
                            operation=spec.operation,
                            task_spec=spec,
                            bound_task_spec_version=spec.version,
                            parser_mode="LLM",
                            warnings=["ENTRY_SEPARATE_BUDGET_V1"],
                            rejected_instructions=args.draft.rejected_instructions,
                        )
                    if frozen is None:
                        authorization = task_policy.authorization(
                            task_id, intent.bound_task_spec_version
                        )
                        UITaskStore(self.settings.checkpoint_db_path).bind_policy(
                            task_id,
                            task_policy,
                            authorization,
                            model_profile(self.settings),
                        )
                    intent = IntentStore(self.settings.checkpoint_db_path).save(intent)
                frozen = UITaskStore(self.settings.checkpoint_db_path).policy(task_id)
                if not frozen:
                    raise EntryConflict("ENTRY_ORIGINAL_TASK_POLICY_REQUIRED")
                if intent.task_spec and intent.task_spec.readiness != "READY":
                    self.store.set_command(
                        command_id,
                        "ACKED",
                        intent=intent,
                        result={
                            "outcome": "WAITING_CLARIFICATION",
                            "limitations": intent.task_spec.unresolved_fields,
                        },
                    )
                else:
                    model = self.model_factory(self.settings, task_policy)
                    if model is None:
                        raise EntryConflict("ENTRY_COORDINATOR_REQUIRED")
                    self._require_task_budget(task_id, frozen[1], model)
                    self.store.enqueue_command(command_id, intent, self.workspace_ref)
                    emit(
                        "ACTION_STATE",
                        status="ACCEPTED",
                        action_id=command_id,
                        thread_id=task_id,
                    )
                self.store.select(self.conversation_id, self.workspace_ref, task_id)
                row = self.store.command(command_id)
            if row["status"] == "QUEUED":
                self.schedule(command_id)
            return self._receipt(row, call_id)
        except UIRequestBlocked as exc:
            raise EntryConflict("ENTRY_TASK_BUSY_OR_POLICY_INVALID") from exc
        except (ValueError, EntryConflict):
            row = self.store.command(command_id)
            if row and row["status"] == "RESERVED":
                self.store.set_command(
                    command_id,
                    "ACKED",
                    result={
                        "outcome": "REJECTED",
                        "limitations": ["ENTRY_TASK_SCOPE_OR_BUDGET_REJECTED"],
                    },
                )
            raise

    def _build(self, args, task_policy, previous, source_id):
        draft = args.draft
        expected_operation = "amend" if previous else "start"
        if draft.operation != expected_operation:
            raise EntryConflict("ENTRY_DRAFT_OPERATION_INVALID")
        catalog = load_subject_catalog(self.settings.data_dir)
        matches = match_subjects(draft.subject_hint or self.text, catalog)
        allowed = set(task_policy.allowed_subject_ids) & set(
            self.policy.allowed_subject_ids
        )
        if any(item.subject_id not in allowed for item in matches):
            raise EntryConflict("ENTRY_SUBJECT_NOT_AUTHORIZED")
        if (
            previous
            and previous.subject_id
            and draft.subject_hint
            and (len(matches) != 1 or matches[0].subject_id != previous.subject_id)
        ):
            raise EntryConflict("ENTRY_CLARIFICATION_SUBJECT_CHANGE")
        if previous:
            old = previous.source_policy
            if (
                old.allowed is not None
                and draft.allowed_sources is not None
                and not set(draft.allowed_sources) <= set(old.allowed)
            ):
                raise EntryConflict("ENTRY_CLARIFICATION_SOURCE_EXPANSION")
            if (
                set(draft.preferred_sources) & set(old.denied)
                or old.allowed is not None
                and not set(draft.preferred_sources) <= set(old.allowed)
            ):
                raise EntryConflict("ENTRY_CLARIFICATION_SOURCE_EXPANSION")
        case_id = args.case_id or (previous.case_id if previous else None)
        if case_id:
            chosen = next((item for item in catalog if item.case_id == case_id), None)
            if not chosen or chosen.subject_id not in allowed:
                raise EntryConflict("ENTRY_CASE_NOT_AUTHORIZED")
            if matches and chosen.subject_id not in {
                item.subject_id for item in matches
            }:
                raise EntryConflict("ENTRY_CASE_SUBJECT_MISMATCH")
            if previous and previous.case_id and case_id != previous.case_id:
                raise EntryConflict("ENTRY_CLARIFICATION_CASE_CHANGE")
            catalog = [chosen]
        spec = build_task_spec(
            self.text,
            message_id=source_id,
            draft=draft,
            catalog=catalog,
            anchor_date=self.as_of,
            current=previous,
        )
        if spec.subject_id and spec.subject_id not in allowed:
            raise EntryConflict("ENTRY_SUBJECT_NOT_AUTHORIZED")
        if previous and previous.subject_id and spec.subject_id != previous.subject_id:
            raise EntryConflict("ENTRY_CLARIFICATION_SUBJECT_CHANGE")
        if (
            not case_id
            and spec.subject_id
            and sum(item.subject_id == spec.subject_id for item in catalog) > 1
        ):
            unresolved = [*spec.unresolved_fields, "case_id"]
            updates = {
                "case_id": None,
                "unresolved_fields": list(dict.fromkeys(unresolved)),
                "readiness": "NEEDS_CLARIFICATION",
            }
            if not draft.years and not (previous and previous.periods):
                updates["periods"] = []
            spec = type(spec).model_validate({**spec.model_dump(), **updates})
        if (
            spec.readiness == "READY"
            and not validate_case(spec.case_id, self.settings.data_dir).valid
        ):
            raise EntryConflict("ENTRY_CASE_PREFLIGHT_FAILED")
        return spec

    def _receipt(self, row, call_id):
        from credra_agent.entry.models import EntryToolResult

        command = self.store.public_command(row)
        outcome = command.get("outcome")
        status = (
            "WAITING_CLARIFICATION"
            if outcome == "WAITING_CLARIFICATION"
            else "REJECTED"
            if outcome == "REJECTED"
            else "UNKNOWN"
            if row["status"] == "UNCERTAIN"
            else "SUCCESS"
            if row["status"] == "ACKED" and outcome == "TASK_STATE"
            else "ACCEPTED"
        )
        try:
            view = self.query.get(row["task_id"])
        except EntryConflict:
            if status != "REJECTED":
                raise
            return EntryToolResult(
                call_id=call_id,
                tool=row["tool"],
                status=status,
                observed_at=now(),
                data={"command": command},
                limitations=command.get("limitations", []),
            )
        if view.summary.status == "WAITING_CLARIFICATION":
            status = "WAITING_CLARIFICATION"
        return EntryToolResult(
            call_id=call_id,
            tool=row["tool"],
            status=status,
            observed_at=now(),
            data={
                "task_id": row["task_id"],
                "command": command,
                "summary": view.summary.model_dump(mode="json"),
                "unresolved_fields": view.unresolved_fields,
                "pending_clarification": view.pending_clarification,
                "budget": view.budget,
            },
            fact_refs=[view.summary.state_ref, row["command_id"]],
            state_ref=view.summary.state_ref,
            limitations=command.get("limitations", []),
        )

    def _has_graph(self, task_id):
        with open_checkpointer(self.settings.checkpoint_db_path) as saver:
            return saver.get_tuple(graph_config(task_id)) is not None

    def _has_next(self, task_id):
        return bool(
            get_task_status(thread_id=task_id, settings=self.settings).get("next")
        )

    def _require_task_budget(self, task_id, authorization, model):
        budget = ActionLedger(self.settings.checkpoint_db_path).budget_snapshot(
            task_id, authorization
        )
        requests, tokens = model_reservation(model, authorization.limits)
        if (
            budget["remaining_external"] < requests
            or budget["remaining_tokens"] < tokens
            or budget["remaining_active_seconds"] <= 0
        ):
            raise EntryConflict("ENTRY_TASK_BUDGET_EXHAUSTED")

    def schedule(self, command_id):
        key = (str(self.settings.checkpoint_db_path.resolve()), command_id)
        with _worker_lock:
            for finished in [key for key, future in _futures.items() if future.done()]:
                del _futures[finished]
            if key in _futures and not _futures[key].done():
                return
            context = copy_context()
            log = current()
            if log:
                log.retain_background()

            def work():
                try:
                    return context.run(self._work, command_id)
                finally:
                    if log:
                        log.release_background()

            try:
                _futures[key] = _pool.submit(work)
            except RuntimeError:
                if log:
                    log.release_background()
                # Durable QUEUED is recoverable; never claim Graph dispatch here.
                emit(
                    "DEGRADED",
                    status="DEGRADED",
                    action_id=command_id,
                    error_code="ENTRY_WORKER_UNAVAILABLE",
                )

    def recover(self):
        cursor = 0
        while True:
            batch = self.store.queued_commands(after_rowid=cursor, status="DISPATCHED")
            for row in batch:
                try:
                    with task_lock(self.settings.checkpoint_db_path, row["task_id"]):
                        self.store.set_command(
                            row["command_id"],
                            "UNCERTAIN",
                            expected="DISPATCHED",
                            result={
                                "outcome": "UNKNOWN",
                                "limitations": [
                                    "ENTRY_PROCESS_INTERRUPTED_AFTER_DISPATCH"
                                ],
                            },
                        )
                except UIRequestBlocked:
                    pass  # An OS-owned live worker still owns this task.
            if len(batch) < 100:
                break
            cursor = batch[-1]["queue_seq"]
        cursor = 0
        while True:
            batch = self.store.queued_commands(after_rowid=cursor)
            for row in batch:
                self.schedule(row["command_id"])
            if len(batch) < 100:
                break
            cursor = batch[-1]["queue_seq"]

    def _work(self, command_id):
        row = self.store.command(command_id)
        if row is None or row["status"] != "QUEUED":
            return
        task_id = row["task_id"]
        try:
            with (
                service_session("entry_worker", config_from_settings(self.settings)),
                task_lock(self.settings.checkpoint_db_path, task_id),
                log_context(
                    conversation_id=row["conversation_id"],
                    message_id=row["message_id"],
                    thread_id=task_id,
                    action_id=command_id,
                    node="entry_worker",
                ),
            ):
                frozen = UITaskStore(self.settings.checkpoint_db_path).policy(task_id)
                if not frozen or frozen[2] != model_profile(self.settings):
                    raise EntryConflict("ENTRY_ORIGINAL_TASK_CONFIGURATION_CHANGED")
                intent = IntentResult.model_validate_json(row["intent_json"])
                from credra_agent.entry.query import TaskQueryService

                task_query = TaskQueryService(
                    self.settings, allowed_subject_ids=frozen[0].allowed_subject_ids
                )
                live_view = task_query.get(task_id)
                payload = json.loads(row["payload_json"])
                if (
                    intent.operation == "resume"
                    and live_view.summary.state_ref
                    != payload["arguments"]["expected_state_ref"]
                ):
                    raise EntryConflict("ENTRY_TASK_STATE_CHANGED_BEFORE_DISPATCH")
                if intent.operation == "start" and self._has_graph(task_id):
                    raise EntryConflict("ENTRY_TASK_ALREADY_STARTED")
                if (
                    intent.operation == "amend"
                    and self._has_graph(task_id)
                    and (
                        live_view.summary.spec_version
                        != payload["arguments"]["expected_spec_version"]
                        or live_view.summary.status != "WAITING_CLARIFICATION"
                    )
                ):
                    raise EntryConflict("ENTRY_TASK_STATE_CHANGED_BEFORE_DISPATCH")
                require_logging()
                model = self.model_factory(self.settings, frozen[0])
                if model is None:
                    raise EntryConflict("ENTRY_COORDINATOR_REQUIRED")
                self._require_task_budget(task_id, frozen[1], model)
                if not self.store.set_command(
                    command_id, "DISPATCHED", expected="QUEUED"
                ):
                    return
                emit("ACTION_STATE", status="STARTED", action_id=command_id)
                authorization = frozen[1].model_copy(
                    update={"task_spec_version": intent.bound_task_spec_version}
                )
                if intent.operation == "resume":
                    spec = live_view.task_spec
                    if not self._has_graph(task_id):
                        original = IntentStore(
                            self.settings.checkpoint_db_path
                        ).find_message(spec.source_message_id)
                        if original is None:
                            raise EntryConflict("ENTRY_READY_DRAFT_MISSING")
                        executed = execute_intent(
                            intent=original,
                            settings=self.settings,
                            authorization=authorization,
                            coordinator_model=model,
                            executor_factory=lambda spec: self.executor_factory(
                                self.settings, spec, model
                            ),
                        )
                        result = executed.model_dump(mode="json")
                    else:
                        task = resume_agentic_task(
                            thread_id=task_id,
                            settings=self.settings,
                            model=model,
                            executor=self.executor_factory(self.settings, spec, model),
                        )
                        result = {
                            "outcome": "TASK_STATE",
                            "task": task,
                            "limitations": [],
                        }
                else:
                    executed = execute_intent(
                        intent=intent,
                        settings=self.settings,
                        authorization=authorization,
                        coordinator_model=model,
                        executor_factory=lambda spec: self.executor_factory(
                            self.settings, spec, model
                        ),
                    )
                    result = executed.model_dump(mode="json")
                self.store.set_command(
                    command_id, "ACKED", result=result, expected="DISPATCHED"
                )
                emit("ACTION_STATE", status="SUCCESS", action_id=command_id)
        except (LoggingUnavailable, UIRequestBlocked):
            # Nothing dispatched before the guard: keep durable queue for recovery.
            if self.store.command(command_id)["status"] == "DISPATCHED":
                self.store.set_command(
                    command_id,
                    "UNCERTAIN",
                    expected="DISPATCHED",
                    result={
                        "outcome": "UNKNOWN",
                        "limitations": ["ENTRY_INTERRUPTED_AFTER_DISPATCH"],
                    },
                )
            emit("INTERRUPT", status="PAUSED", action_id=command_id, thread_id=task_id)
        except Exception as exc:  # noqa: BLE001 - preserve uncertain worker delivery, never resend
            state = self.store.command(command_id)
            dispatched = state and state["status"] == "DISPATCHED"
            self.store.set_command(
                command_id,
                "UNCERTAIN" if dispatched else "ACKED",
                result={
                    "outcome": "UNKNOWN" if dispatched else "REJECTED",
                    "limitations": [
                        "ENTRY_WORKER_RESULT_UNCERTAIN"
                        if dispatched
                        else "ENTRY_WORKER_CONFIGURATION_OR_BUDGET_REJECTED"
                    ],
                },
            )
            emit(
                "ACTION_STATE",
                status="UNKNOWN" if dispatched else "REJECTED",
                action_id=command_id,
                thread_id=task_id,
                conversation_id=row["conversation_id"],
                message_id=row["message_id"],
                node="entry_worker",
                exception=exc,
                level="WARNING",
            )


def recover_entry_commands(settings):
    from credra_agent.entry.query import TaskQueryService

    # Recovery uses frozen task policies; it neither creates conversation authority
    # nor charges an entry model. Only never-dispatched QUEUED commands run.
    controller = InvestigationDelegation(
        settings=settings, query=TaskQueryService(settings, allowed_subject_ids=[])
    )
    controller.recover()
