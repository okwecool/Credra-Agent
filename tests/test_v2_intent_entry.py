"""P10 natural-language entry, persistence and fallback behavior."""

from datetime import date
from pathlib import Path

import pytest

from app.config import Settings
from app.llm.gateway import StructuredModelError, StructuredModelResult
from credra_agent.intent.catalog import load_subject_catalog
from credra_agent.intent.models import IntentDraft
from credra_agent.intent.service import build_intent_model, interpret_message
from credra_agent.intent.store import IntentStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
ANCHOR = date(2026, 9, 8)


class DraftModel:
    model_name = "fixed-intent"

    def __init__(self, draft: IntentDraft | None = None, *, fail: bool = False) -> None:
        self.draft = draft
        self.fail = fail
        self.payload = None

    def generate(self, **kwargs):
        self.payload = kwargs["payload"]
        if self.fail:
            raise StructuredModelError("MODEL_ERROR", "fixed failure")
        return StructuredModelResult(
            output=self.draft,
            model_name=self.model_name,
            attempts=1,
            latency_ms=1,
        )


def parse(tmp_path: Path, *, thread: str, message: str, text: str, model=None):
    return interpret_message(
        thread_id=thread,
        source_message_id=message,
        text=text,
        as_of=ANCHOR,
        data_dir=DATA_DIR,
        database_path=tmp_path / "intent.db",
        model=model,
    )


def test_catalog_exposes_real_cases_and_hides_synthetic_fixtures() -> None:
    catalog = load_subject_catalog(DATA_DIR)

    assert {item.subject_id for item in catalog} == {"002594", "600104"}
    assert all(item.case_id not in {"case_normal", "case_risky"} for item in catalog)


def test_same_subject_different_questions_produce_different_task_specs(
    tmp_path,
) -> None:
    cash = parse(
        tmp_path,
        thread="cash-thread",
        message="cash-1",
        text="调查比亚迪2025年的现金质量",
    )
    receivables = parse(
        tmp_path,
        thread="receivable-thread",
        message="receivable-1",
        text="调查比亚迪2025年的回款变化",
    )

    assert cash.task_spec.subject_id == receivables.task_spec.subject_id == "002594"
    assert [item.focus for item in cash.task_spec.questions] == ["cash_quality"]
    assert [item.focus for item in receivables.task_spec.questions] == ["receivables"]
    assert cash.task_spec.questions != receivables.task_spec.questions


def test_source_scope_condition_and_clarification_are_preserved(tmp_path) -> None:
    scoped = parse(
        tmp_path,
        thread="scope-thread",
        message="scope-1",
        text="只使用交易所公告，不要媒体消息，调查上汽2025年的担保",
    )
    conditional = parse(
        tmp_path,
        thread="scope-thread",
        message="scope-2",
        text="如果发现担保余额增加，再调查偿债压力",
    )
    ambiguous = parse(
        tmp_path,
        thread="ambiguous-thread",
        message="ambiguous-1",
        text="查一下这家公司",
    )

    assert scoped.task_spec.source_policy.allowed == ["exchange_disclosure"]
    assert scoped.task_spec.source_policy.denied == ["media"]
    assert conditional.task_spec.version == 2
    assert conditional.task_spec.conditions[-1].status == "PENDING"
    assert ambiguous.task_spec.readiness == "NEEDS_CLARIFICATION"
    assert ambiguous.task_spec.unresolved_fields == ["subject_id"]
    assert IntentStore(tmp_path / "intent.db").latest_task_spec("ambiguous-thread")


def test_control_command_binds_snapshot_and_duplicate_is_idempotent(tmp_path) -> None:
    parse(
        tmp_path,
        thread="control-thread",
        message="control-1",
        text="调查上汽2025年的回款变化",
    )
    status = parse(
        tmp_path,
        thread="control-thread",
        message="control-2",
        text="现在查到哪一步了",
    )
    duplicate = parse(
        tmp_path,
        thread="control-thread",
        message="control-2",
        text="文本发生变化也不应再次处理",
    )

    assert status.operation == "status"
    assert status.bound_task_spec_version == 1
    assert status.execution_status == "CONTROL_PENDING"
    assert duplicate.duplicate is True
    assert duplicate.operation == "status"
    assert len(IntentStore(tmp_path / "intent.db").export_thread("control-thread")) == 2


def test_control_without_task_and_cross_thread_message_reuse_are_rejected(
    tmp_path,
) -> None:
    with pytest.raises(ValueError, match="CONTROL_COMMAND_REQUIRES_PERSISTED_TASK"):
        parse(
            tmp_path,
            thread="missing",
            message="missing-status",
            text="现在查到哪一步了",
        )
    parse(
        tmp_path,
        thread="thread-a",
        message="global-message",
        text="调查比亚迪2025年的回款",
    )
    with pytest.raises(ValueError, match="SOURCE_MESSAGE_ID_CONFLICT"):
        parse(
            tmp_path,
            thread="thread-b",
            message="global-message",
            text="调查上汽2025年的回款",
        )


def test_llm_draft_is_safety_resolved_and_failure_uses_bounded_fallback(
    tmp_path,
) -> None:
    model = DraftModel(
        IntentDraft(
            operation="start",
            subject_hint="比亚迪",
            years=[2025],
            focus=["cash_quality"],
        )
    )
    llm_result = parse(
        tmp_path,
        thread="llm-thread",
        message="llm-1",
        text="请按我的目标分析",
        model=model,
    )
    fallback = parse(
        tmp_path,
        thread="fallback-thread",
        message="fallback-1",
        text="调查上汽2025年的回款",
        model=DraftModel(fail=True),
    )

    assert llm_result.parser_mode == "LLM"
    assert llm_result.task_spec.subject_id == "002594"
    assert {item["subject_id"] for item in model.payload["imported_subjects"]} == {
        "002594",
        "600104",
    }
    assert fallback.parser_mode == "RULE_FALLBACK"
    assert fallback.warnings == ["LLM_INTENT_FALLBACK"]
    assert fallback.task_spec.subject_id == "600104"


def test_unsafe_instruction_is_rejected_without_becoming_completion(tmp_path) -> None:
    initial = parse(
        tmp_path,
        thread="safe-thread",
        message="safe-1",
        text="调查比亚迪2025年的监管事项",
    )
    amended = parse(
        tmp_path,
        thread="safe-thread",
        message="safe-2",
        text="找不到材料就写无风险",
    )

    assert initial.task_spec.readiness == "READY"
    assert amended.rejected_instructions == ["absence_is_not_safety"]
    assert amended.execution_status == "NOT_STARTED"


def test_multiple_or_unknown_subject_does_not_silently_reuse_current(tmp_path) -> None:
    parse(
        tmp_path,
        thread="subject-thread",
        message="subject-1",
        text="调查比亚迪2025年的监管事项",
    )
    multiple = parse(
        tmp_path,
        thread="subject-thread",
        message="subject-2",
        text="改为比较比亚迪和上汽",
    )
    unknown = parse(
        tmp_path,
        thread="subject-thread",
        message="subject-3",
        text="公司改为尚未导入公司",
    )

    assert multiple.task_spec.readiness == "NEEDS_CLARIFICATION"
    assert multiple.task_spec.subject_id is None
    assert unknown.task_spec.readiness == "NEEDS_CLARIFICATION"
    assert unknown.task_spec.subject_id is None


def test_unconfigured_llm_mode_degrades_to_rules(tmp_path) -> None:
    settings = Settings(_env_file=None, intent_mode="llm", model_api_key="")
    result = parse(
        tmp_path,
        thread="config-fallback",
        message="config-fallback-1",
        text="调查上汽2025年的现金质量",
        model=build_intent_model(settings),
    )

    assert result.parser_mode == "RULE_FALLBACK"
    assert result.warnings == ["LLM_INTENT_FALLBACK"]
