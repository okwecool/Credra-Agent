"""Schemas for structured Case import metadata and validation output."""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, model_validator

SourceTier = Literal["A", "B", "C"]
ValidationSeverity = Literal["ERROR", "WARNING", "CONFIRMATION"]


def _expand_legacy_field(value: str) -> list[str]:
    """Expand a compact v1 field reference into individual field paths."""

    if "." not in value or "/" not in value:
        return [value]
    prefix, suffixes = value.rsplit(".", maxsplit=1)
    return [f"{prefix}.{suffix}" for suffix in suffixes.split("/")]


class SourceDocument(BaseModel):
    """One external document or public-information page."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,127}$")
    source_tier: SourceTier
    source_type: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: AnyHttpUrl
    publication_date: date | None = None
    reporting_period: str | None = None
    used_for: list[str] = Field(min_length=1)


class ManifestCompany(BaseModel):
    """Company identity metadata recorded by a source manifest."""

    model_config = ConfigDict(extra="forbid")

    legal_name: str = Field(min_length=1)
    a_share_code: str | None = None
    market: str | None = None
    reporting_years: list[int] = Field(min_length=2)

    @model_validator(mode="after")
    def reporting_years_must_be_increasing(self) -> ManifestCompany:
        if self.reporting_years != sorted(set(self.reporting_years)):
            raise ValueError("reporting_years must be unique and strictly increasing")
        return self


class ManifestScope(BaseModel):
    """Authorization and sensitive-data boundaries for a public Case."""

    model_config = ConfigDict(extra="forbid")

    data_authorization: str = Field(min_length=1)
    sensitive_data: str = Field(min_length=1)
    accessed_at: date


class FieldLineage(BaseModel):
    """Traceable source metadata for one or more normalized Case fields."""

    model_config = ConfigDict(extra="forbid")

    fields: list[str] = Field(min_length=1)
    source_id: str = Field(min_length=1)
    location: str = Field(min_length=1)
    document_page: int | None = Field(default=None, ge=1)
    printed_page: int | None = Field(default=None, ge=1)
    table_name: str | None = None
    source_unit: str | None = None
    normalized_unit: str | None = None
    accounting_basis: str | None = None
    legacy_compact_field: bool = False

    @model_validator(mode="before")
    @classmethod
    def normalize_v1_compact_field(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        legacy_field = payload.pop("field", None)
        if "fields" not in payload and isinstance(legacy_field, str):
            payload["fields"] = _expand_legacy_field(legacy_field)
            payload["legacy_compact_field"] = True
        return payload


class SourceManifest(BaseModel):
    """Public-source provenance required by a structured non-legacy Case."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["source_manifest_v1", "source_manifest_v2"]
    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    company: ManifestCompany
    scope: ManifestScope
    sources: list[SourceDocument] = Field(min_length=1)
    field_lineage: list[FieldLineage] = Field(min_length=1)

    @model_validator(mode="after")
    def source_ids_must_be_unique_and_resolvable(self) -> SourceManifest:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source ids must be unique")
        unknown = {
            lineage.source_id
            for lineage in self.field_lineage
            if lineage.source_id not in source_ids
        }
        if unknown:
            raise ValueError(
                f"field_lineage references unknown source ids: {sorted(unknown)}"
            )
        return self


class CaseValidationIssue(BaseModel):
    """One actionable import-validation finding."""

    model_config = ConfigDict(extra="forbid")

    severity: ValidationSeverity
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]+$")
    path: str = Field(min_length=1)
    message: str = Field(min_length=1)


class CaseValidationResult(BaseModel):
    """Serializable Case preflight result for CLI and automation use."""

    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    valid: bool
    status: Literal["VALID", "VALID_WITH_WARNINGS", "INVALID"]
    normalized_currency: Literal["CNY_1000"] | None = None
    normalization_factor: float | None = Field(default=None, gt=0)
    errors: list[CaseValidationIssue]
    warnings: list[CaseValidationIssue]
    confirmations: list[CaseValidationIssue]

    @classmethod
    def from_issues(
        cls,
        *,
        case_id: str,
        errors: list[CaseValidationIssue],
        warnings: list[CaseValidationIssue],
        confirmations: list[CaseValidationIssue],
        normalization_factor: float | None,
    ) -> CaseValidationResult:
        valid = not errors
        return cls(
            case_id=case_id,
            valid=valid,
            status=(
                "INVALID"
                if errors
                else "VALID_WITH_WARNINGS"
                if warnings or confirmations
                else "VALID"
            ),
            normalized_currency="CNY_1000" if normalization_factor else None,
            normalization_factor=normalization_factor,
            errors=errors,
            warnings=warnings,
            confirmations=confirmations,
        )


CASE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
