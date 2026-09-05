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
from app.models.verification import VerificationClaim

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
_CLAIM_TEMPLATES: dict[str, str] = {
    "operations": "{subject}存在经营异常或重大经营风险。",
    "regulatory": "{subject}受到监管处罚、罚款或纪律处分。",
    "legal": "{subject}涉及重大诉讼、仲裁或法院判决。",
    "debt": "{subject}存在债务逾期或违约。",
    "fraud": "{subject}存在财务造假、虚假记载或违规披露。",
    "performance": "{subject}存在业绩预亏、预减或显著下滑。",
    "controller": "{subject}的实际控制人或控制权发生重大变化。",
    "industry_risk": "{subject}存在景气度下行、需求收缩或产能过剩风险。",
}


def build_verification_claim(response: SearchResponse) -> VerificationClaim:
    """Build one stable, category-scoped claim shared by candidate sources."""

    subject = response.request.subject
    statement = _CLAIM_TEMPLATES.get(
        response.request.category,
        "{subject}存在与{category}相关且需要核查的公开事项。",
    ).format(subject=subject, category=response.request.category)
    canonical = f"{subject}\n{response.request.category}\n{statement}"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return VerificationClaim(
        claim_id=f"claim:{digest}",
        subject=subject,
        subject_aliases=response.request.subject_aliases,
        category=response.request.category,
        statement=statement,
    )


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


def _subject_match_text(value: str, response: SearchResponse) -> SubjectMatch:
    searchable = value.casefold()
    if response.request.subject.casefold() in searchable:
        return "EXACT"
    if any(
        alias.casefold() in searchable for alias in response.request.subject_aliases
    ):
        return "ALIAS"
    return "NONE"


def _subject_match(item: SearchItem, response: SearchResponse) -> SubjectMatch:
    return _subject_match_text(f"{item.title} {item.content}", response)


def _category_matches(item: SearchItem, response: SearchResponse) -> bool:
    searchable = f"{item.title} {item.content}".casefold()
    keywords = _CATEGORY_KEYWORDS.get(response.request.category, ())
    return not keywords or any(keyword.casefold() in searchable for keyword in keywords)


def _filter_reasons(
    *,
    subject_match: SubjectMatch,
    title_subject_match: SubjectMatch,
    require_title_subject_match: bool,
    category_match: bool,
    relevance_score: float,
    min_relevance_score: float,
) -> list[FilterReason]:
    reasons: list[FilterReason] = []
    if subject_match == "NONE":
        reasons.append("SUBJECT_MISMATCH")
    elif require_title_subject_match and title_subject_match == "NONE":
        reasons.append("SUBJECT_TITLE_MISMATCH")
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
    claim = build_verification_claim(response)
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
            title_subject_match = _subject_match_text(item.title, response)
            category_match = _category_matches(item, response)
            filter_reasons = _filter_reasons(
                subject_match=subject_match,
                title_subject_match=title_subject_match,
                require_title_subject_match=(
                    response.request.query_type == "company" and tier == "C"
                ),
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
                verification_claim=claim,
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
    if verified:
        return "UNVERIFIED"
    if any(item.evidence_stage == "CANDIDATE" for item in evidence):
        return "UNVERIFIED"
    return "NOT_FOUND"


def facts_from_evidence(evidence: list[ResearchEvidence]) -> list[ResearchFact]:
    allowed = {"SUPPORTED", "CORROBORATED"}
    facts = []
    for item in evidence:
        if item.evidence_stage != "VERIFIED" or item.verification_status not in allowed:
            continue
        verification = item.verification
        claim = verification.claim if verification else item.verification_claim
        canonical = "\n".join(
            (
                claim.claim_id if claim else "",
                item.source_id,
                item.content_hash,
            )
        )
        fact_id = f"fact:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
        facts.append(
            ResearchFact(
                fact_id=fact_id,
                subject=claim.subject if claim else None,
                category=item.category,
                statement=(verification.claim.statement if verification else item.fact),
                source_id=item.source_id,
                source_url=item.source_url,
                verification_status=item.verification_status,
                claim_id=claim.claim_id if claim else None,
                relation=verification.relation if verification else "SUPPORTS",
                evidence_excerpt=(
                    verification.evidence_excerpt if verification else None
                ),
                evidence_location=(
                    verification.evidence_location if verification else None
                ),
                verifier_model=(verification.verifier_model if verification else None),
                verified_at=verification.verified_at if verification else None,
            )
        )
    return facts
