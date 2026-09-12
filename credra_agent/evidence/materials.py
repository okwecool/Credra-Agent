"""P20 metadata ingestion, without turning curated candidates into evidence."""

from datetime import date, datetime
from typing import Literal

from pydantic import Field

from credra_agent.evidence.models import Document, Entity, EvidenceBundle, EvidenceModel
from credra_agent.intent.models import Period


class MaterialCatalogArtifact(EvidenceModel):
    schema_version: Literal["p20_catalog_mapping_v2_p21"] = "p20_catalog_mapping_v2_p21"
    bundle: EvidenceBundle
    # Hints are claim-specific; a story can contain other independent claims.
    claim_origin_hints: dict[str, str] = Field(default_factory=dict)


def from_p20_catalog(
    catalog: dict, manifest: dict, *, entities: list[Entity]
) -> MaterialCatalogArtifact:
    if (
        catalog.get("schema_version") != "p20_material_catalog_v1"
        or manifest.get("schema_version") != "p20_file_manifest_v1"
        or catalog.get("status") != "FROZEN"
        or catalog.get("material_list_approved_by_user") is not True
    ):
        raise ValueError("P20 ingestion requires the reviewed, frozen catalog")
    originals = {item["source_id"]: item for item in manifest["originals"]}
    if len(originals) != len(manifest["originals"]):
        raise ValueError("duplicate original source id")
    documents, hints = [], {}
    for material in catalog["materials"]:
        source_id = material["source_id"]
        original = originals.get(source_id)
        access = material["access"]
        if (
            original is None
            or original["sha256"] != access["original_response_sha256"]
            or original["bytes"] != access["original_response_bytes"]
            or original["relative_path"] != access["original_response_local_cache"]
        ):
            raise ValueError("material does not match its original response receipt")
        period = material["event_or_reporting_period"].split("/")
        if len(period) != 2:
            raise ValueError("material event/reporting range is unresolved")
        event_period = Period(
            start=date.fromisoformat(period[0]), end=date.fromisoformat(period[1])
        )
        reporting = material["category"] == "FINANCIAL_DISCLOSURE" or source_id in {
            "issuer-2024-sustainability",
            "issuer-2025-dec-sales",
            "issuer-2026-may-sales",
        }
        documents.append(
            Document(
                document_id=source_id,
                original_source_id=source_id,
                original_publisher=material["original_publisher"],
                hosting_publisher=material["hosting_publisher"],
                source_tags=["exchange_disclosure", "company_disclosure"]
                if reporting
                else ["company_disclosure"]
                if material["category"] == "ISSUER_OPERATING_COMMUNICATION"
                else ["regulator"]
                if material["category"] == "PUBLIC_REGULATORY_RECORD"
                else ["media"],
                title=material["title"],
                url=material["url"],
                published_at=date.fromisoformat(material["published_on"]),
                # Catalog ranges describe both report periods and event windows;
                # retain them as ranges rather than inventing a single event day.
                reporting_period=event_period if reporting else None,
                event_period=None if reporting else event_period,
                retrieved_at=datetime.fromisoformat(
                    original["retrieved_at"].replace("Z", "+00:00")
                ),
                document_hash=original["sha256"],
                hash_scope="RESPONSE_BYTES",
                source_kind=material["source_kind"],
                access_scope="FULL"
                if access["scope"] == "PUBLIC_READABLE"
                else "PARTIAL",
                limitations=[
                    "P20_METADATA_ONLY_NO_VERIFIED_EXCERPT",
                    material["use_and_limit"],
                    f"CATALOG_EVENT_OR_REPORTING_RANGE:{material['event_or_reporting_period']}",
                    f"CATALOG_LOCATION_HINT:{material['location']}",
                ],
            )
        )
        if material.get("claim_origin_group"):
            hints[source_id] = material["claim_origin_group"]
    if {item.document_id for item in documents} != set(originals):
        raise ValueError("original receipt set does not match material set")
    return MaterialCatalogArtifact(
        bundle=EvidenceBundle(
            as_of=date.fromisoformat(catalog["cutoff"]),
            entities=entities,
            documents=documents,
            evidence=[],
            claims=[],
            limitations=["MATERIAL_METADATA_DOES_NOT_IMPLY_VERIFICATION"],
        ),
        claim_origin_hints=hints,
    )
