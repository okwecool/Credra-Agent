"""Convert provider results into ordered, auditable research evidence."""

import hashlib
import re
from urllib.parse import urlparse

from app.models.research import ResearchFact
from app.models.search import (
    ResearchEvidence,
    SearchItem,
    SearchResponse,
    SourceTier,
    VerificationStatus,
)

_TIER_A_DOMAINS = (
    "gov.cn",
    "cninfo.com.cn",
    "szse.cn",
    "sse.com.cn",
)
_TIER_B_DOMAINS = (
    "reuters.com",
    "caixin.com",
    "yicai.com",
    "stcn.com",
    "cs.com.cn",
    "21jingji.com",
    "cnstock.com",
    "zqrb.cn",
)
_TIER_RANK: dict[SourceTier, int] = {"A": 0, "B": 1, "C": 2}
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "operations": ("经营异常", "异常经营", "经营风险"),
    "regulatory": ("监管", "处罚", "罚款", "处分"),
    "legal": ("诉讼", "仲裁", "法院", "判决"),
    "debt": ("债务", "逾期", "违约"),
    "fraud": ("财务造假", "虚假记载", "欺诈", "违规披露"),
    "performance": ("业绩预警", "预亏", "预减", "业绩下滑"),
    "controller": ("实际控制人", "实控人", "控制权"),
    "industry_risk": ("景气度", "行业风险", "下行", "产能过剩"),
}


def source_domain(url: str | None) -> str | None:
    if not url:
        return None
    domain = (urlparse(url).hostname or "").lower()
    return domain.removeprefix("www.") or None


def classify_source_tier(url: str | None) -> SourceTier:
    domain = source_domain(url)
    if domain and any(
        domain == suffix or domain.endswith(f".{suffix}") for suffix in _TIER_A_DOMAINS
    ):
        return "A"
    if domain and any(
        domain == suffix or domain.endswith(f".{suffix}") for suffix in _TIER_B_DOMAINS
    ):
        return "B"
    return "C"


def content_hash(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content).strip()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _source_id(item: SearchItem, digest: str) -> str:
    return item.source_id or f"web-{digest.removeprefix('sha256:')[:20]}"


def _matches_request(item: SearchItem, response: SearchResponse) -> bool:
    searchable = f"{item.title} {item.content}".casefold()
    subject_matches = response.request.subject.casefold() in searchable
    keywords = _CATEGORY_KEYWORDS.get(response.request.category, ())
    category_matches = not keywords or any(
        keyword.casefold() in searchable for keyword in keywords
    )
    return subject_matches and category_matches


def evidence_from_response(
    response: SearchResponse,
    *,
    min_relevance_score: float = 0.5,
) -> list[ResearchEvidence]:
    if not 0.0 <= min_relevance_score <= 1.0:
        raise ValueError("min_relevance_score must be between 0 and 1")
    evidence: list[ResearchEvidence] = []
    for item in response.items:
        tier = classify_source_tier(item.url)
        query_match = _matches_request(item, response)
        verification_status: VerificationStatus = (
            "SUPPORTED"
            if tier in ("A", "B") and query_match and item.score >= min_relevance_score
            else "UNVERIFIED"
        )
        digest = content_hash(item.content)
        evidence.append(
            ResearchEvidence(
                fact=item.content,
                verification_status=verification_status,
                title=item.title,
                source_url=item.url,
                source_domain=source_domain(item.url),
                source_tier=tier,
                published_at=item.published_at,
                retrieved_at=response.retrieved_at,
                query=response.request.query,
                relevance_score=item.score,
                content_hash=digest,
                source_id=_source_id(item, digest),
                category=item.category or response.request.category,
                query_match=query_match,
            )
        )
    return sorted(
        evidence,
        key=lambda item: (_TIER_RANK[item.source_tier], -item.relevance_score),
    )


def deduplicate_evidence(
    evidence: list[ResearchEvidence],
) -> list[ResearchEvidence]:
    unique: dict[tuple[str | None, str], ResearchEvidence] = {}
    for item in evidence:
        key = (item.source_url, item.content_hash)
        existing = unique.get(key)
        item_is_verified = item.verification_status in {"SUPPORTED", "CORROBORATED"}
        existing_is_verified = (
            existing is not None
            and existing.verification_status
            in {
                "SUPPORTED",
                "CORROBORATED",
            }
        )
        if (
            existing is None
            or (item_is_verified and not existing_is_verified)
            or (
                item_is_verified == existing_is_verified
                and item.relevance_score > existing.relevance_score
            )
        ):
            unique[key] = item
    grouped_domains: dict[str, set[str]] = {}
    for item in unique.values():
        if item.verification_status == "SUPPORTED" and item.source_domain:
            grouped_domains.setdefault(item.content_hash, set()).add(item.source_domain)
    verified = [
        item.model_copy(update={"verification_status": "CORROBORATED"})
        if item.verification_status == "SUPPORTED"
        and len(grouped_domains.get(item.content_hash, set())) >= 2
        else item
        for item in unique.values()
    ]
    return sorted(
        verified,
        key=lambda item: (_TIER_RANK[item.source_tier], -item.relevance_score),
    )


def overall_verification_status(
    evidence: list[ResearchEvidence],
) -> VerificationStatus:
    if not evidence:
        return "NOT_FOUND"
    statuses = {item.verification_status for item in evidence}
    if "CONFLICTING" in statuses:
        return "CONFLICTING"
    if "CORROBORATED" in statuses:
        return "CORROBORATED"
    if "SUPPORTED" in statuses:
        return "SUPPORTED"
    return "UNVERIFIED"


def facts_from_evidence(
    evidence: list[ResearchEvidence],
    *,
    include_unverified: bool = False,
) -> list[ResearchFact]:
    allowed = {"SUPPORTED", "CORROBORATED"}
    facts = []
    for item in evidence:
        if not include_unverified and item.verification_status not in allowed:
            continue
        facts.append(
            ResearchFact(
                category=item.category,
                statement=item.fact,
                source_id=item.source_id,
                source_url=item.source_url,
                verification_status=item.verification_status,
            )
        )
    return facts
