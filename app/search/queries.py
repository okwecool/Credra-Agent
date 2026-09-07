"""Deterministic company and industry search queries."""

import re
from collections.abc import Iterable

from app.models.search import QueryType, SearchRequest

COMPANY_QUERY_SUFFIXES = (
    ("operations", "经营异常"),
    ("regulatory", "监管处罚"),
    ("legal", "诉讼 仲裁"),
    ("debt", "债务 逾期"),
    ("fraud", "财务造假"),
    ("performance", "业绩预警"),
    ("controller", "实际控制人 风险"),
)
INDUSTRY_QUERY_SUFFIXES = (("industry_risk", "景气度 风险"),)
_COMPANY_SUFFIXES = (
    "集团股份有限公司",
    "股份有限公司",
    "集团有限公司",
    "有限责任公司",
    "有限公司",
)


def derive_subject_aliases(query_type: QueryType, subject: str) -> list[str]:
    """Derive conservative aliases without inventing external identity data."""

    normalized = subject.strip()
    aliases: list[str] = []
    if query_type == "company":
        for suffix in _COMPANY_SUFFIXES:
            if normalized.endswith(suffix):
                alias = normalized.removesuffix(suffix).strip()
                if len(alias) >= 2:
                    aliases.append(alias)
                break
    else:
        base_industry = re.split(r"[（(]", normalized, maxsplit=1)[0].strip()
        if len(base_industry) >= 2 and base_industry != normalized:
            aliases.append(base_industry)
    return aliases


def _normalize_aliases(
    query_type: QueryType,
    subject: str,
    explicit_aliases: Iterable[str] | None,
) -> list[str]:
    candidates = [
        *derive_subject_aliases(query_type, subject),
        *(explicit_aliases or ()),
    ]
    unique: list[str] = []
    seen = {subject.casefold()}
    for candidate in candidates:
        normalized = candidate.strip()
        folded = normalized.casefold()
        if len(normalized) >= 2 and folded not in seen:
            seen.add(folded)
            unique.append(normalized)
    return unique


def build_search_requests(
    query_type: QueryType,
    subject: str,
    subject_aliases: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
) -> list[SearchRequest]:
    """Build a stable query set whose order is part of the M2 contract."""

    normalized = subject.strip()
    if not normalized:
        raise ValueError("search subject cannot be empty")
    aliases = _normalize_aliases(query_type, normalized, subject_aliases)
    suffixes = (
        COMPANY_QUERY_SUFFIXES if query_type == "company" else INDUSTRY_QUERY_SUFFIXES
    )
    selected = set(categories) if categories is not None else None
    if selected is not None:
        known = {category for category, _ in suffixes}
        unknown = selected - known
        if unknown:
            raise ValueError(
                f"unsupported {query_type} query categories: {sorted(unknown)}"
            )
        suffixes = tuple(item for item in suffixes if item[0] in selected)
    return [
        SearchRequest(
            query_type=query_type,
            subject=normalized,
            subject_aliases=aliases,
            category=category,
            query=f'"{normalized}" {suffix}',
        )
        for category, suffix in suffixes
    ]
