"""Immutable evidence/claim sidecars shared by both investigation runtimes."""

import hashlib
import json
from datetime import date

from app.tools.artifacts import ArtifactStore
from credra_agent.evidence.models import EvidenceBundle
from credra_agent.evidence.service import summarize_bundle


def _digest(payload: dict) -> str:
    serialized = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(serialized.encode('utf-8')).hexdigest()}"


def write_evidence_artifacts(
    store: ArtifactStore,
    bundle: EvidenceBundle,
    version: int,
    *,
    as_of: date,
    subject_id: str,
) -> list[str]:
    # Revalidate mutable Pydantic objects at the persistence trust boundary.
    validated = EvidenceBundle.model_validate(bundle.model_dump())
    _validate_task_scope(validated, as_of, subject_id)
    payload = validated.model_dump(mode="json")
    reference = store.write_json(f"artifacts/agent_evidence_v{version}.json", payload)
    claims_ref = store.write_json(
        f"artifacts/agent_claims_v{version}.json",
        {
            "schema_version": "claim_artifact_v2_p21",
            "evidence_bundle_ref": reference,
            "evidence_bundle_hash": _digest(payload),
            "claims": payload["claims"],
            "evidence_summary": summarize_bundle(validated),
        },
    )
    return [reference, claims_ref]


def validate_evidence_artifacts(
    store: ArtifactStore, references: list[str], *, as_of: date, subject_id: str
) -> None:
    bundles = {
        ref for ref in references if ref.startswith("artifacts/agent_evidence_v")
    }
    claims_refs = [
        ref for ref in references if ref.startswith("artifacts/agent_claims_v")
    ]
    linked = set()
    for reference in claims_refs:
        artifact = store.read_json(reference)
        if not isinstance(artifact, dict):
            raise TypeError("claim artifact must be an object")
        bundle_ref = artifact.get("evidence_bundle_ref")
        if (
            artifact.get("schema_version") != "claim_artifact_v2_p21"
            or not isinstance(bundle_ref, str)
            or bundle_ref not in bundles
        ):
            raise ValueError("invalid claim artifact link")
        payload = store.read_json(bundle_ref)
        bundle = EvidenceBundle.model_validate(payload)
        _validate_task_scope(bundle, as_of, subject_id)
        if (
            artifact.get("evidence_bundle_hash") != _digest(payload)
            or artifact.get("claims") != payload["claims"]
            or artifact.get("evidence_summary") != summarize_bundle(bundle)
        ):
            raise ValueError("evidence artifact integrity mismatch")
        linked.add(bundle_ref)
    if linked != bundles:
        raise ValueError("evidence bundle has no matching claim artifact")


def _validate_task_scope(bundle: EvidenceBundle, as_of: date, subject_id: str) -> None:
    if bundle.as_of != as_of or subject_id not in {
        item.entity_id for item in bundle.entities
    }:
        raise ValueError("evidence bundle does not match task subject or cutoff")
