"""SQLite-backed start, inspect, and resume operations for Credra tasks."""

import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from app.config import Settings
from app.graph.state import initial_state
from app.graph.workflow import build_workflow
from app.llm.gateway import StructuredModel
from app.runtime.fault import ResearchFaultInjector
from app.runtime.tracing import TraceWriter
from credra_agent.observability.checkpoint import ObservedSqliteSaver
from credra_agent.observability.runtime import emit, logged_operation


def _checkpoint_values(checkpointer: SqliteSaver, thread_id: str) -> dict[str, Any]:
    checkpoint = checkpointer.get_tuple(graph_config(thread_id))
    if checkpoint is None:
        raise ValueError(f"thread does not exist: {thread_id}")
    return dict(checkpoint.checkpoint.get("channel_values", {}))


def _runtime_services(settings: Settings) -> tuple[TraceWriter, ResearchFaultInjector]:
    trace = TraceWriter(settings.trace_dir)
    fault = ResearchFaultInjector(
        settings.trace_dir / ".fault_state", settings.research_fail_first
    )
    return trace, fault


def graph_config(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id}}


def _serialize_interrupts(snapshot: Any) -> list[dict[str, Any]]:
    return [
        {"id": item.id, "value": item.value}
        for task in snapshot.tasks
        for item in task.interrupts
    ]


def snapshot_payload(snapshot: Any, thread_id: str) -> dict[str, Any]:
    values = dict(snapshot.values) if snapshot.values else {}
    payload = {
        "thread_id": thread_id,
        "exists": bool(values),
        "state": values,
        "next": list(snapshot.next),
        "interrupts": _serialize_interrupts(snapshot),
    }
    if any(
        "LoggingUnavailable('LOGGING_UNAVAILABLE_BEFORE_NODE')" in str(task.error)
        for task in snapshot.tasks
    ):
        payload["execution_blocked"] = {
            "reason": "LOGGING_UNAVAILABLE",
            "resume_safe": True,
        }
    return payload


@contextmanager
def open_checkpointer(db_path: Path) -> Iterator[SqliteSaver]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with ObservedSqliteSaver.from_conn_string(str(db_path.resolve())) as checkpointer:
        emit("CHECKPOINT_OPEN", status="SUCCESS")
        try:
            yield checkpointer
        except Exception as exc:
            emit("CHECKPOINT_ERROR", status="FAILED", exception=exc)
            raise
        finally:
            emit("CHECKPOINT_CLOSE", status="SUCCESS")


def _case_id_from_checkpoint(checkpointer: SqliteSaver, thread_id: str) -> str:
    case_id = _checkpoint_values(checkpointer, thread_id).get("case_id")
    if not isinstance(case_id, str) or not case_id:
        raise ValueError(f"thread has no persisted case_id: {thread_id}")
    return case_id


def run_id_for_thread(thread_id: str) -> str:
    """Create a stable filesystem-safe run id without trusting thread text as a path."""

    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:20]


def _run_dir_from_checkpoint(
    checkpointer: SqliteSaver, case_dir: Path, thread_id: str
) -> Path:
    checkpoint = checkpointer.get_tuple(graph_config(thread_id))
    values = checkpoint.checkpoint.get("channel_values", {}) if checkpoint else {}
    run_id = values.get("run_id")
    if isinstance(run_id, str) and run_id:
        return case_dir / "runs" / run_id
    # Explicit compatibility: checkpoints created before M2.2 keep their case-level paths.
    return case_dir


def _case_dir(settings: Settings, case_id: str) -> Path:
    case_dir = settings.data_dir / case_id
    if not (case_dir / "source").is_dir():
        raise ValueError(f"case does not exist: {case_id}")
    return case_dir


@logged_operation
def start_task(
    *,
    thread_id: str,
    case_id: str,
    settings: Settings,
    research_client: Any | None = None,
    analysis_model: StructuredModel | None = None,
) -> dict[str, Any]:
    trace, fault = _runtime_services(settings)
    trace.instant(
        task_id=thread_id,
        node="runtime",
        event_type="TASK_START",
        input_summary=f"case_id={case_id}",
    )
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        config = graph_config(thread_id)
        if checkpointer.get_tuple(config) is not None:
            raise ValueError(f"thread already exists: {thread_id}")
        case_dir = _case_dir(settings, case_id)
        run_id = run_id_for_thread(thread_id)
        graph = build_workflow(
            case_dir,
            settings,
            research_client,
            checkpointer,
            trace,
            fault,
            run_dir=case_dir / "runs" / run_id,
            analysis_model=analysis_model,
        )
        graph.invoke(initial_state(thread_id, case_id, run_id), config=config)
        payload = snapshot_payload(graph.get_state(config), thread_id)
        trace.instant(
            task_id=thread_id,
            node="runtime",
            event_type="TASK_STATE",
            output_summary=f"status={payload['state'].get('status')}",
        )
        return payload


@logged_operation
def get_task_status(
    *,
    thread_id: str,
    settings: Settings,
    research_client: Any | None = None,
    analysis_model: StructuredModel | None = None,
    agentic_model: StructuredModel | None = None,
    agentic_executor: Any | None = None,
) -> dict[str, Any]:
    trace, fault = _runtime_services(settings)
    trace.instant(
        task_id=thread_id,
        node="runtime",
        event_type="STATUS_QUERY",
    )
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        values = _checkpoint_values(checkpointer, thread_id)
        from credra_agent.graph.versioning import resolve_graph_identity

        identity = resolve_graph_identity(values)
        case_id = _case_id_from_checkpoint(checkpointer, thread_id)
        case_dir = _case_dir(settings, case_id)
        if identity.graph_version == "agentic_v2":
            from credra_agent.execution.executor import ActionExecutor
            from credra_agent.execution.ledger import ActionLedger
            from credra_agent.graph.workflow import build_agentic_workflow

            class StatusOnlyModel:
                model_name = "status-only"

                def generate(self, **kwargs):
                    raise RuntimeError("status-only graph cannot execute")

            graph = build_agentic_workflow(
                run_dir=_run_dir_from_checkpoint(checkpointer, case_dir, thread_id),
                ledger=ActionLedger(settings.checkpoint_db_path),
                model=agentic_model or StatusOnlyModel(),
                executor=agentic_executor or ActionExecutor(),
                checkpointer=checkpointer,
            )
            return snapshot_payload(graph.get_state(graph_config(thread_id)), thread_id)
        graph = build_workflow(
            case_dir,
            settings,
            research_client,
            checkpointer,
            trace,
            fault,
            run_dir=_run_dir_from_checkpoint(checkpointer, case_dir, thread_id),
            analysis_model=analysis_model,
        )
        return snapshot_payload(graph.get_state(graph_config(thread_id)), thread_id)


@logged_operation
def start_agentic_task(
    *,
    thread_id: str,
    task_spec: Any,
    authorization: Any,
    settings: Settings,
    model: StructuredModel,
    executor: Any,
    execution_mode: str = "agentic",
    fallback: Any | None = None,
    fault_hook: Any | None = None,
    initial_evidence_bundle: Any | None = None,
) -> dict[str, Any]:
    """Start agentic_v2 explicitly; baseline remains the default start entry."""

    from app.tools.artifacts import ArtifactStore
    from credra_agent.execution.ledger import ActionLedger
    from credra_agent.graph.state import initial_agentic_state
    from credra_agent.graph.workflow import build_agentic_workflow
    from credra_agent.intent.models import TaskSpec
    from credra_agent.planning.models import HypothesisState, RunAuthorization

    spec = TaskSpec.model_validate(task_spec)
    approved = RunAuthorization.model_validate(authorization)
    if execution_mode not in {"agentic", "shadow"}:
        raise ValueError("execution_mode must be agentic or shadow")
    if spec.case_id is None:
        raise ValueError("agentic task requires a bound case_id")
    case_dir = _case_dir(settings, spec.case_id)
    run_id = run_id_for_thread(thread_id)
    run_dir = case_dir / "runs" / run_id
    # Reject the common duplicate-start path before touching immutable run artifacts.
    # The second check below remains the authoritative race guard.
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        if checkpointer.get_tuple(graph_config(thread_id)) is not None:
            raise ValueError(f"thread already exists: {thread_id}")
    artifacts = ArtifactStore(run_dir)
    task_ref = artifacts.write_json("artifacts/agent_task_spec_v1.json", spec)
    authorization_ref = artifacts.write_json(
        "artifacts/agent_run_authorization_v1.json", approved
    )
    initial_evidence_refs = []
    if initial_evidence_bundle is not None:
        from credra_agent.evidence.artifacts import write_evidence_artifacts
        from credra_agent.evidence.models import EvidenceBundle

        initial_evidence_refs = write_evidence_artifacts(
            artifacts,
            EvidenceBundle.model_validate(initial_evidence_bundle),
            0,
            as_of=spec.as_of,
            subject_id=spec.subject_id,
        )
    hypotheses = [
        HypothesisState(
            hypothesis_id=f"hyp-{question.question_id}",
            question_id=question.question_id,
            statement=f"需要核验：{question.text}",
        )
        for question in spec.questions
    ]
    hypotheses_ref = artifacts.write_json(
        "artifacts/agent_hypotheses_v1.json",
        {"items": [item.model_dump(mode="json") for item in hypotheses]},
    )
    index_ref = artifacts.write_json(
        "artifacts/agent_observation_index_v1.json", {"items": []}
    )
    ledger = ActionLedger(settings.checkpoint_db_path)
    budget_ref = artifacts.write_json(
        "artifacts/agent_budget_ledger_v1.json",
        ledger.budget_snapshot(thread_id, approved),
    )
    refs = [
        task_ref,
        authorization_ref,
        hypotheses_ref,
        index_ref,
        budget_ref,
        *initial_evidence_refs,
    ]
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        config = graph_config(thread_id)
        if checkpointer.get_tuple(config) is not None:
            raise ValueError(f"thread already exists: {thread_id}")
        graph = build_agentic_workflow(
            run_dir=run_dir,
            ledger=ledger,
            model=model,
            executor=executor,
            checkpointer=checkpointer,
            fallback=fallback,
            fault_hook=fault_hook,
        )
        state = initial_agentic_state(
            task_id=thread_id,
            case_id=spec.case_id,
            run_id=run_id,
            execution_mode=execution_mode,
            task_spec_ref=task_ref,
            authorization_ref=authorization_ref,
            hypotheses_ref=hypotheses_ref,
            observation_index_ref=index_ref,
            budget_ledger_ref=budget_ref,
            artifact_refs=refs,
        )
        graph.invoke(state, config=config)
        return snapshot_payload(graph.get_state(config), thread_id)


@logged_operation
def amend_agentic_task(
    *,
    thread_id: str,
    task_spec: Any,
    authorization: Any,
    settings: Settings,
    model: StructuredModel,
    executor: Any,
    fallback: Any | None = None,
) -> dict[str, Any]:
    """Bind a new TaskSpec version to a waiting agentic task and continue it."""

    from app.tools.artifacts import ArtifactStore
    from credra_agent.execution.ledger import ActionLedger
    from credra_agent.graph.versioning import resolve_graph_identity
    from credra_agent.graph.workflow import build_agentic_workflow
    from credra_agent.intent.models import TaskSpec
    from credra_agent.planning.models import RunAuthorization

    spec = TaskSpec.model_validate(task_spec)
    approved = RunAuthorization.model_validate(authorization)
    if approved.task_spec_version != spec.version:
        raise ValueError("AUTHORIZATION_SCOPE_MISMATCH")
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        config = graph_config(thread_id)
        values = _checkpoint_values(checkpointer, thread_id)
        identity = resolve_graph_identity(values)
        if identity.graph_version != "agentic_v2":
            raise ValueError("thread is not an agentic_v2 task")
        if values.get("status") != "WAITING_CLARIFICATION":
            raise ValueError("agentic task is not waiting for clarification")
        case_id = _case_id_from_checkpoint(checkpointer, thread_id)
        if spec.case_id != case_id:
            raise ValueError("CASE_SCOPE_CHANGE_REQUIRES_NEW_TASK")
        case_dir = _case_dir(settings, case_id)
        run_dir = _run_dir_from_checkpoint(checkpointer, case_dir, thread_id)
        artifacts = ArtifactStore(run_dir)
        previous_spec = TaskSpec.model_validate(
            artifacts.read_json(values["task_spec_ref"])
        )
        if spec.version != previous_spec.version + 1:
            raise ValueError("TASK_SPEC_VERSION_CONFLICT")
        if spec.subject_id != previous_spec.subject_id:
            raise ValueError("SUBJECT_SCOPE_CHANGE_REQUIRES_NEW_TASK")
        task_ref = artifacts.write_json(
            artifacts.next_version_reference(
                "agent_task_spec", values["task_spec_ref"]
            ),
            spec,
        )
        authorization_ref = artifacts.write_json(
            artifacts.next_version_reference(
                "agent_run_authorization", values["authorization_ref"]
            ),
            approved,
        )
        graph = build_agentic_workflow(
            run_dir=run_dir,
            ledger=ActionLedger(settings.checkpoint_db_path),
            model=model,
            executor=executor,
            checkpointer=checkpointer,
            fallback=fallback,
        )
        graph.update_state(
            config,
            {
                "task_spec_ref": task_ref,
                "authorization_ref": authorization_ref,
                "status": "RUNNING",
                "stop_reason": None,
                "pending_instruction_version": None,
                "limitations": [],
                "artifact_refs": [
                    *values.get("artifact_refs", []),
                    task_ref,
                    authorization_ref,
                ],
            },
            as_node="execute",
        )
        graph.invoke(None, config=config)
        return snapshot_payload(graph.get_state(config), thread_id)


@logged_operation
def resume_agentic_task(
    *,
    thread_id: str,
    settings: Settings,
    model: StructuredModel,
    executor: Any,
    fallback: Any | None = None,
    fault_hook: Any | None = None,
) -> dict[str, Any]:
    """Resume the exact persisted agentic graph version after a node interruption."""

    from credra_agent.execution.ledger import ActionLedger
    from credra_agent.graph.versioning import resolve_graph_identity
    from credra_agent.graph.workflow import build_agentic_workflow

    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        identity = resolve_graph_identity(_checkpoint_values(checkpointer, thread_id))
        if identity.graph_version != "agentic_v2":
            raise ValueError("thread is not an agentic_v2 task")
        case_id = _case_id_from_checkpoint(checkpointer, thread_id)
        case_dir = _case_dir(settings, case_id)
        graph = build_agentic_workflow(
            run_dir=_run_dir_from_checkpoint(checkpointer, case_dir, thread_id),
            ledger=ActionLedger(settings.checkpoint_db_path),
            model=model,
            executor=executor,
            checkpointer=checkpointer,
            fallback=fallback,
            fault_hook=fault_hook,
        )
        config = graph_config(thread_id)
        snapshot = graph.get_state(config)
        if not snapshot.next:
            return snapshot_payload(snapshot, thread_id)
        graph.invoke(None, config=config)
        return snapshot_payload(graph.get_state(config), thread_id)


@logged_operation
def resume_task(
    *,
    thread_id: str,
    decision: str,
    comment: str | None,
    settings: Settings,
    research_client: Any | None = None,
    analysis_model: StructuredModel | None = None,
) -> dict[str, Any]:
    if decision not in ("approve", "research", "resume_logging"):
        raise ValueError("decision must be approve or research")
    trace, fault = _runtime_services(settings)
    trace.instant(
        task_id=thread_id,
        node="runtime",
        event_type="RESUME",
        input_summary=f"decision={decision}",
    )
    with open_checkpointer(settings.checkpoint_db_path) as checkpointer:
        case_id = _case_id_from_checkpoint(checkpointer, thread_id)
        config = graph_config(thread_id)
        case_dir = _case_dir(settings, case_id)
        graph = build_workflow(
            case_dir,
            settings,
            research_client,
            checkpointer,
            trace,
            fault,
            run_dir=_run_dir_from_checkpoint(checkpointer, case_dir, thread_id),
            analysis_model=analysis_model,
        )
        snapshot = graph.get_state(config)
        if decision == "resume_logging":
            if not snapshot_payload(snapshot, thread_id).get("execution_blocked"):
                raise ValueError("task is not paused at a safe logging boundary")
            emit("LOG_RECOVERY", status="STARTED")
            graph.invoke(None, config=config)
            return snapshot_payload(graph.get_state(config), thread_id)
        if not any(task.interrupts for task in snapshot.tasks):
            raise ValueError(f"thread is not waiting for approval: {thread_id}")
        graph.invoke(
            Command(resume={"decision": decision, "comment": comment}),
            config=config,
        )
        payload = snapshot_payload(graph.get_state(config), thread_id)
        trace.instant(
            task_id=thread_id,
            node="runtime",
            event_type="TASK_STATE",
            output_summary=f"status={payload['state'].get('status')}",
        )
        return payload
