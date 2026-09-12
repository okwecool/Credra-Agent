"""P22 offline controls: evidence changes decisions, budget and durable recovery."""

import json
from dataclasses import replace
from datetime import date

import httpx
import pytest
from pydantic import ValidationError

from app.llm.gateway import StructuredModelError, StructuredModelResult
from app.mcp.research_client import ResearchMCPClient
from app.mcp.research_server import _search_evidence
from app.mcp.research_server import mcp as research_mcp
from app.models.content import FetchedDocument
from app.models.research import ResearchQueryResult
from app.runtime.tasks import resume_agentic_task, run_id_for_thread, start_agentic_task
from app.search.content import ContentSnapshotStore, HTTPContentFetcher
from app.tools.artifacts import ArtifactStore
from credra_agent.evidence.artifacts import write_evidence_artifacts
from credra_agent.evidence.investigation import (
    EvidenceVerdict,
    InvestigationActions,
    VerificationDraft,
)
from credra_agent.evidence.models import EntityRelationAssertion, EntityRelationship
from credra_agent.evidence.service import assemble_bundle
from credra_agent.execution.executor import ExecutionContext
from credra_agent.execution.models import (
    ReferenceArgs,
    SearchEvidenceArgs,
    VerifyClaimArgs,
)
from credra_agent.intent.models import SourcePolicy
from credra_agent.planning.evidence_context import (
    assess_questions,
    coordinator_evidence_context,
    current_conflicts,
    store_proposals,
)
from credra_agent.planning.models import (
    ClaimProposal,
    CoordinatorLimits,
    DecisionDraft,
    QuestionAssessment,
)
from credra_agent.runtime.executors import build_agentic_executor, build_agentic_model
from tests.test_v2_agentic_recovery import (
    FailOnce,
    FixedModel,
    authorization,
    settings,
    task_spec,
)
from tests.test_v2_evidence import (
    AS_OF,
    CHILD,
    PARENT,
    bundle,
    claim,
    digest,
    document,
    verified,
)


def spec():
    return task_spec().model_copy(
        update={
            "subject_id": PARENT.entity_id,
            "subject_name": PARENT.legal_name,
            "as_of": AS_OF,
            "source_policy": SourcePolicy(
                allowed=["regulator"], denied=["social_media"]
            ),
        }
    )


def caps(**updates):
    return authorization().model_copy(
        update={
            "external_request_limit": 30,
            "token_limit": 3000,
            "limits": CoordinatorLimits(
                max_decisions=10,
                no_progress_limit=3,
                model_attempt_reservation=2,
                decision_token_reservation=100,
                decision_max_output_tokens=50,
            ),
            **updates,
        }
    )


def doc(identifier="original", **updates):
    return document(identifier, **{"source_tags": ["regulator"], **updates})


def context(tmp_path, source=None):
    store = ArtifactStore(tmp_path)
    source = source or bundle([doc()], [])
    refs = write_evidence_artifacts(
        store, source, version=0, as_of=AS_OF, subject_id=PARENT.entity_id
    )
    return ExecutionContext(store, spec(), caps().limits, set(refs))


def args(ctx, ids=None):
    return VerifyClaimArgs(
        claim_id="claim-control",
        source_refs=[
            next(
                ref
                for ref in ctx.available_refs
                if ref.startswith("artifacts/agent_evidence_v")
            )
        ],
        document_ids=ids or [],
    )


class Verifier:
    model_name = "offline-semantic-control"

    def __init__(self, transform=None, *, unknown_usage=False, error=False):
        self.calls = 0
        self.transform = transform
        self.unknown_usage, self.error = unknown_usage, error

    def generate(self, **kwargs):
        self.calls += 1
        assert kwargs["max_output_tokens"] == 50
        assert kwargs["payload"]["untrusted_input"]
        if self.error:
            raise StructuredModelError(
                "INVALID_OUTPUT", "offline invalid output", attempts=2
            )
        rows = []
        for material in kwargs["payload"]["documents"]:
            fragment = material["fragments"][0]
            rows.append(
                EvidenceVerdict(
                    document_id=material["document_id"],
                    subject_id=PARENT.entity_id,
                    relation="REFUTES"
                    if "并非全部" in fragment["text"]
                    else "SUPPORTS",
                    excerpt=fragment["text"],
                    location=fragment["location"],
                    entity_excerpt=PARENT.legal_name,
                    entity_location=fragment["location"],
                    reason="合成控制中的明确断言及法人名称。",
                )
            )
        output = VerificationDraft(results=rows)
        if self.transform:
            output = self.transform(output)
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            latency_ms=1,
            input_tokens=8,
            output_tokens=12,
            accounting_complete=not self.unknown_usage,
        )


class SearchClient:
    def __init__(self):
        self.calls = []

    async def search_evidence(self, arguments):
        self.calls.append(arguments)
        return ResearchQueryResult(
            query_type="company", query=arguments.query, found=False, facts=[]
        )


class EvidenceDrivenCoordinator:
    """Choose from current text/status, rather than consume a fixed action list."""

    model_name = "offline-evidence-coordinator"

    def __init__(self):
        self.calls, self.actions = 0, []

    def generate(self, **kwargs):
        self.calls += 1
        payload = kwargs["payload"]
        ctx = payload["evidence_context"]
        latest = ctx["bundles"][-1]
        documents = {row["document_id"]: row for row in latest["documents"]}
        read_ids = {row["document_id"] for row in ctx["document_reads"]}
        claims = {
            row["claim_id"]: row for item in ctx["bundles"] for row in item["claims"]
        }
        status = claims.get("proposal:arrears", {}).get("status", "UNVERIFIED")
        proposal = ClaimProposal(
            proposal_id="proposal:arrears",
            question_id="q-regulatory",
            statement=claim().statement,
            kind="HYPOTHESIS",
        )
        if "original" not in read_ids:
            tool, arguments = (
                "read_document",
                {"reference_id": latest["reference"], "document_id": "original"},
            )
        elif status == "UNVERIFIED":
            tool, arguments = (
                "verify_claim",
                {
                    "claim_id": proposal.proposal_id,
                    "source_refs": [latest["reference"]],
                    "document_ids": ["original"],
                },
            )
        elif "clarification" in documents and "clarification" not in read_ids:
            tool, arguments = (
                "read_document",
                {"reference_id": latest["reference"], "document_id": "clarification"},
            )
        elif "clarification" in documents and status == "SUPPORTED":
            tool, arguments = (
                "verify_claim",
                {
                    "claim_id": proposal.proposal_id,
                    "source_refs": [latest["reference"]],
                    "document_ids": ["clarification"],
                },
            )
        elif status == "CONFLICTING" and "search_evidence" not in self.actions:
            tool, arguments = (
                "search_evidence",
                {
                    "query": "示例甲 应收逾期 独立调查 澄清",
                    "subject_id": PARENT.entity_id,
                    "subject_name": PARENT.legal_name,
                    "period": payload["task_spec"]["periods"][0],
                    "source_policy": payload["task_spec"]["source_policy"],
                    "category": "regulatory",
                },
            )
        else:
            claim_bundle = next(
                item
                for item in reversed(ctx["bundles"])
                if any(
                    row["claim_id"] == proposal.proposal_id for row in item["claims"]
                )
            )
            conflicting = status == "CONFLICTING"
            output = DecisionDraft(
                decision="FINISH",
                finish_reason="NEEDS_REVIEW" if conflicting else "ANSWERED",
                reason_summary="原文存在反证，检索后仍有缺口。"
                if conflicting
                else "明确原文支持指定断言。",
                question_assessments=[
                    QuestionAssessment(
                        question_id="q-regulatory",
                        status="UNRESOLVED" if conflicting else "ANSWERED",
                        conclusion="合成资料之间存在冲突。"
                        if conflicting
                        else "合成资料支持全部逾期断言。",
                        evidence_refs=[claim_bundle["reference"]],
                        claim_ids=[proposal.proposal_id],
                        limitations=["独立来源未检出，不能消除冲突。"]
                        if conflicting
                        else [],
                    )
                ],
                limitations=["合成控制，不能用作真实企业结论。"],
            )
            return self.result(output)
        self.actions.append(tool)
        return self.result(
            DecisionDraft(
                decision="ACTION",
                tool=tool,
                arguments=arguments,
                expected_observation="取得原文、核验关系或独立来源缺口。",
                reason_summary="根据当前原文及主张状态选择下一步。",
                claim_proposals=[proposal] if not ctx["claim_proposals"] else [],
            )
        )

    def result(self, output):
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            latency_ms=1,
            input_tokens=8,
            output_tokens=12,
            accounting_complete=True,
        )


@pytest.mark.parametrize("conflicting", [False, True])
def test_evidence_changes_route_and_completion(tmp_path, conflicting):
    documents = [doc()]
    if conflicting:
        documents.append(
            doc(
                "clarification",
                text=PARENT.legal_name + "并非全部应收账款已逾期。",
                original_publisher="合成监管乙",
            )
        )
    seed = bundle(documents, [])
    model, verifier, client = EvidenceDrivenCoordinator(), Verifier(), SearchClient()
    config = settings(tmp_path)
    tools = build_agentic_executor(
        spec(), research_client=client, verifier_model=verifier
    )
    result = start_agentic_task(
        thread_id="control-p22",
        task_spec=spec(),
        authorization=caps(),
        settings=config,
        model=model,
        executor=tools,
        initial_evidence_bundle=seed,
    )
    state = result["state"]
    assert state["status"] == ("LIMITED" if conflicting else "COMPLETED")
    assert model.actions == (
        [
            "read_document",
            "verify_claim",
            "read_document",
            "verify_claim",
            "search_evidence",
        ]
        if conflicting
        else ["read_document", "verify_claim"]
    )
    assert verifier.calls == (2 if conflicting else 1)
    assert len(client.calls) == int(conflicting)
    store = ArtifactStore(
        config.data_dir / spec().case_id / "runs" / run_id_for_thread("control-p22")
    )
    coverage = store.read_json(state["coverage_ref"])
    assert coverage["answered_question_ids"] == (
        [] if conflicting else ["q-regulatory"]
    )
    assert coverage["unresolved_conflict_ids"] == (
        ["proposal:arrears"] if conflicting else []
    )
    ledger = store.read_json(state["budget_ledger_ref"])
    assert ledger["external_spent"] == model.calls + verifier.calls + len(client.calls)
    assert ledger["token_spent"] == 20 * (model.calls + verifier.calls)


@pytest.mark.parametrize(
    "variation", ["entity", "excerpt", "date", "missing-publication"]
)
def test_verifier_cannot_accept_ungrounded_output(tmp_path, variation):
    source = doc(
        **(
            {"published_at": date(2026, 7, 1)}
            if variation == "date"
            else {"published_at": None}
            if variation == "missing-publication"
            else {}
        )
    )
    ctx = context(tmp_path, bundle([source], []))

    def mutate(draft):
        if variation == "entity":
            draft.results[0].subject_id = CHILD.entity_id
        if variation == "excerpt":
            draft.results[0].excerpt = "不存在的关键事实。"
        return draft

    outcome = InvestigationActions(model=Verifier(mutate)).verify(args(ctx), ctx)
    assert outcome.status == "SUCCESS"
    assert outcome.evidence_bundle.claims[0].status == "UNVERIFIED"
    assert variation == "date" or not any(
        row.verification_status == "ACCEPTED"
        for row in outcome.evidence_bundle.evidence
    )
    assert outcome.actual_external_requests == 1 and outcome.actual_tokens == 20


def test_model_cannot_forge_receipt_or_rebind_proposal(tmp_path):
    with pytest.raises(ValidationError):
        EvidenceVerdict(
            document_id="original",
            relation="SUPPORTS",
            reason="control",
            verification_status="ACCEPTED",
        )
    ctx = context(tmp_path)
    proposal = ClaimProposal(
        proposal_id="proposal:control",
        question_id="q-regulatory",
        statement="合成断言",
        kind="HYPOTHESIS",
    )
    ref = store_proposals(
        ctx.artifacts,
        [proposal],
        references=ctx.available_refs,
        task=ctx.task_spec,
        version=1,
    )
    with pytest.raises(ValueError, match="immutable"):
        store_proposals(
            ctx.artifacts,
            [proposal.model_copy(update={"statement": "另一断言"})],
            references=ctx.available_refs | {ref},
            task=ctx.task_spec,
            version=2,
        )


@pytest.mark.parametrize("same_origin", [False, True])
def test_independence_requires_semantics_and_origin_gate(tmp_path, same_origin):
    ctx = context(
        tmp_path,
        bundle(
            [
                doc(),
                doc(
                    "second",
                    text=PARENT.legal_name + "全部应收已逾期。",
                    original_publisher="合成监管乙",
                ),
            ],
            [],
        ),
    )

    def mutate(draft):
        draft.independent_document_ids = ["original", "second"]
        draft.independence_reason = "合成控制：明确独立调查。"
        if same_origin:
            draft.results[1].depends_on_document_id = "original"
        return draft

    outcome = InvestigationActions(model=Verifier(mutate)).verify(args(ctx), ctx)
    assert bool(outcome.evidence_bundle.corroborations) is not same_origin
    if same_origin:
        assert "INDEPENDENCE_NOT_ACCEPTED" in outcome.payload["rejected"]


def test_selected_subset_does_not_erase_counterevidence_and_obsolete_answer_fails(
    tmp_path,
):
    ctx = context(
        tmp_path,
        bundle(
            [doc(), doc("clarification", text=PARENT.legal_name + "并非全部逾期。")], []
        ),
    )
    actions = InvestigationActions(model=Verifier())
    first = actions.verify(args(ctx, ["original"]), ctx)
    first_refs = write_evidence_artifacts(
        ctx.artifacts,
        first.evidence_bundle,
        version=1,
        as_of=AS_OF,
        subject_id=PARENT.entity_id,
    )
    ctx = ExecutionContext(
        ctx.artifacts, ctx.task_spec, ctx.limits, ctx.available_refs | set(first_refs)
    )
    second = actions.verify(args(ctx, ["clarification"]), ctx)
    second_refs = write_evidence_artifacts(
        ctx.artifacts,
        second.evidence_bundle,
        version=2,
        as_of=AS_OF,
        subject_id=PARENT.entity_id,
    )
    ctx = ExecutionContext(
        ctx.artifacts, ctx.task_spec, ctx.limits, ctx.available_refs | set(second_refs)
    )
    third = actions.verify(args(ctx, ["original"]), ctx)
    assert third.evidence_bundle.claims[0].status == "CONFLICTING"
    for ref in [first_refs[0], second_refs[0]]:
        assessment = QuestionAssessment(
            question_id="q-regulatory",
            status="ANSWERED",
            conclusion="控制结论",
            evidence_refs=[ref],
            claim_ids=["claim-control"],
        )
        with pytest.raises(ValueError):
            assess_questions(
                ctx.artifacts,
                [assessment],
                references=ctx.available_refs,
                task=ctx.task_spec,
            )
    assert current_conflicts(
        ctx.artifacts, ctx.available_refs, ctx.task_spec, ["legacy-unknown"]
    ) == ["claim-control", "legacy-unknown"]


@pytest.mark.parametrize("unknown_usage,error", [(True, False), (False, True)])
def test_unknown_usage_and_failed_attempts_remain_conservative(
    tmp_path, unknown_usage, error
):
    ctx = context(tmp_path)
    outcome = InvestigationActions(
        model=Verifier(unknown_usage=unknown_usage, error=error)
    ).verify(args(ctx), ctx)
    assert outcome.actual_tokens is None
    assert outcome.actual_external_requests == (2 if error else None)


def test_source_policy_blocks_fetch_before_callback_and_read_truncates(tmp_path):
    ctx = context(
        tmp_path,
        bundle(
            [doc(url="https://example.test/article", source_tags=["social_media"])], []
        ),
    )
    calls = []
    outcome = InvestigationActions(
        model=None, fetcher=lambda url: calls.append(url)
    ).fetch(ReferenceArgs(reference_id=args(ctx).source_refs[0]), ctx)
    assert outcome.status == "MISSING_DATA" and calls == []
    long = doc(text=PARENT.legal_name + "合成正文" * 3000)
    ctx = context(tmp_path / "long", bundle([long], []))
    outcome = InvestigationActions(model=None).read(
        ReferenceArgs(reference_id=args(ctx).source_refs[0]), ctx
    )
    assert (
        outcome.payload["truncated"]
        and len(outcome.payload["fragments"][0]["text"]) == 8000
    )
    for version in range(1, 6):
        ref = ctx.artifacts.write_json(
            f"artifacts/agent_tool_result_v{version}.json", outcome.payload
        )
        ctx.available_refs.add(ref)
    visible = coordinator_evidence_context(
        ctx.artifacts, ctx.available_refs, ctx.task_spec
    )
    assert visible["context_truncated"]
    assert len(json.dumps(visible, ensure_ascii=False)) <= 18000


def test_fetch_preserves_hash_scope_and_unknown_http_usage(tmp_path):
    url = "https://example.test/article"
    ctx = context(tmp_path, bundle([doc(url=url, fragments=[])], []))
    text = PARENT.legal_name + "合成原文。"
    fetched = FetchedDocument(
        requested_url=url,
        final_url=url,
        status="SUCCESS",
        content_kind="HTML",
        document_hash=digest(text),
        segments=[{"location": "p:1", "text": text}],
    )
    outcome = InvestigationActions(
        model=None,
        fetcher=lambda _: fetched,
        fetch_external_requests=4,
        fetch_actual_external_requests=None,
    ).fetch(ReferenceArgs(reference_id=args(ctx).source_refs[0]), ctx)
    assert outcome.status == "SUCCESS" and outcome.actual_external_requests is None
    assert outcome.evidence_bundle.documents[-1].hash_scope == "EXTRACTED_TEXT"
    assert len(outcome.evidence_bundle.documents) == 2


def test_agent_fetch_never_follows_redirect_to_unclassified_host(tmp_path):
    requests = []

    def response(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://other.test/redirect"})

    fetcher = HTTPContentFetcher(
        timeout_seconds=1,
        max_bytes=1000,
        max_redirects=3,
        max_pdf_pages=1,
        max_text_chars=1000,
        snapshot_store=ContentSnapshotStore(tmp_path),
        client=httpx.Client(transport=httpx.MockTransport(response)),
        resolver=lambda _: ["93.184.216.34"],
        restrict_redirect_host=True,
    )
    assert fetcher.fetch("https://original.test/a").error_code == "INVALID_URL"
    assert requests == ["https://original.test/a"]


@pytest.mark.parametrize(
    "boundary", ["after_action_reserved", "after_request", "after_result_stored"]
)
def test_verification_recovery_windows_never_duplicate_request(tmp_path, boundary):
    seed = bundle([doc()], [])

    class Coordinator(EvidenceDrivenCoordinator):
        def generate(self, **kwargs):
            if self.calls == 0:
                self.calls += 1
                return self.result(
                    DecisionDraft(
                        decision="ACTION",
                        tool="verify_claim",
                        arguments={
                            "claim_id": "claim-control",
                            "source_refs": ["artifacts/agent_evidence_v0.json"],
                        },
                        expected_observation="核验合成断言。",
                        reason_summary="直接核验已登记主张。",
                    )
                )
            self.calls += 1
            ref = next(
                item["reference"]
                for item in reversed(kwargs["payload"]["evidence_context"]["bundles"])
                if item["claims"]
            )
            return self.result(
                DecisionDraft(
                    decision="FINISH",
                    finish_reason="ANSWERED",
                    reason_summary="依据原文核验完成。",
                    question_assessments=[
                        QuestionAssessment(
                            question_id="q-regulatory",
                            status="ANSWERED",
                            conclusion="合成断言获得支持。",
                            evidence_refs=[ref],
                            claim_ids=["claim-control"],
                        )
                    ],
                )
            )

    model, verifier = Coordinator(), Verifier()
    config = settings(tmp_path)
    tools = build_agentic_executor(
        spec(), research_client=SearchClient(), verifier_model=verifier
    )
    with pytest.raises(RuntimeError, match=boundary):
        start_agentic_task(
            thread_id="p22-recovery",
            task_spec=spec(),
            authorization=caps(),
            settings=config,
            model=model,
            executor=tools,
            initial_evidence_bundle=seed,
            fault_hook=FailOnce(boundary),
        )
    result = resume_agentic_task(
        thread_id="p22-recovery",
        settings=config,
        model=model,
        executor=tools,
    )
    assert result["state"]["status"] == (
        "LIMITED" if boundary == "after_request" else "COMPLETED"
    )
    assert verifier.calls == 1


def test_verifier_budget_is_reserved_before_dispatch(tmp_path):
    model = EvidenceDrivenCoordinator()
    verifier = Verifier()
    tools = build_agentic_executor(
        spec(), research_client=SearchClient(), verifier_model=verifier
    )
    result = start_agentic_task(
        thread_id="p22-budget",
        task_spec=spec(),
        authorization=caps(token_limit=120),
        settings=settings(tmp_path),
        model=model,
        executor=tools,
        initial_evidence_bundle=bundle([doc()], []),
    )
    assert result["state"]["status"] == "LIMITED" and verifier.calls == 0


@pytest.mark.asyncio
async def test_candidate_mcp_never_fetches_or_verifies(monkeypatch):
    from app.config import Settings

    request = SearchEvidenceArgs(
        query="迅驰供应链科技有限公司 监管处罚",
        subject_id="control-id",
        subject_name="迅驰供应链科技有限公司",
        period={"start": "2024-01-01", "end": "2025-12-31"},
        source_policy={"preferred": [], "allowed": None, "denied": []},
        category="regulatory",
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("candidate search must not construct downstream services")

    monkeypatch.setattr("app.mcp.research_server.build_content_fetcher", forbidden)
    monkeypatch.setattr("app.mcp.research_server.build_fact_verifier", forbidden)
    direct = _search_evidence(
        request, settings=Settings(_env_file=None), candidates_only=True
    )
    assert (
        direct.fetched_content_count == 0 and direct.completed_verification_count == 0
    )
    assert direct.facts == []
    returned = await ResearchMCPClient(research_mcp).search_candidates(request)
    assert returned.facts == [] and returned.completed_verification_count == 0


def test_runtime_model_attempts_follow_authorization_with_two_stage_accounting(
    monkeypatch,
):
    from app.config import Settings

    received = []
    monkeypatch.setattr(
        "credra_agent.runtime.executors.build_analysis_model",
        lambda configured, **kwargs: received.append((configured, kwargs)),
    )
    configured = Settings(
        _env_file=None,
        analysis_mode="llm",
        model_api_key="test-placeholder",
        analysis_llm_max_retry=5,
        analysis_llm_enable_thinking=False,
    )
    build_agentic_model(configured, caps().limits)
    assert received[0][0].analysis_llm_max_retry == 1
    assert received[0][1] == {
        "aggregate_accounting": True,
        "process_max_output_tokens": 50,
    }
    assert configured.analysis_llm_max_retry == 5
    build_agentic_model(
        configured.model_copy(update={"analysis_llm_enable_thinking": True}),
        caps().limits,
    )
    assert received[1][0].analysis_llm_enable_thinking is True
    assert received[1][0].analysis_llm_thinking_budget_tokens <= 50


def test_verification_preserves_existing_entity_and_independence_receipts(tmp_path):
    ownership = claim(
        claim_id="ownership",
        subject_id=CHILD.entity_id,
        statement=CHILD.legal_name + "是" + PARENT.legal_name + "子公司。",
        kind="REPORTED_FACT",
        entity_relation=EntityRelationAssertion(
            related_entity_id=PARENT.entity_id, kind="SUBSIDIARY_OF"
        ),
    )
    ownership_doc = doc("ownership", text=ownership.statement)
    receipt = verified(ownership_doc, asserted=ownership)
    relationship = EntityRelationship(
        relationship_id="child-parent",
        subject_id=CHILD.entity_id,
        related_entity_id=PARENT.entity_id,
        kind="SUBSIDIARY_OF",
        evidence_ids=[receipt.evidence_id],
        verifier_version="offline-relationship",
    )
    source = assemble_bundle(
        as_of=AS_OF,
        entities=[PARENT, CHILD],
        documents=[
            ownership_doc,
            doc(),
            doc(
                "second",
                text=PARENT.legal_name + "全部应收已逾期。",
                original_publisher="合成乙",
            ),
        ],
        claims=[ownership, claim()],
        evidence=[receipt],
        relationships=[relationship],
    )
    ctx = context(tmp_path, source)

    def independence(draft):
        draft.independent_document_ids = ["original", "second"]
        draft.independence_reason = "两个合成独立调查回执。"
        return draft

    first = InvestigationActions(model=Verifier(independence)).verify(
        args(ctx, ["original", "second"]), ctx
    )
    assert first.status == "SUCCESS" and first.evidence_bundle.relationships == [
        relationship
    ]
    refs = write_evidence_artifacts(
        ctx.artifacts,
        first.evidence_bundle,
        version=1,
        as_of=AS_OF,
        subject_id=PARENT.entity_id,
    )
    ctx = ExecutionContext(
        ctx.artifacts, ctx.task_spec, ctx.limits, ctx.available_refs | set(refs)
    )
    second = InvestigationActions(model=Verifier()).verify(args(ctx, ["original"]), ctx)
    assert second.evidence_bundle.relationships == [relationship]
    assert second.evidence_bundle.corroborations == first.evidence_bundle.corroborations


@pytest.mark.parametrize("request_limit", [7, 30])
def test_two_stage_calls_share_budget_and_reserve_before_verification(
    tmp_path, request_limit
):
    class TwoStageCoordinator(EvidenceDrivenCoordinator):
        request_reservation_multiplier = token_reservation_multiplier = 2

        def result(self, output):
            return replace(
                super().result(output),
                input_tokens=16,
                output_tokens=24,
                external_requests=2,
            )

    class TwoStageVerifier(Verifier):
        request_reservation_multiplier = token_reservation_multiplier = 2

        def generate(self, **kwargs):
            return replace(
                super().generate(**kwargs),
                input_tokens=16,
                output_tokens=24,
                external_requests=2,
            )

    model, verifier = TwoStageCoordinator(), TwoStageVerifier()
    tools = build_agentic_executor(
        spec(), research_client=SearchClient(), verifier_model=verifier
    )
    config = settings(tmp_path)
    result = start_agentic_task(
        thread_id="two-stage",
        task_spec=spec(),
        authorization=caps(external_request_limit=request_limit),
        settings=config,
        model=model,
        executor=tools,
        initial_evidence_bundle=bundle([doc()], []),
    )
    assert result["state"]["status"] == (
        "LIMITED" if request_limit == 7 else "COMPLETED"
    )
    assert verifier.calls == (0 if request_limit == 7 else 1)
    store = ArtifactStore(
        config.data_dir / spec().case_id / "runs" / run_id_for_thread("two-stage")
    )
    budget = store.read_json(result["state"]["budget_ledger_ref"])
    assert budget["external_spent"] == 2 * (model.calls + verifier.calls)
    assert budget["token_spent"] == 40 * (model.calls + verifier.calls)


def test_resume_rejects_increased_request_profile_before_dispatch(tmp_path):
    decision = DecisionDraft(
        decision="ACTION",
        tool="verify_claim",
        arguments={
            "claim_id": "claim-control",
            "source_refs": ["artifacts/agent_evidence_v0.json"],
        },
        reason_summary="核验已登记的合成主张。",
        expected_observation="取得核验回执。",
    )
    model, verifier = FixedModel([decision]), Verifier()
    config = settings(tmp_path)
    tools = build_agentic_executor(
        spec(), research_client=SearchClient(), verifier_model=verifier
    )
    with pytest.raises(RuntimeError, match="after_action_reserved"):
        start_agentic_task(
            thread_id="changed-profile",
            task_spec=spec(),
            authorization=caps(),
            settings=config,
            model=model,
            executor=tools,
            initial_evidence_bundle=bundle([doc()], []),
            fault_hook=FailOnce("after_action_reserved"),
        )
    verifier.request_reservation_multiplier = verifier.token_reservation_multiplier = 2
    changed = build_agentic_executor(
        spec(), research_client=SearchClient(), verifier_model=verifier
    )
    result = resume_agentic_task(
        thread_id="changed-profile", settings=config, model=model, executor=changed
    )
    assert result["state"]["stop_reason"] == "MODEL_REQUEST_PROFILE_CHANGED"
    assert verifier.calls == 0 and model.calls == 1
