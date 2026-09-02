"""Rules-only investigation intent normalization and query planning."""

import hashlib
import json

from app.models.company import CompanyProfile
from app.models.investigation import (
    IntentTrigger,
    InvestigationIntent,
    QueryPlan,
)
from app.models.risk import RiskFlag
from app.search.queries import build_search_requests

CATEGORY_ORDER = (
    "operations",
    "regulatory",
    "legal",
    "debt",
    "fraud",
    "performance",
    "controller",
    "industry_risk",
)

_ANOMALY_CATEGORIES = {
    "REVENUE_CASHFLOW_DIVERGENCE": ("operations", "performance"),
    "DEBT_RATIO_RISING": ("debt",),
}
_RISK_CATEGORIES = {
    "leverage": ("debt",),
    "cashflow": ("operations",),
    "liquidity": ("debt",),
    "external_regulatory": ("regulatory",),
    "external_legal": ("legal",),
    "external_debt": ("debt",),
    "external_fraud": ("fraud",),
    "external_performance": ("performance",),
    "external_controller": ("controller",),
    "external_industry_risk": ("industry_risk",),
}
_COMMENT_KEYWORDS = {
    "regulatory": ("监管", "处罚", "罚款", "处分"),
    "legal": ("诉讼", "仲裁", "法院", "判决"),
    "debt": ("债务", "逾期", "违约", "偿债", "流动性"),
    "fraud": ("造假", "虚假", "欺诈", "违规披露"),
    "performance": ("业绩", "利润", "营收", "预亏", "预减"),
    "controller": ("实控", "控制人", "控制权"),
    "industry_risk": ("行业", "景气", "产能", "需求"),
    "operations": ("经营", "现金流", "供应链"),
}


def _ordered(categories: set[str]) -> list[str]:
    return [category for category in CATEGORY_ORDER if category in categories]


def _digest(prefix: str, payload: object) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"{prefix}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def build_investigation_intent(
    *,
    anomaly_flags: list[str],
    risk_flags: list[RiskFlag],
    human_decision: str | None,
    human_comment: str | None,
) -> InvestigationIntent:
    """Normalize workflow signals into one stable, explainable intent."""

    triggers: list[IntentTrigger] = []
    selected: set[str] = set()
    for anomaly in sorted(set(anomaly_flags)):
        categories = list(_ANOMALY_CATEGORIES.get(anomaly, ("operations",)))
        selected.update(categories)
        triggers.append(
            IntentTrigger(source="anomaly", value=anomaly, categories=categories)
        )
    for risk_type in sorted({flag.type for flag in risk_flags}):
        categories = list(_RISK_CATEGORIES.get(risk_type, ()))
        if categories:
            selected.update(categories)
            triggers.append(
                IntentTrigger(source="risk", value=risk_type, categories=categories)
            )

    normalized_comment = human_comment.strip() if human_comment else None
    if human_decision == "research":
        comment_categories = [
            category
            for category in CATEGORY_ORDER
            if normalized_comment
            and any(
                keyword in normalized_comment for keyword in _COMMENT_KEYWORDS[category]
            )
        ]
        if not comment_categories:
            comment_categories = _ordered(selected) or ["operations"]
        selected.update(comment_categories)
        triggers.append(
            IntentTrigger(
                source="human",
                value=normalized_comment or "补充调查",
                categories=comment_categories,
            )
        )

    if not selected:
        selected.add("operations")
        triggers.append(
            IntentTrigger(
                source="anomaly", value="UNSPECIFIED_SIGNAL", categories=["operations"]
            )
        )
    categories = _ordered(selected)
    payload = {
        "categories": categories,
        "triggers": [trigger.model_dump(mode="json") for trigger in triggers],
        "human_decision": human_decision,
        "human_comment": normalized_comment,
    }
    return InvestigationIntent(
        intent_id=_digest("intent", payload),
        categories=categories,
        triggers=triggers,
        human_decision=human_decision,
        human_comment=normalized_comment,
    )


def build_query_plan(
    company: CompanyProfile,
    intent: InvestigationIntent,
    previous: QueryPlan | None = None,
) -> QueryPlan:
    company_categories = [c for c in intent.categories if c != "industry_risk"]
    queries = build_search_requests(
        "company", company.company_name, categories=company_categories
    )
    if "industry_risk" in intent.categories:
        queries.extend(
            build_search_requests(
                "industry", company.industry, categories=["industry_risk"]
            )
        )
    query_texts = [item.query for item in queries]
    previous_texts = [item.query for item in previous.queries] if previous else []
    payload = {
        "intent_id": intent.intent_id,
        "queries": [item.model_dump(mode="json") for item in queries],
    }
    return QueryPlan(
        plan_id=_digest("query-plan", payload),
        intent=intent,
        queries=queries,
        previous_plan_id=previous.plan_id if previous else None,
        added_queries=[query for query in query_texts if query not in previous_texts],
        removed_queries=[query for query in previous_texts if query not in query_texts],
    )
