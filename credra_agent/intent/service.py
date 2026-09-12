"""Application service joining parsing, safety resolution and persistence."""

from datetime import date
from pathlib import Path

from app.config import Settings
from app.llm.gateway import (
    OpenAICompatibleStructuredModel,
    StructuredModel,
    StructuredModelError,
)
from credra_agent.intent.catalog import load_subject_catalog
from credra_agent.intent.models import IntentResult
from credra_agent.intent.parser import build_task_spec, parse_draft
from credra_agent.intent.store import IntentStore
from credra_agent.observability.events import log_context
from credra_agent.observability.runtime import emit


class _UnavailableIntentModel:
    model_name = "unavailable"

    def __init__(self, error: StructuredModelError) -> None:
        self.error = error

    def generate(self, **_kwargs):
        raise self.error


def build_intent_model(settings: Settings) -> StructuredModel | None:
    if settings.intent_mode == "deterministic":
        return None
    try:
        return OpenAICompatibleStructuredModel(
            api_key=settings.model_api_key.get_secret_value(),
            base_url=settings.model_base_url,
            model_name=settings.intent_model
            or settings.analysis_model
            or settings.model_name,
            timeout_seconds=settings.analysis_llm_timeout_seconds,
            max_attempts=settings.analysis_llm_max_retry + 1,
            max_input_chars=settings.analysis_llm_max_input_chars,
            max_output_tokens=settings.intent_llm_max_output_tokens,
            enable_thinking=False,
        )
    except StructuredModelError as exc:
        return _UnavailableIntentModel(exc)  # type: ignore[return-value]


def interpret_message(
    *,
    thread_id: str,
    source_message_id: str,
    text: str,
    as_of: date,
    data_dir: Path,
    database_path: Path,
    model: StructuredModel | None = None,
) -> IntentResult:
    if not thread_id.strip() or not source_message_id.strip() or not text.strip():
        raise ValueError("thread_id, source_message_id and text are required")
    store = IntentStore(database_path)
    duplicate = store.find_message(source_message_id)
    if duplicate is not None:
        if duplicate.thread_id != thread_id:
            raise ValueError("SOURCE_MESSAGE_ID_CONFLICT")
        return duplicate.model_copy(update={"duplicate": True})
    current = store.latest_task_spec(thread_id)
    catalog = load_subject_catalog(data_dir)
    with log_context(thread_id=thread_id):
        emit("REQUEST_ACCEPTED", status="ACCEPTED", node="intent")
        draft, parser_mode, warnings = parse_draft(
            text,
            catalog=catalog,
            anchor_date=as_of,
            current=current,
            model=model,
        )
        if draft.operation in {"start", "amend"}:
            spec = build_task_spec(
                text,
                message_id=source_message_id,
                draft=draft,
                catalog=catalog,
                anchor_date=as_of,
                current=current,
            )
            result = IntentResult(
                source_message_id=source_message_id,
                thread_id=thread_id,
                operation=spec.operation,
                task_spec=spec,
                bound_task_spec_version=spec.version,
                parser_mode=parser_mode,
                warnings=warnings,
                rejected_instructions=draft.rejected_instructions,
            )
        else:
            if current is None:
                emit("REQUEST_REJECTED", status="REJECTED", node="intent")
                raise ValueError("CONTROL_COMMAND_REQUIRES_PERSISTED_TASK")
            result = IntentResult(
                source_message_id=source_message_id,
                thread_id=thread_id,
                operation=draft.operation,
                bound_task_spec_version=current.version,
                parser_mode=parser_mode,
                execution_status="CONTROL_PENDING",
                warnings=[*warnings, "CONTROL_REQUIRES_AGENT_RUNTIME"],
                rejected_instructions=draft.rejected_instructions,
            )
        stored = store.save(result)
        emit(
            "PLAN_CHANGED",
            status="SUCCESS",
            node="intent",
            task_spec_version=(
                stored.task_spec.version
                if stored.task_spec
                else stored.bound_task_spec_version
            ),
        )
        emit("REQUEST_END", status="SUCCESS", node="intent")
        return stored
