"""Versioned contracts for a portable task audit package."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AuditFileEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArtifactManifestEntry(AuditFileEntry):
    artifact_type: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)


class AuditManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["audit_manifest_v1"] = "audit_manifest_v1"
    case_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(pattern=r"^[0-9a-f]{20}$")
    task_status: Literal["COMPLETED"] = "COMPLETED"
    exported_at: datetime
    files: list[AuditFileEntry] = Field(min_length=1, max_length=150)
    limitations: list[str] = Field(default_factory=list, max_length=20)


class AuditExportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(pattern=r"^[0-9a-f]{20}$")
    archive_path: str = Field(min_length=1)
    archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_count: int = Field(ge=1)
    artifact_count: int = Field(ge=1)
