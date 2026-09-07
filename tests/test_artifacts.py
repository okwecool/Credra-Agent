"""Artifact Store read/write, reference, and traversal safety tests."""

from pathlib import Path

import pytest

from app.models.company import CompanyProfile
from app.tools.artifacts import ArtifactStore


def sample_company() -> CompanyProfile:
    return CompanyProfile(
        company_name="测试企业",
        industry="制造业",
        registered_capital="1000万元",
        established_date="2020-01-01",
        shareholders=["股东甲"],
        business_scope="设备制造",
        major_customers=["客户甲"],
        major_suppliers=["供应商甲"],
    )


def test_artifact_store_round_trips_json_and_markdown(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "case")

    json_ref = store.write_json("artifacts/company_profile_v1.json", sample_company())
    markdown_ref = store.write_markdown("artifacts/note_v1.md", "# Evidence\n")

    assert json_ref == "artifacts/company_profile_v1.json"
    assert store.read_json(json_ref)["company_name"] == "测试企业"
    assert markdown_ref == "artifacts/note_v1.md"
    assert store.read_markdown(markdown_ref) == "# Evidence\n"


def test_artifact_store_does_not_overwrite_different_content(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "case")
    reference = "artifacts/risk_analysis_v1.json"
    store.write_json(reference, {"risk_level": "LOW"})

    with pytest.raises(FileExistsError, match="other content"):
        store.write_json(reference, {"risk_level": "HIGH"})

    assert store.read_json(reference) == {"risk_level": "LOW"}


def test_next_artifact_version_is_derived_from_state_reference() -> None:
    assert (
        ArtifactStore.next_version_reference("research_result", None)
        == "artifacts/research_result_v1.json"
    )
    assert (
        ArtifactStore.next_version_reference(
            "research_result", "artifacts/research_result_v1.json"
        )
        == "artifacts/research_result_v2.json"
    )

    with pytest.raises(ValueError, match="unexpected artifact reference"):
        ArtifactStore.next_version_reference(
            "research_result", "artifacts/risk_analysis_v1.json"
        )


@pytest.mark.parametrize(
    "reference",
    (
        "../secret.json",
        "artifacts/../secret.json",
        "source/company_profile.json",
        "artifacts/nested/file.json",
        "C:/secret.json",
    ),
)
def test_artifact_store_rejects_unsafe_references(
    tmp_path: Path, reference: str
) -> None:
    store = ArtifactStore(tmp_path / "case")

    with pytest.raises(ValueError):
        store.write_json(reference, {})
