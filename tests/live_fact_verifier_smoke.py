"""Explicit one-document LLM verifier smoke; never run by default."""

import os

from app.config import get_settings
from app.models.search import SearchItem, SearchRequest, SearchResponse
from app.search.content import (
    ContentSnapshotStore,
    SnapshotContentFetcher,
    fetch_candidate_content,
)
from app.search.evidence import evidence_from_response
from app.search.verifier import (
    VerificationCacheStore,
    build_fact_verifier,
    verify_candidate_evidence,
)

_CATEGORY_TERMS = {
    "operations": "经营异常 经营风险",
    "regulatory": "监管 处罚",
    "legal": "诉讼 仲裁",
    "debt": "债务 逾期 违约",
    "fraud": "财务造假 虚假记载",
    "performance": "业绩预亏 业绩下滑",
    "controller": "实际控制人 控制权",
    "industry_risk": "景气度 下行 产能过剩",
}


def main() -> None:
    if os.getenv("RUN_LIVE_FACT_VERIFIER_SMOKE") != "1":
        raise SystemExit(
            "Set RUN_LIVE_FACT_VERIFIER_SMOKE=1 to authorize one model call"
        )
    url = os.getenv("LIVE_CONTENT_FETCH_URL", "").strip()
    subject = os.getenv("LIVE_VERIFICATION_SUBJECT", "").strip()
    category = os.getenv("LIVE_VERIFICATION_CATEGORY", "debt").strip()
    aliases = [
        value.strip()
        for value in os.getenv("LIVE_VERIFICATION_ALIASES", "").split(",")
        if value.strip()
    ]
    if not url or not subject:
        raise SystemExit("Set LIVE_CONTENT_FETCH_URL and LIVE_VERIFICATION_SUBJECT")
    try:
        category_terms = _CATEGORY_TERMS[category]
    except KeyError as exc:
        raise SystemExit(f"Unsupported LIVE_VERIFICATION_CATEGORY: {category}") from exc

    settings = get_settings()
    verifier = build_fact_verifier(settings)
    if verifier is None or verifier.name != "llm":
        raise SystemExit("Set FACT_VERIFIER=llm for the live verifier smoke test")
    content_store = ContentSnapshotStore(settings.search_content_snapshot_dir)
    request = SearchRequest(
        query_type="company" if category != "industry_risk" else "industry",
        subject=subject,
        subject_aliases=aliases,
        category=category,
        query=f'"{subject}" {category_terms}',
    )
    response = SearchResponse(
        provider="snapshot",
        request=request,
        items=[
            SearchItem(
                title=f"{subject} {category_terms}",
                content=f"{subject} {category_terms}",
                url=url,
                score=1.0,
                category=category,
            )
        ],
    )
    evidence = evidence_from_response(response)
    evidence, _ = fetch_candidate_content(
        evidence,
        fetcher=SnapshotContentFetcher(content_store),
        snapshot_store=content_store,
        max_candidates=1,
        max_concurrency=1,
    )
    evidence, status = verify_candidate_evidence(
        evidence,
        verifier=verifier,
        content_store=content_store,
        cache_store=VerificationCacheStore(settings.verification_snapshot_dir),
        min_confidence=settings.fact_verifier_min_confidence,
        max_candidates=1,
        max_input_chars=settings.fact_verifier_max_input_chars,
    )
    record = evidence[0].verification
    if record is None:
        raise SystemExit("Verifier produced no auditable record")
    print(f"provider={verifier.name}")
    print(f"model={verifier.model_name}")
    print(f"execution_status={status}")
    print(f"relation={record.relation}")
    print(f"subject_match={record.subject_match}")
    print(f"confidence={record.confidence}")
    print(f"accepted={record.accepted}")
    print(f"evidence_location={record.evidence_location}")
    print(f"cache_hit={record.cache_hit}")
    print(f"error_code={record.error_code}")
    if status == "FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
