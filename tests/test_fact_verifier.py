"""M2.1-C claim verification, back-reference, cache, and aggregation tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from app.agents.risk import analyze_risk
from app.config import Settings
from app.mcp.research_server import _search
from app.models.company import CompanyProfile
from app.models.content import ContentSegment, FetchedDocument
from app.models.financial import FinancialAnalysis, FinancialMetric
from app.models.research import ResearchQueryResult, ResearchResult
from app.models.search import SearchItem, SearchRequest, SearchResponse
from app.models.verification import VerifierDecision, VerifierInput
from app.report import render_credit_report
from app.search.content import ContentSnapshotStore, fetch_candidate_content
from app.search.evidence import (
    build_verification_claim,
    deduplicate_evidence,
    evidence_from_response,
    facts_from_evidence,
)
from app.search.verifier import (
    FactVerifierError,
    LLMFactVerifier,
    MockFactVerifier,
    RulesFactVerifier,
    VerificationCacheStore,
    build_fact_verifier,
    verify_candidate_evidence,
)

SUBJECT = "比亚迪股份有限公司"
CLAIM = "比亚迪股份有限公司存在债务逾期或违约。"


def _hash(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def _document(url: str, text: str) -> FetchedDocument:
    segment = ContentSegment(location="paragraph:1", text=text)
    return FetchedDocument(
        requested_url=url,
        final_url=url,
        status="SUCCESS",
        content_kind="HTML",
        content_type="text/html",
        byte_size=len(text.encode("utf-8")),
        char_count=len(text),
        document_hash=_hash(text),
        segments=[segment],
    )


class MappingFetcher:
    name = "fixture"

    def __init__(self, documents: dict[str, FetchedDocument]) -> None:
        self.documents = documents

    def fetch(self, url: str) -> FetchedDocument:
        return self.documents[url]


def _candidate_evidence(
    items: list[SearchItem],
    documents: dict[str, FetchedDocument],
    tmp_path: Path,
):
    request = SearchRequest(
        query_type="company",
        subject=SUBJECT,
        subject_aliases=["比亚迪", "002594"],
        category="debt",
        query=f'"{SUBJECT}" 债务 逾期',
    )
    response = SearchResponse(provider="snapshot", request=request, items=items)
    evidence = deduplicate_evidence(evidence_from_response(response))
    store = ContentSnapshotStore(tmp_path / "content")
    fetched, status = fetch_candidate_content(
        evidence,
        fetcher=MappingFetcher(documents),
        snapshot_store=store,
        max_candidates=10,
        max_concurrency=2,
    )
    assert status == "COMPLETE"
    return fetched, store


def _item(source_id: str, url: str) -> SearchItem:
    return SearchItem(
        title="比亚迪债务事项公告",
        content="比亚迪股份有限公司债务逾期事项核查。",
        url=url,
        score=0.95,
        source_id=source_id,
        category="debt",
    )


def _metric(name: str, values: list[float]) -> FinancialMetric:
    return FinancialMetric(
        metric=name,
        years=[2024, 2025],
        values=values,
        unit="ratio",
        formula="verification flow fixture",
    )


def _financial() -> FinancialAnalysis:
    return FinancialAnalysis(
        case_id="case_byd_002594",
        currency="CNY_1000",
        metrics={
            "revenue_growth": _metric("revenue_growth", [0.0, 0.1]),
            "net_profit_margin": _metric("net_profit_margin", [0.05, 0.04]),
            "current_ratio": _metric("current_ratio", [1.2, 0.9]),
            "debt_ratio": _metric("debt_ratio", [0.4, 0.6]),
            "operating_cash_flow_trend": _metric(
                "operating_cash_flow_trend", [100.0, 50.0]
            ),
        },
    )


def _company() -> CompanyProfile:
    return CompanyProfile(
        company_name=SUBJECT,
        industry="汽车制造业",
        registered_capital="test",
        established_date="test",
        shareholders=[],
        business_scope="test",
        major_customers=[],
        major_suppliers=[],
    )


def _decision(
    relation: str,
    excerpt: str | None,
    *,
    location: str | None = "paragraph:1",
    confidence: float = 0.95,
) -> VerifierDecision:
    return VerifierDecision(
        subject_match="EXACT" if relation != "IRRELEVANT" else "NONE",
        relation=relation,
        claim=CLAIM,
        evidence_excerpt=excerpt,
        evidence_location=location if excerpt else None,
        reason=f"fixture relation={relation}",
        confidence=confidence,
    )


def _verify(
    evidence,
    store: ContentSnapshotStore,
    verifier,
    tmp_path: Path,
    *,
    max_candidates: int = 10,
    max_input_chars: int = 30_000,
):
    return verify_candidate_evidence(
        evidence,
        verifier=verifier,
        content_store=store,
        cache_store=VerificationCacheStore(tmp_path / "verification"),
        min_confidence=0.75,
        max_candidates=max_candidates,
        max_input_chars=max_input_chars,
    )


def test_rules_verifier_only_accepts_literal_claim_in_reliable_source(
    tmp_path: Path,
) -> None:
    url = "https://www.szse.cn/disclosure/byd-debt.html"
    body = f"公开公告：{CLAIM}"
    evidence, store = _candidate_evidence(
        [_item("szse-debt", url)],
        {url: _document(url, body)},
        tmp_path,
    )

    verified, status = _verify(evidence, store, RulesFactVerifier(), tmp_path)

    assert status == "COMPLETE"
    assert verified[0].verification_claim is not None
    assert verified[0].verification_claim.statement == CLAIM
    assert verified[0].verification is not None
    assert verified[0].verification.accepted is True
    assert verified[0].evidence_stage == "VERIFIED"
    assert verified[0].verification_status == "SUPPORTED"


def test_excerpt_mismatch_and_low_confidence_never_create_verified_fact(
    tmp_path: Path,
) -> None:
    mismatch_url = "https://www.szse.cn/disclosure/mismatch.html"
    low_url = "https://www.cninfo.com.cn/new/disclosure/low.html"
    evidence, store = _candidate_evidence(
        [_item("mismatch", mismatch_url), _item("low", low_url)],
        {
            mismatch_url: _document(mismatch_url, f"公告：{CLAIM}"),
            low_url: _document(low_url, f"公告：{CLAIM}"),
        },
        tmp_path,
    )
    verifier = MockFactVerifier(
        {
            "mismatch": _decision("SUPPORTS", "正文中不存在的证据"),
            "low": _decision("SUPPORTS", CLAIM, confidence=0.5),
        }
    )

    verified, status = _verify(evidence, store, verifier, tmp_path)

    by_source = {item.source_id: item for item in verified}
    assert status == "PARTIAL"
    assert by_source["mismatch"].verification.error_code == "EVIDENCE_MISMATCH"
    assert by_source["mismatch"].evidence_stage == "CANDIDATE"
    assert by_source["low"].verification.error_code == "LOW_CONFIDENCE"
    assert by_source["low"].verification.accepted is False
    assert by_source["low"].verification_status == "UNVERIFIED"


def test_irrelevant_decision_is_rejected_with_auditable_reason(tmp_path: Path) -> None:
    url = "https://www.21jingji.com/irrelevant.html"
    evidence, store = _candidate_evidence(
        [_item("irrelevant", url)],
        {url: _document(url, "华谊兄弟存在债务逾期事项。")},
        tmp_path,
    )

    verified, status = _verify(
        evidence,
        store,
        MockFactVerifier({"irrelevant": _decision("IRRELEVANT", None)}),
        tmp_path,
    )

    assert status == "COMPLETE"
    assert verified[0].evidence_stage == "REJECTED"
    assert "VERIFIER_IRRELEVANT" in verified[0].filter_reasons


def test_independent_sources_corroborate_but_republication_does_not(
    tmp_path: Path,
) -> None:
    first = "https://www.szse.cn/disclosure/one.html"
    second = "https://www.21jingji.com/two.html"
    first_body = f"深交所公告：{CLAIM}"
    second_body = f"独立报道确认：{CLAIM}"
    evidence, store = _candidate_evidence(
        [_item("one", first), _item("two", second)],
        {
            first: _document(first, first_body),
            second: _document(second, second_body),
        },
        tmp_path,
    )
    verifier = MockFactVerifier(
        {
            "one": _decision("SUPPORTS", CLAIM),
            "two": _decision("SUPPORTS", CLAIM),
        }
    )

    corroborated, _ = _verify(evidence, store, verifier, tmp_path)

    assert {item.verification_status for item in corroborated} == {"CORROBORATED"}
    assert len(facts_from_evidence(corroborated)) == 2

    copied_evidence, copied_store = _candidate_evidence(
        [_item("one", first), _item("two", second)],
        {
            first: _document(first, first_body),
            second: _document(second, first_body),
        },
        tmp_path / "copy",
    )
    copied, _ = _verify(
        copied_evidence,
        copied_store,
        verifier,
        tmp_path / "copy",
    )

    assert {item.verification_status for item in copied} == {"SUPPORTED"}


def test_reliable_support_and_refutation_become_conflicting(tmp_path: Path) -> None:
    support_url = "https://www.szse.cn/disclosure/support.html"
    refute_url = "https://www.cninfo.com.cn/new/disclosure/refute.html"
    support_text = f"公告确认：{CLAIM}"
    refute_excerpt = "比亚迪股份有限公司不存在债务逾期或违约。"
    evidence, store = _candidate_evidence(
        [_item("support", support_url), _item("refute", refute_url)],
        {
            support_url: _document(support_url, support_text),
            refute_url: _document(refute_url, f"澄清公告：{refute_excerpt}"),
        },
        tmp_path,
    )
    verifier = MockFactVerifier(
        {
            "support": _decision("SUPPORTS", CLAIM),
            "refute": _decision("REFUTES", refute_excerpt),
        }
    )

    verified, _ = _verify(evidence, store, verifier, tmp_path)

    assert {item.verification_status for item in verified} == {"CONFLICTING"}
    assert facts_from_evidence(verified) == []

    company_result = ResearchQueryResult(
        query_type="company",
        query=f'"{SUBJECT}" 债务 逾期',
        found=True,
        facts=[],
        evidence=verified,
        candidate_evidence=verified,
        verification_status="CONFLICTING",
    )
    research = ResearchResult(
        company_name=SUBJECT,
        industry="汽车制造业",
        anomaly_flags=[],
        company_result=company_result,
        industry_result=ResearchQueryResult(
            query_type="industry",
            query='"汽车制造业" 景气度 风险',
            found=False,
            facts=[],
        ),
        status="PARTIAL",
        verification_status="CONFLICTING",
    )
    risk = analyze_risk(
        _financial(),
        settings=Settings(_env_file=None),
        research=research,
    )
    conflict_flag = next(
        flag for flag in risk.risk_flags if flag.type == "external_research_conflict"
    )
    report = render_credit_report(_company(), _financial(), risk)

    assert support_url in conflict_flag.evidence
    assert refute_url in conflict_flag.evidence
    assert "外部调查存在可靠来源冲突" in report


def test_c_tier_support_stays_unverified(tmp_path: Path) -> None:
    url = "https://example.com/byd-debt.html"
    evidence, store = _candidate_evidence(
        [_item("tier-c", url)],
        {url: _document(url, f"自媒体称：{CLAIM}")},
        tmp_path,
    )

    verified, _ = _verify(
        evidence,
        store,
        MockFactVerifier({"tier-c": _decision("SUPPORTS", CLAIM)}),
        tmp_path,
    )

    assert verified[0].verification.accepted is True
    assert verified[0].source_tier == "C"
    assert verified[0].evidence_stage == "CANDIDATE"
    assert verified[0].verification_status == "UNVERIFIED"
    assert facts_from_evidence(verified) == []


class CountingVerifier:
    name = "counting"
    model_name = "counting-v1"

    def __init__(self, decision: VerifierDecision) -> None:
        self.decision = decision
        self.calls = 0
        self.requests: list[VerifierInput] = []

    def verify(self, request: VerifierInput):
        self.calls += 1
        self.requests.append(request)
        return SimpleNamespace(decision=self.decision, attempts=1)


def test_verification_cache_and_candidate_budget_are_explicit(tmp_path: Path) -> None:
    first = "https://www.szse.cn/disclosure/cache-one.html"
    second = "https://www.cninfo.com.cn/new/disclosure/cache-two.html"
    evidence, store = _candidate_evidence(
        [_item("cache-one", first), _item("cache-two", second)],
        {
            first: _document(first, f"公告：{CLAIM}"),
            second: _document(second, f"公告二：{CLAIM}"),
        },
        tmp_path,
    )
    verifier = CountingVerifier(_decision("SUPPORTS", CLAIM))

    first_run, first_status = _verify(
        evidence,
        store,
        verifier,
        tmp_path,
        max_candidates=1,
    )
    second_run, second_status = _verify(
        evidence,
        store,
        verifier,
        tmp_path,
        max_candidates=1,
    )

    assert first_status == second_status == "PARTIAL"
    assert verifier.calls == 1
    assert first_run[1].verification.error_code == "BUDGET_EXCEEDED"
    assert second_run[0].verification.cache_hit is True


def test_single_oversized_segment_is_truncated_to_model_input_budget(
    tmp_path: Path,
) -> None:
    url = "https://www.szse.cn/disclosure/long.html"
    evidence, store = _candidate_evidence(
        [_item("long", url)],
        {url: _document(url, CLAIM + "附录" * 1_000)},
        tmp_path,
    )
    verifier = CountingVerifier(_decision("SUPPORTS", CLAIM))

    verified, status = _verify(
        evidence,
        store,
        verifier,
        tmp_path,
        max_input_chars=100,
    )

    assert status == "COMPLETE"
    assert len(verifier.requests[0].segments[0].text) == 100
    assert verified[0].verification.accepted is True


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self.content))]
        )


def test_llm_verifier_uses_json_only_prompt_and_rejects_invalid_output() -> None:
    decision = _decision("SUPPORTS", CLAIM)
    completions = FakeCompletions(decision.model_dump_json())
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    verifier = LLMFactVerifier(
        api_key="test-key",
        base_url="https://llm.example.test/v1",
        model_name="test-model",
        timeout_seconds=1,
        max_attempts=1,
        client=client,
    )
    response = SearchResponse(
        provider="snapshot",
        request=SearchRequest(
            query_type="company",
            subject=SUBJECT,
            category="debt",
            query="test",
        ),
        items=[],
    )
    request = VerifierInput(
        claim=build_verification_claim(response),
        source_id="llm-source",
        source_url="https://www.szse.cn/disclosure/llm.html",
        source_tier="A",
        document_hash=_hash(CLAIM),
        segments=[ContentSegment(location="paragraph:1", text=CLAIM)],
    )

    outcome = verifier.verify(request)

    assert outcome.decision.relation == "SUPPORTS"
    call = completions.calls[0]
    assert call["response_format"] == {"type": "json_object"}
    assert "不可信" in call["messages"][0]["content"]
    assert CLAIM in call["messages"][1]["content"]

    invalid = LLMFactVerifier(
        api_key="test-key",
        base_url="https://llm.example.test/v1",
        model_name="test-model",
        timeout_seconds=1,
        max_attempts=1,
        client=SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions("not-json"))
        ),
    )
    try:
        invalid.verify(request)
    except FactVerifierError as exc:
        assert exc.code == "INVALID_OUTPUT"
    else:
        raise AssertionError("invalid structured output should fail")


class OneCandidateProvider:
    name = "snapshot"

    def __init__(self, url: str) -> None:
        self.url = url

    def search(self, request: SearchRequest) -> SearchResponse:
        items = (
            [_item("integrated-source", self.url)] if request.category == "debt" else []
        )
        return SearchResponse(provider=self.name, request=request, items=items)


def test_search_integration_creates_traceable_fact_only_after_verification(
    tmp_path: Path,
) -> None:
    url = "https://www.szse.cn/disclosure/integrated.html"
    result = _search(
        "company",
        SUBJECT,
        provider=OneCandidateProvider(url),
        content_fetcher=MappingFetcher({url: _document(url, f"公告：{CLAIM}")}),
        fact_verifier=MockFactVerifier(
            {"integrated-source": _decision("SUPPORTS", CLAIM)}
        ),
        settings=Settings(
            _env_file=None,
            search_content_snapshot_dir=tmp_path / "content",
            verification_snapshot_dir=tmp_path / "verification",
        ),
    )

    assert result.verification_execution_status == "COMPLETE"
    assert result.completed_verification_count == 1
    assert result.failed_verification_count == 0
    assert result.verification_status == "SUPPORTED"
    assert len(result.facts) == 1
    assert result.facts[0].statement == CLAIM
    assert result.facts[0].evidence_location == "paragraph:1"
    assert result.facts[0].verifier_model == "mock-verifier-v1"
    serialized = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    assert "paragraph:1" in serialized

    research = ResearchResult(
        company_name=SUBJECT,
        industry="汽车制造业",
        anomaly_flags=[],
        company_result=result,
        industry_result=ResearchQueryResult(
            query_type="industry",
            query='"汽车制造业" 景气度 风险',
            found=False,
            facts=[],
        ),
        status="PARTIAL",
        verification_status="SUPPORTED",
        verified_fact_count=1,
    )
    financial = _financial()
    risk = analyze_risk(
        financial,
        settings=Settings(_env_file=None),
        research=research,
    )
    evidence_references = [
        reference for flag in risk.risk_flags for reference in flag.evidence
    ]
    report = render_credit_report(
        _company(),
        financial,
        risk,
    )

    assert url in evidence_references
    assert "evidence_location:paragraph:1" in evidence_references
    assert "verifier_model:mock-verifier-v1" in evidence_references
    assert url in report
    assert "evidence_location:paragraph:1" in report


def test_fact_verifier_factory_is_explicit_and_reuses_base_model_name(
    tmp_path: Path,
) -> None:
    rules = build_fact_verifier(Settings(_env_file=None, fact_verifier="rules"))
    disabled = build_fact_verifier(Settings(_env_file=None, fact_verifier="disabled"))

    assert isinstance(rules, RulesFactVerifier)
    assert disabled is None

    try:
        build_fact_verifier(
            Settings(
                _env_file=None,
                fact_verifier="llm",
                model_name="configured-model",
                model_api_key="",
                verification_snapshot_dir=tmp_path,
            )
        )
    except ValueError as exc:
        assert "MODEL_API_KEY" in str(exc)
    else:
        raise AssertionError("llm verifier should require an API key")
