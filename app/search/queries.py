"""Deterministic company and industry search queries."""

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


def build_search_requests(
    query_type: QueryType,
    subject: str,
) -> list[SearchRequest]:
    """Build a stable query set whose order is part of the M2 contract."""

    normalized = subject.strip()
    if not normalized:
        raise ValueError("search subject cannot be empty")
    suffixes = (
        COMPANY_QUERY_SUFFIXES if query_type == "company" else INDUSTRY_QUERY_SUFFIXES
    )
    return [
        SearchRequest(
            query_type=query_type,
            subject=normalized,
            category=category,
            query=f'"{normalized}" {suffix}',
        )
        for category, suffix in suffixes
    ]
