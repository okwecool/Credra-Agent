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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
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


@pytest.mark.parametrize(
    "scope,expected,comparison",
    [
        ("2023至2025年", [(2023, 1, 12), (2024, 1, 12), (2025, 1, 12)], []),
        ("2024—2025年", [(2024, 1, 12), (2025, 1, 12)], []),
        ("2025年，以2024年作比较", [(2025, 1, 12)], [2024]),
        ("2025年，比较基期为2024年", [(2025, 1, 12)], [2024]),
        ("2025年，对比2024年", [(2025, 1, 12)], [2024]),
        ("2025年上半年", [(2025, 1, 6)], []),
        ("2025年第一季度", [(2025, 1, 3)], []),
        ("2025年第4季度", [(2025, 10, 12)], []),
    ],
)
def test_explicit_scope_excludes_cutoff_year_and_preserves_period_roles(
    tmp_path, scope, expected, comparison
):
    result = parse(
        tmp_path,
        thread="periods",
        message="periods-1",
        text=f"调查case_byd_cash_quality_v2的{scope}现金质量，资料截止2026-04-02",
    )
    spec = result.task_spec
    assert spec.case_id == "case_byd_cash_quality_v2"
    assert spec.as_of == date(2026, 4, 2)
    assert [
        (p.start.year, p.start.month, p.end.month) for p in spec.periods
    ] == expected
    assert [p.start.year for p in spec.comparison_periods] == comparison
    assert all(
        p.end.day == (31 if p.end.month in {3, 12} else 30) for p in spec.periods
    )
    assert spec.readiness == "READY"


def test_llm_period_draft_is_normalized_once_and_amendment_keeps_case_scope(tmp_path):
    from credra_agent.intent.models import Period, TaskSpec

    model = DraftModel(
        IntentDraft(operation="start", subject_hint="比亚迪", years=[2024, 2025, 2026])
    )
    initial = parse(
        tmp_path,
        thread="roles",
        message="roles-1",
        text="调查case_byd_cash_quality_v2 2025年，以2024年作为比较，截止2026年4月2日",
        model=model,
    )
    assert [p.start.year for p in initial.task_spec.periods] == [2025]
    assert [p.start.year for p in initial.task_spec.comparison_periods] == [2024]
    assert initial.task_spec.readiness == "READY"
    amended = parse(
        tmp_path, thread="roles", message="roles-2", text="不要媒体，只核对现金质量"
    )
    assert amended.task_spec.case_id == initial.task_spec.case_id
    assert amended.task_spec.periods == initial.task_spec.periods
    assert amended.task_spec.comparison_periods == initial.task_spec.comparison_periods
    cross = parse(
        tmp_path,
        thread="cross",
        message="cross-1",
        text="调查上汽现金质量",
        model=DraftModel(
            IntentDraft(
                operation="start",
                subject_hint="上汽",
                periods=[Period(start=date(2024, 7, 1), end=date(2025, 6, 30))],
            )
        ),
    )
    assert [
        (p.start.isoformat(), p.end.isoformat()) for p in cross.task_spec.periods
    ] == [("2024-07-01", "2024-12-31"), ("2025-01-01", "2025-06-30")]
    changed = parse(tmp_path, thread="cross", message="cross-2", text="不要媒体")
    assert changed.task_spec.periods == cross.task_spec.periods
    legacy = initial.task_spec.model_dump(mode="json")
    legacy.pop("comparison_periods")
    assert TaskSpec.model_validate(legacy).comparison_periods == []


def test_invalid_cutoff_or_future_partial_scope_requires_clarification(tmp_path):
    invalid = parse(
        tmp_path,
        thread="invalid-date",
        message="invalid-date-1",
        text="调查上汽2025年现金质量，截至2026-02-30",
    )
    assert "as_of" in invalid.task_spec.unresolved_fields
    future = parse(
        tmp_path,
        thread="future-period",
        message="future-period-1",
        text="调查上汽2026年上半年现金质量，截止2026-04-02",
    )
    assert "period_after_as_of" in future.task_spec.unresolved_fields
    assert future.task_spec.readiness == "NEEDS_CLARIFICATION"
