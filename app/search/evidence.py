"""Convert provider results into staged, auditable research evidence."""

import hashlib
import re
from collections import defaultdict
from urllib.parse import urlparse

from app.models.research import ResearchFact
from app.models.search import (
    EvidenceStage,
    FilterReason,
    ResearchEvidence,
    SearchItem,
    SearchResponse,
    SourceTier,
    SubjectMatch,
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
_STAGE_RANK: dict[EvidenceStage, int] = {
    "VERIFIED": 0,
    "CANDIDATE": 1,
    "REJECTED": 2,
}
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


def _subject_match(item: SearchItem, response: SearchResponse) -> SubjectMatch:
    searchable = f"{item.title} {item.content}".casefold()
    if response.request.subject.casefold() in searchable:
        return "EXACT"
    if any(
        alias.casefold() in searchable for alias in response.request.subject_aliases
    ):
        return "ALIAS"
    return "NONE"


def _category_matches(item: SearchItem, response: SearchResponse) -> bool:
    searchable = f"{item.title} {item.content}".casefold()
    keywords = _CATEGORY_KEYWORDS.get(response.request.category, ())
    return not keywords or any(keyword.casefold() in searchable for keyword in keywords)


def _filter_reasons(
    *,
    subject_match: SubjectMatch,
    category_match: bool,
    relevance_score: float,
    min_relevance_score: float,
) -> list[FilterReason]:
    reasons: list[FilterReason] = []
    if subject_match == "NONE":
        reasons.append("SUBJECT_MISMATCH")
    if not category_match:
        reasons.append("CATEGORY_MISMATCH")
    if relevance_score < min_relevance_score:
        reasons.append("LOW_RELEVANCE")
    return reasons


def evidence_from_response(
    response: SearchResponse,
    *,
    min_relevance_score: float = 0.5,
    trusted_fixture: bool = False,
) -> list[ResearchEvidence]:
    """Classify raw results; Web candidates remain unverified until deep verification."""

    if not 0.0 <= min_relevance_score <= 1.0:
        raise ValueError("min_relevance_score must be between 0 and 1")
    evidence: list[ResearchEvidence] = []
    for item in response.items:
        tier = classify_source_tier(item.url)
        if trusted_fixture:
            subject_match: SubjectMatch = "EXACT"
            category_match = True
            filter_reasons: list[FilterReason] = []
            evidence_stage: EvidenceStage = "VERIFIED"
            verification_status: VerificationStatus = "SUPPORTED"
        else:
            subject_match = _subject_match(item, response)
            category_match = _category_matches(item, response)
            filter_reasons = _filter_reasons(
                subject_match=subject_match,
                category_match=category_match,
                relevance_score=item.score,
                min_relevance_score=min_relevance_score,
            )
            evidence_stage = "REJECTED" if filter_reasons else "CANDIDATE"
            verification_status = "UNVERIFIED"
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
                query_match=subject_match != "NONE" and category_match,
                subject_match=subject_match,
                category_match=category_match,
                filter_reasons=filter_reasons,
                evidence_stage=evidence_stage,
            )
        )
    return _sort_evidence(evidence)


def _sort_evidence(evidence: list[ResearchEvidence]) -> list[ResearchEvidence]:
    return sorted(
        evidence,
        key=lambda item: (
            _STAGE_RANK[item.evidence_stage],
            _TIER_RANK[item.source_tier],
            -item.relevance_score,
            item.source_url or item.source_id,
        ),
    )


def deduplicate_evidence(
    evidence: list[ResearchEvidence],
) -> list[ResearchEvidence]:
    """Preserve every result while marking non-winning duplicates as rejected."""

    grouped: dict[tuple[str | None, str], list[ResearchEvidence]] = defaultdict(list)
    for item in evidence:
        grouped[(item.source_url, item.content_hash)].append(item)

    classified: list[ResearchEvidence] = []
    for items in grouped.values():
        winner = min(
            items,
            key=lambda item: (
                _STAGE_RANK[item.evidence_stage],
                -item.relevance_score,
                item.query,
            ),
        )
        classified.append(winner)
        winner_used = False
        for item in items:
            if item is winner and not winner_used:
                winner_used = True
                continue
            reasons = [*item.filter_reasons]
            if "DUPLICATE_CONTENT" not in reasons:
                reasons.append("DUPLICATE_CONTENT")
            classified.append(
                item.model_copy(
                    update={
                        "verification_status": "UNVERIFIED",
                        "filter_reasons": reasons,
                        "evidence_stage": "REJECTED",
                    }
                )
            )
    return _sort_evidence(classified)


def overall_verification_status(
    evidence: list[ResearchEvidence],
) -> VerificationStatus:
    verified = [item for item in evidence if item.evidence_stage == "VERIFIED"]
    statuses = {item.verification_status for item in verified}
    if "CONFLICTING" in statuses:
        return "CONFLICTING"
    if "CORROBORATED" in statuses:
        return "CORROBORATED"
    if "SUPPORTED" in statuses:
        return "SUPPORTED"
    if any(item.evidence_stage == "CANDIDATE" for item in evidence):
        return "UNVERIFIED"
    return "NOT_FOUND"


def facts_from_evidence(evidence: list[ResearchEvidence]) -> list[ResearchFact]:
    allowed = {"SUPPORTED", "CORROBORATED"}
    facts = []
    for item in evidence:
        if item.evidence_stage != "VERIFIED" or item.verification_status not in allowed:
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
