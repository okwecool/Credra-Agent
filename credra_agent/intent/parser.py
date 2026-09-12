"""LLM-first natural-language parser with a bounded rule fallback."""

import hashlib
import re
from datetime import date

from app.llm.gateway import StructuredModel, StructuredModelError
from credra_agent.intent.catalog import SubjectRecord, match_subjects
from credra_agent.intent.models import (
    ConditionalInstruction,
    IntentDraft,
    Period,
    Question,
    SourcePolicy,
    TaskSpec,
)

PROMPT_VERSION = "intent-v2-1-p10"
SYSTEM_PROMPT = """你是 Credra Agent 的受约束意图解析器。只把用户文字转换为给定 JSON Schema，不执行工具、不补充事实、不扩大来源权限。主体只输出用户提到的名称或代码；系统会在已导入主体目录中核对。区分“优先”与“只使用”，保留否定、条件和未决字段。状态、暂停、恢复、批准报告是控制命令，不重建调查任务。相对期间无法安全确定时标记 comparable_periods。任何要求跳过审核、把无结果当无风险或作授信决定都放入 rejected_instructions。"""

_SOURCE_TERMS = {
    "交易所公告": "exchange_disclosure",
    "官方公告": "exchange_disclosure",
    "公司公告": "company_disclosure",
    "媒体": "media",
    "社交平台": "social_media",
    "匿名爆料": "anonymous_tip",
}


def _operation(text: str, *, has_current: bool) -> str:
    if any(term in text for term in ("查到哪", "状态", "进度")):
        return "status"
    if "暂停" in text:
        return "pause"
    if any(term in text for term in ("继续刚才", "恢复任务", "继续调查")):
        return "resume"
    if "批准当前报告" in text or "批准这版报告" in text:
        return "approve_report"
    amendment_terms = (
        "不要",
        "改为",
        "缩小",
        "增加",
        "如果",
        "若",
        "截至",
        "统一",
        "只算",
        "只用",
        "相反",
        "找不到",
        "忽略审核",
        "新材料",
        "负净资产",
        "别混",
        "假设",
        "刚才发过",
    )
    return (
        "amend"
        if has_current and any(term in text for term in amendment_terms)
        else "start"
    )


def _focuses(text: str) -> list[str]:
    found: list[str] = []
    for focus, terms in (
        ("cash_quality", ("现金质量", "现金利润", "现金流")),
        ("receivables", ("应收", "回款")),
        ("guarantee", ("担保",)),
        ("regulatory", ("监管", "处罚")),
        ("legal", ("诉讼", "仲裁")),
        ("debt", ("债务", "逾期", "偿债")),
    ):
        if any(term in text for term in terms):
            found.append(focus)
    return found or ["general"]


def _rule_draft(text: str, *, has_current: bool) -> IntentDraft:
    years = sorted(
        {int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)}
    )
    as_of = None
    cutoff = re.search(r"截至(20\d{2})年(?:底|末)", text)
    if cutoff:
        as_of = date(int(cutoff.group(1)), 12, 31)
    preferred = [
        value for label, value in _SOURCE_TERMS.items() if f"优先{label}" in text
    ]
    denied = [
        value
        for label, value in _SOURCE_TERMS.items()
        if f"不要{label}" in text or f"不使用{label}" in text or f"别用{label}" in text
    ]
    allowed_terms = [
        value
        for label, value in _SOURCE_TERMS.items()
        if f"只使用{label}" in text
        or f"只用{label}" in text
        or f"只核对{label}" in text
    ]
    unresolved: list[str] = []
    if any(term in text for term in ("去年", "今年", "最近")) and not years:
        unresolved.append("comparable_periods")
    if "子公司" in text:
        unresolved.append("subsidiary_scope")
    if allowed_terms and "匿名爆料" in text and "anonymous_tip" not in allowed_terms:
        unresolved.append("contradictory_source_policy")
    rejected = []
    if "找不到材料就写无风险" in text:
        rejected.append("absence_is_not_safety")
    if "忽略审核" in text:
        rejected.append("mandatory_review_cannot_be_bypassed")
    assumptions = []
    for marker, assumption in (
        ("相反的报道", "include_refuting_evidence"),
        ("合并净利润", "missing_group_profit_is_not_computable"),
        ("不要再搜索", "stop_external_calls"),
        ("统一成千元", "normalized_unit=CNY_1000;retain_original_unit"),
        ("负净资产", "allow_negative_equity;do_not_clamp_zero"),
        ("利润为负", "nonpositive_profit_is_not_computable"),
        ("转载只算一条", "deduplicate_by_original_source_id"),
        ("同名企业", "verify_entity_before_acceptance"),
        ("未发布的材料不要使用", "exclude_published_after_as_of"),
        ("之前的批准作废", "invalidate_old_report_approval"),
        ("同行比较", "request_compare_peers"),
        ("估算占款", "request_scenario;label_as_assumption"),
        ("不要执行两次", "deduplicate_by_source_message_id"),
    ):
        if marker in text:
            assumptions.append(assumption)
    condition = None
    requested_action = None
    conditional = re.search(r"(?:如果|若)(.+?)[，,](.+)", text)
    if conditional:
        condition, requested_action = (
            conditional.group(1).strip(),
            conditional.group(2).strip(),
        )
    return IntentDraft(
        operation=_operation(text, has_current=has_current),
        subject_hint=text,
        years=years,
        as_of=as_of,
        focus=_focuses(text),
        preferred_sources=list(dict.fromkeys(preferred)),
        allowed_sources=list(dict.fromkeys(allowed_terms)) or None,
        denied_sources=list(dict.fromkeys(denied)),
        condition=condition,
        requested_action=requested_action,
        unresolved_fields=list(dict.fromkeys(unresolved)),
        assumptions=assumptions,
        rejected_instructions=rejected,
    )


def parse_draft(
    text: str,
    *,
    catalog: list[SubjectRecord],
    anchor_date: date,
    current: TaskSpec | None = None,
    model: StructuredModel | None = None,
) -> tuple[IntentDraft, str, list[str]]:
    warnings: list[str] = []
    if model is not None:
        try:
            result = model.generate(
                output_schema=IntentDraft,
                purpose="intent_parse",
                prompt_version=PROMPT_VERSION,
                system_prompt=SYSTEM_PROMPT,
                payload={
                    "message": text,
                    "anchor_date": anchor_date.isoformat(),
                    "has_current_task": current is not None,
                    "current_task_spec": current.model_dump(mode="json")
                    if current
                    else None,
                    "imported_subjects": [
                        {
                            "subject_id": item.subject_id,
                            "subject_name": item.subject_name,
                            "aliases": item.aliases,
                            "available_years": item.available_years,
                        }
                        for item in catalog
                    ],
                },
            )
            return result.output, "LLM", warnings
        except StructuredModelError:
            warnings.append("LLM_INTENT_FALLBACK")
    return _rule_draft(text, has_current=current is not None), "RULE_FALLBACK", warnings


def _periods(years: list[int], as_of: date) -> list[Period]:
    return [
        Period(start=date(year, 1, 1), end=min(date(year, 12, 31), as_of))
        for year in sorted(set(years))
        if year <= as_of.year
    ]


def _question(text: str, focus: str, index: int) -> Question:
    digest = hashlib.sha256(f"{focus}\n{text}".encode()).hexdigest()[:16]
    criteria = {
        "cash_quality": "说明利润向经营现金流的转化及不可计算项",
        "receivables": "说明应收与回款变化并披露数据缺口",
        "guarantee": "核对担保主体、范围、金额及来源",
        "regulatory": "核对监管事项原始来源、主体和日期",
        "legal": "核对诉讼仲裁事项原始来源、主体和日期",
        "debt": "说明债务压力、到期范围及数据缺口",
        "general": "回答用户问题并逐项引用可追溯材料",
    }[focus]
    return Question(
        question_id=f"q_{index}_{digest}",
        text=text,
        completion_criteria=criteria,
        focus=focus,
    )


def build_task_spec(
    text: str,
    *,
    message_id: str,
    draft: IntentDraft,
    catalog: list[SubjectRecord],
    anchor_date: date,
    current: TaskSpec | None,
) -> TaskSpec:
    subject_matches = match_subjects(draft.subject_hint or text, catalog)
    subject = subject_matches[0] if len(subject_matches) == 1 else None
    explicit_unknown_subject = bool(
        re.search(r"(?:公司|主体)改为|调查[^，,。]{1,30}(?:公司|集团)", text)
    )
    if (
        subject is None
        and current is not None
        and not subject_matches
        and not explicit_unknown_subject
    ):
        subject = next(
            (item for item in catalog if item.subject_id == current.subject_id), None
        )
    unresolved = list(draft.unresolved_fields)
    if subject is None:
        unresolved.append("subject_id")
    resolved_as_of = draft.as_of or (current.as_of if current else anchor_date)
    if any(year > resolved_as_of.year for year in draft.years):
        unresolved.append("period_after_as_of")
    years = draft.years or (
        [period.end.year for period in current.periods] if current else []
    )
    assumptions = [*(current.assumptions if current else []), *draft.assumptions]
    if not years and subject is not None:
        completed = [
            year for year in subject.available_years if year <= resolved_as_of.year
        ]
        if completed:
            years = [max(completed)]
            assumptions.append("未指定期间，采用已导入的最近完整财务年度")
    periods = _periods(years, resolved_as_of)
    denied = list(
        dict.fromkeys(
            [
                *(current.source_policy.denied if current else []),
                *draft.denied_sources,
            ]
        )
    )
    preferred = draft.preferred_sources or (
        current.source_policy.preferred if current else []
    )
    preferred = [item for item in preferred if item not in denied]
    allowed = (
        draft.allowed_sources
        if draft.allowed_sources is not None
        else (current.source_policy.allowed if current else None)
    )
    if allowed is not None:
        allowed = [item for item in allowed if item not in denied]
        preferred = [item for item in preferred if item in allowed]
        if not allowed:
            unresolved.append("contradictory_source_policy")
            preferred = current.source_policy.preferred if current else []
            allowed = current.source_policy.allowed if current else None
            denied = current.source_policy.denied if current else []
    policy = SourcePolicy(preferred=preferred, allowed=allowed, denied=denied)
    conditions = list(current.conditions) if current else []
    if draft.condition and draft.requested_action:
        conditions.append(
            ConditionalInstruction(
                instruction_id=f"condition_{hashlib.sha256(message_id.encode()).hexdigest()[:16]}",
                condition=draft.condition,
                requested_action=draft.requested_action,
            )
        )
    focuses = list(dict.fromkeys(draft.focus or ["general"]))
    new_questions = [
        _question(text, focus, index) for index, focus in enumerate(focuses, 1)
    ]
    questions = (
        list(current.questions)
        if current is not None and focuses == ["general"]
        else [*(current.questions if current else []), *new_questions]
    )
    questions = list({item.question_id: item for item in questions}.values())
    return TaskSpec(
        version=(current.version + 1 if current else 1),
        source_message_id=message_id,
        operation="amend" if current else "start",
        subject_id=subject.subject_id if subject else None,
        subject_name=subject.subject_name if subject else "",
        case_id=subject.case_id if subject else None,
        as_of=resolved_as_of,
        periods=periods,
        source_policy=policy,
        questions=questions,
        conditions=conditions,
        assumptions=list(dict.fromkeys(assumptions)),
        unresolved_fields=list(dict.fromkeys(unresolved)),
        readiness="NEEDS_CLARIFICATION" if unresolved else "READY",
    )
