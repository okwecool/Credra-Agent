"""P21 controls: grounding, scope, dependency and durable compatibility."""

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.models.case import SourceManifest
from app.models.content import FetchedContentReference, FetchedDocument
from app.models.research import ResearchFact, ResearchQueryResult
from app.models.search import ResearchEvidence
from app.models.verification import ClaimVerification, VerificationClaim
from app.tools.artifacts import ArtifactStore
from credra_agent.evidence.adapters import from_research_result, from_source_manifest
from credra_agent.evidence.artifacts import (
    validate_evidence_artifacts,
    write_evidence_artifacts,
)
from credra_agent.evidence.materials import from_p20_catalog
from credra_agent.evidence.models import (
    Claim,
    Corroboration,
    Document,
    Entity,
    EntityRelationAssertion,
    EntityRelationship,
    EvidenceBundle,
    EvidenceDraft,
    Fragment,
)
from credra_agent.evidence.service import (
    accept_evidence,
    active_evidence,
    assemble_bundle,
    match_entity,
    summarize_bundle,
)
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.execution.models import SearchEvidenceArgs
from credra_agent.runtime.executors import build_agentic_executor
from tests.test_v2_agentic_recovery import (
    FailOnce,
    FixedModel,
    authorization,
    finish_decision,
    search_decision,
    settings,
    task_spec,
)

ROOT = Path(__file__).resolve().parents[1]
AS_OF = date(2026, 6, 30)
RETRIEVED = datetime(2026, 9, 11, tzinfo=UTC)
PARENT = Entity(
    entity_id="control-parent", legal_name="示例甲股份有限公司", aliases=["示例甲"]
)
CHILD = Entity(entity_id="control-child", legal_name="示例甲汽车工业有限公司")


def digest(text):
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def claim(**updates):
    return Claim.model_validate(
        {
            "claim_id": "claim-control",
            "subject_id": PARENT.entity_id,
            "statement": "示例甲股份有限公司全部应收账款已逾期。",
            "kind": "HYPOTHESIS",
            "source_kind": "SYNTHETIC",
            **updates,
        }
    )


def document(identifier="original", text=None, **updates):
    text = text or "调查记录：示例甲股份有限公司全部应收账款已逾期。"
    return Document.model_validate(
        {
            "document_id": identifier,
            "original_source_id": identifier,
            "original_publisher": "调查机构甲",
            "title": "合成控制资料",
            "published_at": date(2025, 6, 11),
            "retrieved_at": RETRIEVED,
            "document_hash": digest(text),
            "hash_scope": "EXTRACTED_TEXT",
            "source_kind": "SYNTHETIC",
            "access_scope": "FULL",
            "fragments": [
                {
                    "location": "page:4",
                    "physical_page": 4,
                    "printed_page": "1",
                    "text": text,
                }
            ],
            **updates,
        }
    )


def verified(doc, *, asserted=None, relation="SUPPORTS", origin=None, **updates):
    asserted = asserted or claim()
    draft = EvidenceDraft(
        evidence_id=f"evidence-{doc.document_id}",
        document_id=doc.document_id,
        claim_id=asserted.claim_id,
        subject_id=asserted.subject_id,
        relation=relation,
        excerpt=doc.fragments[0].text,
        location=doc.fragments[0].location,
    )
    result = accept_evidence(
        draft,
        document=doc,
        claim=asserted,
        verifier_version="synthetic-control-verifier-v1",
        scope_subject_id=asserted.subject_id,
        entity_scope_verifier_version="synthetic-entity-v1",
        origin_group_id=origin,
    )
    return type(result).model_validate({**result.model_dump(), **updates})


def bundle(documents, evidence, *, asserted=None, **updates):
    return assemble_bundle(
        as_of=AS_OF,
        entities=[PARENT, CHILD],
        documents=documents,
        evidence=evidence,
        claims=[asserted or claim()],
        **updates,
    )


def control_bundle():
    fixture = json.loads(
        (ROOT / "tests/fixtures/v2_evidence/synthetic_sources.json").read_text(
            encoding="utf-8"
        )
    )
    documents = [
        document(row["id"], row["text"], original_publisher=row["publisher"])
        for row in fixture["records"]
    ]
    evidence = [
        verified(doc, relation=row["relation"], origin=row["origin"])
        for doc, row in zip(documents, fixture["records"], strict=True)
    ]
    return bundle(documents, evidence)


def test_synthetic_conflict_preserves_repost_and_party_attribution():
    result = control_bundle()
    assert result.claims[0].status == "CONFLICTING"
    assert result.claims[0].supporting_evidence_ids == [
        "evidence-original",
        "evidence-repost",
    ]
    assert result.claims[0].refuting_evidence_ids == ["evidence-clarification"]
    summary = summarize_bundle(result)
    assert summary["conflict_ids"] == ["claim-control"]
    assert len(summary["origin_groups"]) == 2
    assert summary["independently_corroborated_claim_ids"] == []
    assert all(item.source_kind == "SYNTHETIC" for item in result.documents)
    assert EvidenceBundle.model_validate_json(result.model_dump_json()) == result


def test_repost_is_not_independent_even_on_a_different_site():
    result = control_bundle()
    payload = result.model_dump()
    payload["corroborations"] = [
        Corroboration(
            claim_id="claim-control",
            evidence_ids=["evidence-original", "evidence-repost"],
            verifier_version="control-v1",
            reason="Claimed separate websites.",
        ).model_dump()
    ]
    with pytest.raises(ValidationError, match="same or unknown origin"):
        EvidenceBundle.model_validate(payload)


@pytest.mark.parametrize(
    "variation", ["publisher", "hash", "unknown-origin", "duplicate-id"]
)
def test_independence_rejects_same_publisher_same_bytes_and_missing_origin(variation):
    first = document()
    second = document(
        "independent", "另一个独立调查片段。", original_publisher="机构乙"
    )
    left, right = verified(first), verified(second)
    if variation == "publisher":
        second = second.model_copy(
            update={"original_publisher": first.original_publisher}
        )
    elif variation == "hash":
        second = second.model_copy(
            update={"document_hash": first.document_hash, "fragments": first.fragments}
        )
    elif variation == "unknown-origin":
        second = second.model_copy(update={"original_source_id": None})
    ids = (
        [left.evidence_id, right.evidence_id]
        if variation != "duplicate-id"
        else [left.evidence_id] * 2
    )
    with pytest.raises(ValidationError):
        bundle(
            [first, second],
            [left, right],
            corroborations=[
                Corroboration(
                    claim_id="claim-control",
                    evidence_ids=ids,
                    verifier_version="control-v1",
                    reason="Control.",
                )
            ],
        )


def test_independence_needs_explicit_semantic_verifier_receipt():
    first, second = (
        document(),
        document("other", "第二份独立调查片段。", original_publisher="机构乙"),
    )
    left, right = verified(first), verified(second)
    result = bundle([first, second], [left, right])
    assert summarize_bundle(result)["independently_corroborated_claim_ids"] == []
    result = bundle(
        [first, second],
        [left, right],
        corroborations=[
            Corroboration(
                claim_id="claim-control",
                evidence_ids=[left.evidence_id, right.evidence_id],
                verifier_version="control-v1",
                reason="Independently collected control records.",
            )
        ],
    )
    assert summarize_bundle(result)["independently_corroborated_claim_ids"] == [
        "claim-control"
    ]


def test_entity_alias_collision_and_whole_name_matching():
    other = Entity(
        entity_id="same-name", legal_name="示例甲商贸有限公司", aliases=["示例甲"]
    )
    assert (
        match_entity("示例甲", PARENT.entity_id, [PARENT, CHILD, other])["status"]
        == "AMBIGUOUS"
    )
    assert (
        match_entity(PARENT.legal_name, PARENT.entity_id, [PARENT, CHILD])["status"]
        == "EXACT"
    )
    assert (
        match_entity("示例甲", PARENT.entity_id, [PARENT, CHILD])["status"] == "ALIAS"
    )
    assert (
        match_entity(CHILD.legal_name, PARENT.entity_id, [PARENT, CHILD])["status"]
        == "MISMATCH"
    )
    assert (
        match_entity("示例甲汽车", PARENT.entity_id, [PARENT, CHILD])["status"]
        == "UNMATCHED"
    )


def test_verified_affiliation_does_not_transfer_child_assertion_to_parent():
    ownership = claim(
        claim_id="ownership",
        subject_id=CHILD.entity_id,
        statement="示例甲汽车工业有限公司是示例甲股份有限公司子公司。",
        kind="REPORTED_FACT",
        entity_relation=EntityRelationAssertion(
            related_entity_id=PARENT.entity_id, kind="SUBSIDIARY_OF"
        ),
    )
    doc = document("ownership", ownership.statement)
    receipt = verified(doc, asserted=ownership)
    relationship = EntityRelationship(
        relationship_id="child-parent",
        subject_id=CHILD.entity_id,
        related_entity_id=PARENT.entity_id,
        kind="SUBSIDIARY_OF",
        evidence_ids=[receipt.evidence_id],
        verifier_version="control-relationship-v1",
    )
    result = bundle([doc], [receipt], asserted=ownership, relationships=[relationship])
    assert (
        match_entity(
            CHILD.legal_name, PARENT.entity_id, result.entities, result.relationships
        )["status"]
        == "RELATED"
    )
    assert result.claims[0].subject_id == CHILD.entity_id
    parent_claim = claim()
    draft = EvidenceDraft(
        evidence_id="wrong-scope",
        document_id=doc.document_id,
        claim_id=parent_claim.claim_id,
        subject_id=CHILD.entity_id,
        relation="SUPPORTS",
        excerpt=doc.fragments[0].text,
        location="page:4",
    )
    with pytest.raises(ValueError, match="entity scope"):
        accept_evidence(
            draft,
            document=doc,
            claim=parent_claim,
            verifier_version="control-v1",
            scope_subject_id=CHILD.entity_id,
            entity_scope_verifier_version="scope-v1",
        )
    payload = result.model_dump()
    payload["claims"][0]["entity_relation"] = None
    with pytest.raises(ValidationError, match="relationship requires"):
        EvidenceBundle.model_validate(payload)


@pytest.mark.parametrize(
    "field,value",
    [
        ("excerpt", "文本中并不存在的关键事实"),
        ("location", "page:99"),
        ("subject_id", CHILD.entity_id),
        ("document_id", "unknown-document"),
        ("claim_id", "unknown-claim"),
        ("verifier_version", None),
        ("entity_scope_verifier_version", None),
    ],
)
def test_accepted_evidence_rejects_broken_grounding_and_receipts(field, value):
    doc = document()
    result = bundle([doc], [verified(doc)])
    payload = result.model_dump()
    payload["evidence"][0][field] = value
    with pytest.raises(ValidationError):
        EvidenceBundle.model_validate(payload)


def test_model_draft_cannot_set_acceptance_or_verifier_version():
    for update in (
        {"verification_status": "ACCEPTED"},
        {"verifier_version": "model-self"},
    ):
        with pytest.raises(ValidationError, match="Extra inputs"):
            EvidenceDraft.model_validate(
                {
                    "evidence_id": "e",
                    "document_id": "d",
                    "claim_id": "c",
                    "subject_id": "s",
                    "relation": "SUPPORTS",
                    **update,
                }
            )


@pytest.mark.parametrize(
    "field,value",
    [
        ("published_at", date(2026, 7, 1)),
        ("published_at", None),
        ("event_date", date(2026, 7, 1)),
        ("reporting_period", {"start": "2026-01-01", "end": "2026-07-01"}),
    ],
)
def test_cutoff_excludes_late_or_unknown_dates_without_using_fetch_date(field, value):
    doc = document(**{field: value})
    if value is None:
        draft = EvidenceDraft(
            evidence_id="pending",
            document_id=doc.document_id,
            claim_id="claim-control",
            subject_id=PARENT.entity_id,
            relation="INSUFFICIENT",
        )
        from credra_agent.evidence.models import Evidence

        result = bundle([doc], [Evidence(**draft.model_dump())])
    else:
        result = bundle([doc], [verified(doc)])
    assert result.claims[0].status == "UNVERIFIED"
    assert summarize_bundle(result)["out_of_scope_evidence_ids"]


def test_2025_post_june_publication_is_allowed_and_fetch_is_separate():
    doc = document(published_at=date(2025, 10, 31))
    result = bundle([doc], [verified(doc)])
    assert result.claims[0].status == "SUPPORTED"
    assert doc.retrieved_at.date() > result.as_of


def test_revision_retires_only_explicit_same_claim_evidence_and_preserves_history():
    first = document()
    second = document(
        "revised",
        "更正：示例甲股份有限公司并非全部应收逾期。",
        original_source_id="original",
        revision_of="original",
        published_at=date(2025, 7, 1),
    )
    left = verified(first)
    right = verified(second, relation="REFUTES", revises_evidence_id=left.evidence_id)
    result = bundle([first, second], [left, right])
    assert result.claims[0].status == "REFUTED"
    assert len(result.evidence) == 2
    assert [item.evidence_id for item in active_evidence(result, "claim-control")] == [
        right.evidence_id
    ]
    future = second.model_copy(update={"published_at": date(2026, 7, 1)})
    result = bundle([first, future], [left, right])
    assert result.claims[0].status == "SUPPORTED"


@pytest.mark.parametrize(
    "variation",
    ["cycle", "missing", "origin", "publisher", "earlier", "unlinked-evidence"],
)
def test_revision_chains_reject_cycles_missing_nodes_and_provenance_changes(variation):
    first = document()
    second = document(
        "revised",
        "修订控制文本。",
        original_source_id="original",
        revision_of="original",
        published_at=date(2025, 7, 1),
    )
    left = verified(first)
    right = verified(second, relation="REFUTES", revises_evidence_id=left.evidence_id)
    if variation == "cycle":
        first = first.model_copy(update={"revision_of": "revised"})
    elif variation == "missing":
        second = second.model_copy(update={"revision_of": "absent"})
    elif variation == "origin":
        second = second.model_copy(update={"original_source_id": "other"})
    elif variation == "publisher":
        second = second.model_copy(update={"original_publisher": "other"})
    elif variation == "earlier":
        second = second.model_copy(update={"published_at": date(2024, 1, 1)})
    else:
        second = second.model_copy(update={"revision_of": None})
    with pytest.raises((ValidationError, ValueError)):
        bundle([first, second], [left, right])


def test_claim_graph_rejects_erased_conflict_or_fabricated_reference():
    result = control_bundle()
    for update in (
        {"status": "SUPPORTED"},
        {"refuting_evidence_ids": []},
        {"supporting_evidence_ids": ["fabricated"]},
    ):
        payload = result.model_dump()
        payload["claims"][0].update(update)
        with pytest.raises(ValidationError):
            EvidenceBundle.model_validate(payload)


def test_real_and_synthetic_sources_cannot_support_same_claim():
    doc = document()
    receipt = verified(doc)
    real_doc = doc.model_copy(update={"source_kind": "REAL"})
    with pytest.raises(ValidationError, match="cannot mix"):
        bundle([real_doc], [receipt])


def test_hash_and_physical_printed_page_constraints():
    doc = document()
    with pytest.raises(ValidationError, match="document hash"):
        bundle(
            [doc.model_copy(update={"document_hash": digest("wrong")})], [verified(doc)]
        )
    with pytest.raises(ValidationError, match="physical page"):
        Fragment(location="page:4", physical_page=7, printed_page="4", text="text")
    assert doc.fragments[0].physical_page == 4
    assert doc.fragments[0].printed_page == "1"
    with pytest.raises(ValidationError, match="timezone"):
        document(retrieved_at=RETRIEVED.replace(tzinfo=None))


def test_party_statement_and_analysis_estimate_require_attribution():
    for kind in ("PARTY_STATEMENT", "ANALYST_ESTIMATE"):
        with pytest.raises(ValidationError, match="attribution"):
            claim(kind=kind)
    with pytest.raises(ValidationError, match="reasoning"):
        claim(kind="SYSTEM_INFERENCE")
    statement = claim(
        kind="PARTY_STATEMENT",
        attributed_to=PARENT.legal_name,
        statement="示例甲承诺将账期缩短至60天。",
    )
    doc = document(text=statement.statement)
    result = bundle([doc], [verified(doc, asserted=statement)], asserted=statement)
    assert result.claims[0].kind == "PARTY_STATEMENT"
    assert result.claims[0].statement == statement.statement


def legacy_result():
    source_claim = VerificationClaim(
        claim_id=f"claim:{'a' * 64}",
        subject=PARENT.legal_name,
        category="regulatory",
        statement=claim().statement,
    )
    fetched = FetchedDocument(
        requested_url="https://example.test/control",
        final_url="https://example.test/control",
        status="SUCCESS",
        content_kind="HTML",
        fetched_at=RETRIEVED,
        byte_size=120,
        char_count=30,
        document_hash=digest(document().fragments[0].text),
        segments=[{"location": "paragraph:1", "text": document().fragments[0].text}],
    )
    evidence = ResearchEvidence(
        fact=claim().statement,
        verification_status="SUPPORTED",
        title="控制材料",
        source_url=fetched.requested_url,
        source_domain="example.test",
        source_tier="B",
        published_at="2025-06-11",
        retrieved_at=RETRIEVED,
        query="控制查询",
        relevance_score=1,
        content_hash=digest("检索摘要"),
        source_id="control-news",
        evidence_stage="VERIFIED",
        verification_claim=source_claim,
        fetched_content=FetchedContentReference(
            requested_url=fetched.requested_url,
            final_url=fetched.final_url,
            status="SUCCESS",
            content_kind="HTML",
            fetched_at=RETRIEVED,
            byte_size=120,
            char_count=30,
            document_hash=fetched.document_hash,
            locations=["paragraph:1"],
            snapshot_id=f"content:{'b' * 64}",
        ),
        verification=ClaimVerification(
            claim=source_claim,
            subject_match="EXACT",
            relation="SUPPORTS",
            evidence_excerpt=fetched.segments[0].text,
            evidence_location="paragraph:1",
            reason="Control receipt.",
            confidence=1,
            accepted=True,
            verifier="rules",
            verifier_model="offline-control",
            prompt_version="legacy-v1",
        ),
    )
    return ResearchQueryResult(
        query_type="company",
        query="控制查询",
        found=True,
        facts=[],
        evidence=[evidence],
        candidate_evidence=[evidence],
        verification_status="CORROBORATED",
    ), fetched


def test_legacy_accepted_is_not_promoted_even_with_local_snapshot():
    legacy, fetched = legacy_result()
    original = legacy.model_dump_json()
    mapped = from_research_result(
        legacy,
        target=PARENT,
        as_of=AS_OF,
        source_kind="SYNTHETIC",
        document_loader=lambda url: fetched,
    )
    assert legacy.model_dump_json() == original
    assert len(mapped.evidence) == 1
    assert mapped.evidence[0].verification_status == "PENDING"
    assert (
        mapped.evidence[0].verifier_version == "legacy:rules:offline-control:legacy-v1"
    )
    assert mapped.documents[0].document_hash == fetched.document_hash
    assert mapped.documents[0].document_hash != legacy.evidence[0].content_hash
    assert mapped.claims[0].status == "UNVERIFIED"
    assert mapped.documents[0].original_source_id is None
    assert mapped.evidence[0].entity_scope_verifier_version is None


@pytest.mark.parametrize("variation", ["missing", "changed-hash", "wrong-url"])
def test_missing_or_changed_snapshot_retains_legacy_result_and_pending_evidence(
    variation,
):
    legacy, fetched = legacy_result()

    def loader(url):
        if variation == "missing":
            raise FileNotFoundError("control")
        return fetched.model_copy(
            update={"document_hash": digest("different")}
            if variation == "changed-hash"
            else {"requested_url": "https://example.test/wrong"}
        )

    mapped = from_research_result(
        legacy, target=PARENT, as_of=AS_OF, document_loader=loader
    )
    assert not mapped.documents[0].fragments
    assert mapped.evidence[0].verification_status == "PENDING"
    assert mapped.claims[0].status == "UNVERIFIED"


def test_old_manifest_does_not_invent_document_fetch_event_or_original_text():
    manifest = SourceManifest.model_validate_json(
        (ROOT / "data/case_byd_002594/source/source_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    result = from_source_manifest(manifest, subject_id="002594.SZ", as_of=AS_OF)
    assert not result.evidence and not result.claims
    assert all(
        item.document_hash is None
        and item.retrieved_at is None
        and item.event_date is None
        and item.original_source_id is None
        and not item.fragments
        for item in result.documents
    )
    assert sum(len(item.location_hints) for item in result.documents) == len(
        manifest.field_lineage
    )
    assert result.documents[0].declared_publisher == manifest.sources[0].publisher


def test_frozen_p20_catalog_maps_sixteen_receipts_and_claim_specific_dependency_hints():
    directory = ROOT / "docs/materials/v2-2-p20"
    catalog = json.loads(
        (directory / "material_catalog.json").read_text(encoding="utf-8")
    )
    manifest = json.loads(
        (directory / "file_manifest.json").read_text(encoding="utf-8")
    )
    result = from_p20_catalog(
        catalog,
        manifest,
        entities=[
            Entity(entity_id="002594.SZ", legal_name="比亚迪股份有限公司"),
            Entity(entity_id="byd-auto-industry", legal_name="比亚迪汽车工业有限公司"),
        ],
    )
    assert len(result.bundle.documents) == 16
    assert not result.bundle.evidence and not result.bundle.relationships
    assert (
        result.claim_origin_hints["analysis-gmt-2025"]
        == result.claim_origin_hints["media-bloomberg-gmt"]
    )
    assert (
        result.claim_origin_hints["regulator-miit-60day"]
        == result.claim_origin_hints["media-reuters-60day"]
    )
    assert all(
        item.document_hash and not item.fragments and item.revision_of is None
        for item in result.bundle.documents
    )
    assert (
        match_entity("比亚迪汽车工业有限公司", "002594.SZ", result.bundle.entities)[
            "status"
        ]
        == "MISMATCH"
    )
    bad = json.loads(json.dumps(catalog))
    bad["materials"][0]["access"]["original_response_sha256"] = digest("wrong")
    with pytest.raises(ValueError, match="original response receipt"):
        from_p20_catalog(bad, manifest, entities=result.bundle.entities)
    catalog["material_list_approved_by_user"] = False
    with pytest.raises(ValueError, match="frozen catalog"):
        from_p20_catalog(catalog, manifest, entities=result.bundle.entities)


def test_mapping_failure_keeps_returned_external_result_without_retry(monkeypatch):
    returned = ResearchQueryResult(
        query_type="company",
        query="控制查询",
        found=True,
        facts=[
            ResearchFact(
                category="regulatory", statement="旧摘要", source_id="control-news"
            )
        ],
    )
    calls = []

    class Client:
        async def search_evidence(self, arguments):
            calls.append(arguments)
            return returned

    def broken_mapping(*args, **kwargs):
        raise ValueError("injected local mapping failure")

    monkeypatch.setattr(
        "credra_agent.runtime.executors.from_research_result", broken_mapping
    )
    executor = build_agentic_executor(task_spec(), research_client=Client())
    outcome = executor.execute(
        "search_evidence",
        SearchEvidenceArgs.model_validate(search_decision().arguments),
    )
    assert len(calls) == 1 and outcome.actual_external_requests == 1
    assert outcome.status == "SUCCESS" and outcome.evidence_bundle is None
    assert outcome.payload == returned.model_dump(mode="json")
    assert (
        outcome.gap_question_ids == ["q-regulatory"]
        and not outcome.answered_question_ids
    )


def test_evidence_claim_artifacts_survive_result_window_recovery(tmp_path):
    from app.runtime.tasks import resume_agentic_task, start_agentic_task

    evidence = control_bundle()
    calls = []

    def handler(arguments):
        calls.append(arguments)
        return ExecutionOutcome(
            status="SUCCESS",
            summary="合成主张存在未决冲突。",
            evidence_bundle=evidence,
            payload={"legacy_control": True},
            novelty_keys=["original-investigation"],
            gap_question_ids=["q-regulatory"],
            conflict_ids=["claim-control"],
            actual_external_requests=1,
        )

    executor = ActionExecutor(
        {"search_evidence": HandlerDefinition(handler, external_request_reservation=1)}
    )
    config = settings(tmp_path)
    spec = task_spec().model_copy(
        update={
            "subject_id": PARENT.entity_id,
            "subject_name": PARENT.legal_name,
            "as_of": AS_OF,
        }
    )
    decision = search_decision()
    decision.arguments.update(
        subject_id=PARENT.entity_id, subject_name=PARENT.legal_name
    )
    with pytest.raises(RuntimeError, match="after_result_stored"):
        start_agentic_task(
            thread_id="p21-recovery",
            task_spec=spec,
            authorization=authorization(),
            model=FixedModel([decision]),
            executor=executor,
            settings=config,
            fault_hook=FailOnce("after_result_stored"),
        )
    result = resume_agentic_task(
        thread_id="p21-recovery",
        model=FixedModel([finish_decision()]),
        executor=executor,
        settings=config,
    )
    assert len(calls) == 1
    assert result["state"]["status"] == "LIMITED"
    refs = result["state"]["artifact_refs"]
    evidence_ref = next(
        ref for ref in refs if ref.startswith("artifacts/agent_evidence_v")
    )
    claims_ref = next(ref for ref in refs if ref.startswith("artifacts/agent_claims_v"))
    # Runtime stores runs beneath the imported case; locate only the temp tree.
    path = next(tmp_path.rglob(Path(evidence_ref).name))
    store = ArtifactStore(path.parent.parent)
    assert store.read_json(result["state"]["coverage_ref"])["review_required"] is True
    assert EvidenceBundle.model_validate(store.read_json(evidence_ref)) == evidence
    assert store.read_json(claims_ref)["evidence_bundle_ref"] == evidence_ref
    assert store.read_json(claims_ref)["claims"][0]["status"] == "CONFLICTING"


@pytest.mark.parametrize(
    "variation",
    [
        "missing-bundle",
        "bad-claim-link",
        "changed-bundle",
        "changed-summary",
        "invalid-type",
    ],
)
def test_artifact_validation_rejects_missing_or_changed_sidecars(tmp_path, variation):
    store = ArtifactStore(tmp_path)
    refs = write_evidence_artifacts(
        store, control_bundle(), 1, as_of=AS_OF, subject_id=PARENT.entity_id
    )
    bundle_path, claims_path = [tmp_path / ref for ref in refs]
    if variation == "missing-bundle":
        bundle_path.unlink()
    elif variation == "changed-bundle":
        payload = json.loads(bundle_path.read_text(encoding="utf-8"))
        payload["documents"][0]["title"] = "Changed metadata with still-valid contract"
        bundle_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        payload = json.loads(claims_path.read_text(encoding="utf-8"))
        if variation == "bad-claim-link":
            payload["evidence_bundle_ref"] = "artifacts/missing.json"
        elif variation == "changed-summary":
            payload["evidence_summary"]["conflict_ids"] = []
        else:
            payload = []
        claims_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises((OSError, ValueError, TypeError)):
        validate_evidence_artifacts(
            store, refs, as_of=AS_OF, subject_id=PARENT.entity_id
        )


def test_artifact_task_subject_and_cutoff_are_bound(tmp_path):
    store = ArtifactStore(tmp_path)
    for cutoff, subject in (
        (date(2025, 12, 31), PARENT.entity_id),
        (AS_OF, "different-company"),
    ):
        with pytest.raises(ValueError, match="task subject or cutoff"):
            write_evidence_artifacts(
                store, control_bundle(), 1, as_of=cutoff, subject_id=subject
            )
    assert not store.artifact_dir.exists()


def test_corrupt_evidence_stops_recovery_without_model_or_tool_dispatch(tmp_path):
    from app.runtime.tasks import resume_agentic_task, start_agentic_task

    calls = []
    spec = task_spec().model_copy(
        update={
            "subject_id": PARENT.entity_id,
            "subject_name": PARENT.legal_name,
            "as_of": AS_OF,
        }
    )
    decision = search_decision()
    decision.arguments.update(
        subject_id=PARENT.entity_id, subject_name=PARENT.legal_name
    )

    def handler(arguments):
        calls.append(arguments)
        return ExecutionOutcome(
            status="SUCCESS",
            summary="Synthetic control.",
            evidence_bundle=control_bundle(),
            novelty_keys=["control"],
            actual_external_requests=1,
        )

    executor = ActionExecutor({"search_evidence": HandlerDefinition(handler, 1)})
    config = settings(tmp_path)
    with pytest.raises(RuntimeError, match="after_result_stored"):
        start_agentic_task(
            thread_id="p21-corrupt",
            task_spec=spec,
            authorization=authorization(),
            model=FixedModel([decision]),
            executor=executor,
            settings=config,
            fault_hook=FailOnce("after_result_stored"),
        )
    next(tmp_path.rglob("agent_claims_v1.json")).write_text("[]", encoding="utf-8")
    model = FixedModel([])
    result = resume_agentic_task(
        thread_id="p21-corrupt", model=model, executor=executor, settings=config
    )
    assert result["state"]["stop_reason"] == "EVIDENCE_ARTIFACT_INVALID"
    assert result["state"]["status"] == "LIMITED"
    assert len(calls) == 1 and model.calls == 0


def test_runtime_research_result_reaches_graph_as_pending_evidence(tmp_path):
    from app.runtime.tasks import start_agentic_task

    legacy, fetched = legacy_result()
    calls = []

    class Client:
        async def search_evidence(self, arguments):
            calls.append(arguments)
            return legacy

    spec = task_spec()
    executor = build_agentic_executor(
        spec,
        research_client=Client(),
        evidence_source_kind="SYNTHETIC",
        document_loader=lambda url: fetched,
    )
    result = start_agentic_task(
        thread_id="p21-runtime",
        task_spec=spec,
        authorization=authorization(),
        model=FixedModel([search_decision(), finish_decision()]),
        executor=executor,
        settings=settings(tmp_path),
    )
    assert result["state"]["status"] == "LIMITED" and len(calls) == 1
    assert result["state"]["stop_reason"] == "FINISH_GATE_REJECTED"
    bundle_path = next(tmp_path.rglob("agent_evidence_v1.json"))
    mapped = EvidenceBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    assert mapped.evidence[0].verification_status == "PENDING"
    assert mapped.documents[0].fragments
    legacy_path = next(tmp_path.rglob("agent_tool_result_v1.json"))
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy.model_dump(
        mode="json"
    )


def test_duplicate_bytes_are_reported_without_claiming_independent_sources():
    first = document()
    second = document("copy", first.fragments[0].text, original_publisher="机构乙")
    result = bundle([first, second], [verified(first), verified(second)])
    summary = summarize_bundle(result)
    assert summary["duplicate_document_groups"] == [["copy", "original"]]
    assert summary["independently_corroborated_claim_ids"] == []
