"""M4-B citation-bound evidence summaries and review-only Query proposals."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.llm.gateway import StructuredModelResult
from app.llm.research_analysis import build_research_analysis
from app.mcp.research_client import ResearchMCPClient
from app.mcp.research_server import mcp as research_mcp
from app.models.investigation import IntentTrigger, InvestigationIntent, QueryPlan
from app.models.research import ResearchFact, ResearchQueryResult, ResearchResult
from app.models.search import ResearchEvidence, SearchRequest
from app.models.verification import ClaimVerification, VerificationClaim
from app.runtime.tasks import start_task
from app.runtime.tracing import read_trace

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _identifier(prefix: str, token: str) -> str:
    return f"{prefix}:{token * 64}"


def _plan() -> QueryPlan:
    return QueryPlan(
        plan_id=_identifier("query-plan", "b"),
        intent=InvestigationIntent(
            intent_id=_identifier("intent", "a"),
            categories=["debt", "regulatory"],
            triggers=[
                IntentTrigger(
                    source="anomaly",
                    value="DEBT_RATIO_RISING",
                    categories=["debt", "regulatory"],
                )
            ],
        ),
        queries=[
            SearchRequest(
                query_type="company",
                subject="测试企业",
                category="debt",
                query='"测试企业" 债务 逾期',
            ),
            SearchRequest(
                query_type="industry",
                subject="测试行业",
                category="regulatory",
                query='"测试行业" 监管 处罚',
            ),
        ],
    )


def _evidence(
    *,
    source_id: str,
    status: str,
    stage: str,
    claim: VerificationClaim,
    verification: ClaimVerification | None = None,
) -> ResearchEvidence:
    return ResearchEvidence(
        fact="外部候选证据摘要。",
        verification_status=status,
        title=f"{source_id} 标题",
        source_url=f"https://example.test/{source_id}",
        source_domain="example.test",
        source_tier="A",
        retrieved_at=datetime.now(UTC),
        query='"测试企业" 债务 逾期',
        relevance_score=0.95,
        content_hash=f"sha256:{source_id[-1] * 64}",
        source_id=source_id,
        category="debt",
        query_match=True,
        subject_match="EXACT",
        category_match=True,
        evidence_stage=stage,
        verification_claim=claim,
        verification=verification,
    )


def _research() -> ResearchResult:
    supported_claim = VerificationClaim(
        claim_id=_identifier("claim", "c"),
        subject="测试企业",
        category="debt",
        statement="测试企业存在已披露的债务风险事项。",
    )
    supported_verification = ClaimVerification(
        claim=supported_claim,
        subject_match="EXACT",
        relation="SUPPORTS",
        evidence_excerpt="公告披露了债务风险事项。",
        evidence_location="paragraph:1",
        reason="正文直接支持该 Claim。",
        confidence=0.97,
        accepted=True,
        verifier="rules",
        verifier_model="rules-v1",
        prompt_version="test-v1",
    )
    supported = _evidence(
        source_id="source-supported-a",
        status="SUPPORTED",
        stage="VERIFIED",
        claim=supported_claim,
        verification=supported_verification,
    )
    unverified_claim = VerificationClaim(
        claim_id=_identifier("claim", "e"),
        subject="测试企业",
        category="debt",
        statement="测试企业存在未核验的债务风险事项。",
    )
    unverified = _evidence(
        source_id="source-unverified-e",
        status="UNVERIFIED",
        stage="CANDIDATE",
        claim=unverified_claim,
    )
    fact = ResearchFact(
        fact_id=_identifier("fact", "d"),
        subject="测试企业",
        category="debt",
        statement=supported_claim.statement,
        source_id=supported.source_id,
        source_url=supported.source_url,
        verification_status="SUPPORTED",
        claim_id=supported_claim.claim_id,
        relation="SUPPORTS",
        evidence_excerpt=supported_verification.evidence_excerpt,
        evidence_location=supported_verification.evidence_location,
        verifier_model="rules-v1",
        verified_at=supported_verification.verified_at,
    )
    company_result = ResearchQueryResult(
        query_type="company",
        query='"测试企业" 债务 逾期',
        found=True,
        facts=[fact],
        evidence=[supported, unverified],
        candidate_evidence=[supported, unverified],
        raw_result_count=2,
        candidate_found=True,
        content_fetch_status="COMPLETE",
        fetched_content_count=2,
        verification_execution_status="COMPLETE",
        completed_verification_count=1,
        verification_status="SUPPORTED",
        source="snapshot",
    )
    industry_result = ResearchQueryResult(
        query_type="industry",
        query='"测试行业" 监管 处罚',
        found=False,
        facts=[],
        content_fetch_status="NOT_NEEDED",
        verification_execution_status="NOT_NEEDED",
        verification_status="NOT_FOUND",
        source="snapshot",
    )
    return ResearchResult(
        company_name="测试企业",
        industry="测试行业",
        anomaly_flags=["DEBT_RATIO_RISING"],
        intent_id=_identifier("intent", "a"),
        query_plan_id=_identifier("query-plan", "b"),
        company_result=company_result,
        industry_result=industry_result,
        status="COMPLETE",
        verification_status="SUPPORTED",
        candidate_count=2,
        verified_fact_count=1,
        content_fetch_status="COMPLETE",
        fetched_content_count=2,
        verification_execution_status="COMPLETE",
        completed_verification_count=1,
    )


class _ResearchAnalysisModel:
    model_name = "mock-qwen"

    def __init__(
        self,
        *,
        promote_unverified: bool = False,
        proposal_factory: Any | None = None,
    ) -> None:
        self.promote_unverified = promote_unverified
        self.proposal_factory = proposal_factory
        self.calls: list[str] = []

    def generate(self, *, output_schema: type, purpose: str, payload: dict, **_: Any):
        self.calls.append(purpose)
        if purpose != "evidence_summary_query_proposal":
            raise AssertionError(f"unexpected purpose: {purpose}")
        evidence_summaries = []
        verified_ids = []
        for record in payload["evidence_records"]:
            status = record["verification_status"]
            if self.promote_unverified and status == "UNVERIFIED":
                status = "SUPPORTED"
            if record["verification_status"] in {"SUPPORTED", "CORROBORATED"}:
                verified_ids.append(record["evidence_id"])
            evidence_summaries.append(
                {
                    "evidence_id": record["evidence_id"],
                    "verification_status": status,
                    "summary": (
                        f"模型摘要：{record['fact_statement']}"
                        if status in {"SUPPORTED", "CORROBORATED"}
                        else None
                    ),
                }
            )
        output = output_schema.model_validate(
            {
                "verified_summary": "模型仅汇总已核验事实。",
                "summary_evidence_ids": verified_ids,
                "evidence_summaries": evidence_summaries,
                "gap_summaries": [
                    {
                        "gap_id": gap["gap_id"],
                        "summary": f"模型说明：{gap['reason']}。",
                    }
                    for gap in payload["evidence_gaps"]
                ],
                "query_proposals": (
                    self.proposal_factory(payload) if self.proposal_factory else []
                ),
                "limitations": ["不把候选、冲突或未核验证据写成事实。"],
            }
        )
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            latency_ms=3,
            input_tokens=120,
            output_tokens=60,
        )


def test_deterministic_research_analysis_never_calls_model_and_preserves_statuses() -> (
    None
):
    model = _ResearchAnalysisModel()
    plan = _plan()
    research = _research()
    before = plan.model_dump(mode="json")

    artifacts = build_research_analysis(
        research,
        plan,
        analysis_mode="deterministic",
        model=model,
        model_name=model.model_name,
    )

    assert model.calls == []
    assert artifacts.evidence_summary.execution_status == "NOT_REQUESTED"
    assert artifacts.query_proposal.execution_status == "NOT_REQUESTED"
    assert [
        entry.verification_status for entry in artifacts.evidence_summary.entries
    ] == [
        "SUPPORTED",
        "UNVERIFIED",
    ]
    assert artifacts.query_proposal.proposals == []
    assert plan.model_dump(mode="json") == before


def test_llm_summary_preserves_statuses_and_proposal_is_review_only() -> None:
    model = _ResearchAnalysisModel(
        proposal_factory=lambda payload: [
            {
                "query_type": "company",
                "category": "debt",
                "query": '"测试企业" 债务 展期',
                "reference_ids": [payload["evidence_records"][0]["evidence_id"]],
                "rationale": "围绕已核验债务证据补充人工复核线索。",
            }
        ]
    )
    plan = _plan()
    research = _research()
    before = plan.model_dump(mode="json")

    artifacts = build_research_analysis(
        research,
        plan,
        analysis_mode="llm",
        model=model,
        model_name=model.model_name,
    )

    summary = artifacts.evidence_summary
    proposal = artifacts.query_proposal
    assert model.calls == ["evidence_summary_query_proposal"]
    assert summary.execution_status == "COMPLETE"
    assert summary.summary_evidence_ids == [
        entry.evidence_id
        for entry in summary.entries
        if entry.verification_status == "SUPPORTED"
    ]
    assert summary.entries[0].summary.startswith("模型摘要：")
    assert summary.entries[1].verification_status == "UNVERIFIED"
    assert "UNVERIFIED" in summary.entries[1].summary
    assert proposal.proposals[0].decision == "ACCEPTED_FOR_REVIEW"
    assert proposal.proposals[0].rejection_reasons == []
    assert plan.model_dump(mode="json") == before


def test_status_promotion_degrades_both_artifacts_without_changing_research() -> None:
    model = _ResearchAnalysisModel(promote_unverified=True)
    research = _research()

    artifacts = build_research_analysis(
        research,
        _plan(),
        analysis_mode="llm",
        model=model,
        model_name=model.model_name,
    )

    assert artifacts.evidence_summary.execution_status == "DEGRADED"
    assert artifacts.evidence_summary.error_code == "UNSUPPORTED_EVIDENCE"
    assert artifacts.query_proposal.execution_status == "DEGRADED"
    assert artifacts.query_proposal.error_code == "UNSUPPORTED_EVIDENCE"
    assert artifacts.evidence_summary.entries[1].verification_status == "UNVERIFIED"
    assert (
        research.company_result.candidate_evidence[1].verification_status
        == "UNVERIFIED"
    )


def test_legacy_evidence_categories_are_canonicalized_before_gap_analysis() -> None:
    research = _research()
    supported = research.company_result.candidate_evidence[0]
    supported.category = "financing"
    research.company_result.evidence = [supported]
    research.company_result.candidate_evidence = [supported]
    research.company_result.facts[0].category = "financing"

    artifacts = build_research_analysis(
        research,
        _plan(),
        analysis_mode="deterministic",
        model=None,
        model_name="deterministic-template-v1",
    )

    entry = artifacts.evidence_summary.entries[0]
    assert entry.category == "debt"
    assert entry.source_category == "financing"
    assert [
        (gap.query_type, gap.category) for gap in artifacts.evidence_summary.gaps
    ] == [("industry", "regulatory")]


def test_untrusted_query_proposals_are_deterministically_rejected() -> None:
    def proposals(payload: dict[str, Any]) -> list[dict[str, Any]]:
        evidence_id = payload["evidence_records"][0]["evidence_id"]
        baseline = payload["allowed_queries"][0]["baseline_query"]
        return [
            {
                "query_type": "company",
                "category": "fraud",
                "query": '"测试企业" 舞弊',
                "reference_ids": [evidence_id],
                "rationale": "越权类别。",
            },
            {
                "query_type": "company",
                "category": "debt",
                "query": baseline,
                "reference_ids": [evidence_id],
                "rationale": "重复基线。",
            },
            {
                "query_type": "company",
                "category": "debt",
                "query": '"测试企业" 债务 重组',
                "reference_ids": ["source-not-in-index"],
                "rationale": "引用未知 ID。",
            },
            {
                "query_type": "company",
                "category": "debt",
                "query": '"测试企业" 债务 展期',
                "reference_ids": ["source-supported-a"],
                "rationale": "可供人工审阅。",
            },
            {
                "query_type": "company",
                "category": "debt",
                "query": '"测试企业" 债务 展期',
                "reference_ids": ["source-supported-a"],
                "rationale": "重复建议。",
            },
        ]

    plan = _plan()
    artifacts = build_research_analysis(
        _research(),
        plan,
        analysis_mode="llm",
        model=_ResearchAnalysisModel(proposal_factory=proposals),
        model_name="mock-qwen",
    )

    decisions = artifacts.query_proposal.proposals
    assert decisions[0].rejection_reasons == ["UNAUTHORIZED_CATEGORY"]
    assert decisions[1].rejection_reasons == ["DUPLICATE_BASELINE_QUERY"]
    assert decisions[2].rejection_reasons == ["UNKNOWN_REFERENCE"]
    assert decisions[3].decision == "ACCEPTED_FOR_REVIEW"
    assert decisions[4].rejection_reasons == ["DUPLICATE_PROPOSAL"]
    assert [request.query for request in plan.queries] == [
        '"测试企业" 债务 逾期',
        '"测试行业" 监管 处罚',
    ]


class _MainChainModel(_ResearchAnalysisModel):
    """One deterministic fake that supports both M4-B and the existing M4-A call."""

    def generate(
        self, *, output_schema: type, purpose: str, payload: dict, **kwargs: Any
    ):
        if purpose == "evidence_summary_query_proposal":
            return super().generate(
                output_schema=output_schema,
                purpose=purpose,
                payload=payload,
                **kwargs,
            )
        self.calls.append(purpose)
        if purpose != "risk_narrative":
            raise AssertionError(f"unexpected purpose: {purpose}")
        evidence_ids = [
            evidence_id
            for flag in payload["risk_flags"]
            for evidence_id in flag["allowed_evidence_ids"][:1]
        ]
        output = output_schema.model_validate(
            {
                "overall_summary": "模型风险摘要。",
                "summary_evidence_ids": evidence_ids,
                "explanations": [
                    {
                        "risk_id": flag["risk_id"],
                        "explanation": f"模型解释：{flag['description']}",
                        "evidence_ids": flag["allowed_evidence_ids"][:1],
                    }
                    for flag in payload["risk_flags"]
                ],
                "limitations": ["只使用提供的风险证据。"],
            }
        )
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            latency_ms=3,
            input_tokens=100,
            output_tokens=50,
        )


def test_llm_research_artifacts_enter_durable_main_chain_without_changing_hitl(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    shutil.copytree(
        PROJECT_ROOT / "data/case_risky/source",
        data_dir / "case_risky/source",
    )
    settings = Settings(
        _env_file=None,
        analysis_mode="llm",
        model_name="mock-qwen",
        model_api_key="test-key",
        data_dir=data_dir,
        checkpoint_db_path=tmp_path / "credra.db",
        trace_dir=tmp_path / "traces",
    )
    model = _MainChainModel()

    payload = start_task(
        thread_id="m4b-main-chain",
        case_id="case_risky",
        settings=settings,
        research_client=ResearchMCPClient(research_mcp),
        analysis_model=model,
    )

    assert payload["state"]["status"] == "WAITING_APPROVAL"
    assert payload["next"] == ["approval"]
    assert payload["state"]["evidence_summary_artifact"] == (
        "artifacts/evidence_summary_v1.json"
    )
    assert payload["state"]["query_proposal_artifact"] == (
        "artifacts/query_proposal_v1.json"
    )
    run_dir = data_dir / "case_risky/runs" / payload["state"]["run_id"]
    summary = json.loads(
        (run_dir / "artifacts/evidence_summary_v1.json").read_text(encoding="utf-8")
    )
    proposals = json.loads(
        (run_dir / "artifacts/query_proposal_v1.json").read_text(encoding="utf-8")
    )
    plan = json.loads(
        (run_dir / "artifacts/query_plan_v1.json").read_text(encoding="utf-8")
    )
    assert summary["execution_status"] == "COMPLETE"
    assert proposals["execution_status"] == "COMPLETE"
    assert proposals["allowed_categories"] == plan["intent"]["categories"]
    assert ("company", "debt") not in {
        (gap["query_type"], gap["category"]) for gap in summary["gaps"]
    }
    assert model.calls == ["evidence_summary_query_proposal", "risk_narrative"]
    events = read_trace(tmp_path / "traces/m4b-main-chain.jsonl")
    research_events = [
        event
        for event in events
        if event["event_type"] == "LLM_CALL"
        and event["input_summary"] == "purpose=evidence_summary_query_proposal"
    ]
    assert len(research_events) == 1
    assert "execution=COMPLETE" in research_events[0]["output_summary"]
    serialized_trace = json.dumps(events, ensure_ascii=False)
    assert "test-key" not in serialized_trace
    assert "模型仅汇总已核验事实" not in serialized_trace
