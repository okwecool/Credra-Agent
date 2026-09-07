"""Build a bounded, checksummed audit ZIP from one completed durable task."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import zipfile
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from app.audit.models import (
    ArtifactManifestEntry,
    AuditExportResult,
    AuditFileEntry,
    AuditManifest,
)
from app.config import Settings
from app.models.research import ResearchResult
from app.models.trace import TraceEvent
from app.runtime.tasks import get_task_status
from app.workbench import report_markdown_to_html

_THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CASE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_RUN_ID = re.compile(r"^[0-9a-f]{20}$")
_ARTIFACT = re.compile(r"(?P<type>[a-z][a-z0-9_]*)_v(?P<version>\d+)\.json")
_CREDENTIAL_FIELD = re.compile(
    rb'"(?:api[_-]?key|authorization|access[_-]?token|client[_-]?secret)"\s*:\s*"[^"\r\n]{4,}"',
    re.IGNORECASE,
)
_WINDOWS_ABSOLUTE_PATH = re.compile(rb"(?<![A-Za-z])[A-Za-z]:[\\/]")
_METRIC_VALUE = re.compile(r"(?:^|;)([a-z_]+)=([^;]+)")
_DEGRADED_STATUSES = {"DEGRADED", "FAILED", "INCOMPLETE", "PARTIAL"}

MAX_ARTIFACTS = 100
MAX_ARTIFACT_BYTES = 5_000_000
MAX_ARTIFACT_TOTAL_BYTES = 25_000_000
MAX_REPORT_BYTES = 2_000_000
MAX_TRACE_BYTES = 5_000_000


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _safe_run(payload: dict[str, Any], settings: Settings) -> tuple[str, str, Path]:
    state = payload.get("state", {})
    if state.get("status") != "COMPLETED":
        raise ValueError("audit export requires a COMPLETED task")
    case_id = state.get("case_id")
    run_id = state.get("run_id")
    if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
        raise ValueError("task contains an invalid case id")
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("audit export requires a new-style run id")
    data_root = settings.data_dir.resolve()
    case_path = data_root / case_id
    case_dir = case_path.resolve()
    run_path = case_dir / "runs" / run_id
    run_dir = run_path.resolve()
    if case_dir.parent != data_root or run_dir.parent != case_dir / "runs":
        raise ValueError("task run path is unsafe")
    if case_path.is_symlink() or run_path.is_symlink():
        raise ValueError("task run path cannot use symbolic links")
    if not (case_dir / "source").is_dir() or not run_dir.is_dir():
        raise ValueError("task run directory is unavailable")
    return case_id, run_id, run_dir


def _safe_artifact_path(run_dir: Path, reference: str) -> Path:
    normalized = PurePosixPath(reference)
    if (
        normalized.is_absolute()
        or ".." in normalized.parts
        or len(normalized.parts) != 2
        or normalized.parts[0] != "artifacts"
        or not _ARTIFACT.fullmatch(normalized.parts[1])
    ):
        raise ValueError("task contains an unsafe artifact reference")
    raw_target = run_dir / Path(*normalized.parts)
    if raw_target.is_symlink():
        raise ValueError("referenced artifact cannot use a symbolic link")
    target = raw_target.resolve()
    if target.parent != (run_dir / "artifacts").resolve() or not target.is_file():
        raise ValueError("referenced artifact is unavailable")
    return target


def _read_report(run_dir: Path, reference: Any) -> str:
    if not isinstance(reference, str):
        raise TypeError("completed task has no report reference")
    normalized = PurePosixPath(reference)
    if (
        normalized.is_absolute()
        or ".." in normalized.parts
        or len(normalized.parts) != 2
        or normalized.parts[0] != "output"
        or normalized.parts[1] != "credit_report.md"
    ):
        raise ValueError("task contains an unsafe report reference")
    raw_target = run_dir / Path(*normalized.parts)
    if raw_target.is_symlink():
        raise ValueError("task report cannot use a symbolic link")
    target = raw_target.resolve()
    if target.parent != (run_dir / "output").resolve() or not target.is_file():
        raise ValueError("task report is unavailable")
    if target.is_symlink() or target.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("task report violates export limits")
    return target.read_text(encoding="utf-8")


def _read_source_manifest(settings: Settings, case_id: str) -> dict[str, Any]:
    data_root = settings.data_dir.resolve()
    case_dir = (data_root / case_id).resolve()
    raw_source_dir = case_dir / "source"
    if raw_source_dir.is_symlink():
        raise ValueError("source directory cannot use a symbolic link")
    source_dir = raw_source_dir.resolve()
    if case_dir.parent != data_root or source_dir.parent != case_dir:
        raise ValueError("source manifest path is unsafe")
    raw_manifest = source_dir / "source_manifest.json"
    if raw_manifest.is_symlink():
        raise ValueError("source manifest cannot use a symbolic link")
    source_manifest = raw_manifest.resolve()
    if source_manifest.parent != source_dir:
        raise ValueError("source manifest path is unsafe")
    if not source_manifest.exists():
        return {
            "schema_version": "audit_source_manifest_placeholder_v1",
            "case_id": case_id,
            "status": "NOT_AVAILABLE",
            "reason": "The source case does not provide source_manifest.json.",
        }
    if not source_manifest.is_file() or source_manifest.is_symlink():
        raise ValueError("source manifest is unsafe")
    return _read_json_bytes(source_manifest)


def _project_evidence(payload: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    reference = payload["state"].get("research_artifact")
    if not reference:
        return {
            "schema_version": "audit_evidence_v1",
            "status": "NOT_REQUESTED",
            "facts": [],
            "evidence": [],
        }
    research = ResearchResult.model_validate_json(
        _safe_artifact_path(run_dir, reference).read_text(encoding="utf-8")
    )
    facts = [
        fact.model_dump(mode="json")
        for result in (research.company_result, research.industry_result)
        for fact in result.facts
    ]
    evidence = []
    for query_type, result in (
        ("company", research.company_result),
        ("industry", research.industry_result),
    ):
        for item in result.evidence:
            verification = item.verification
            evidence.append(
                {
                    "query_type": query_type,
                    "source_id": item.source_id,
                    "title": item.title,
                    "source_url": item.source_url,
                    "source_domain": item.source_domain,
                    "source_tier": item.source_tier,
                    "published_at": (
                        item.published_at.isoformat() if item.published_at else None
                    ),
                    "retrieved_at": item.retrieved_at.isoformat(),
                    "category": item.category,
                    "evidence_stage": item.evidence_stage,
                    "verification_status": item.verification_status,
                    "subject_match": item.subject_match,
                    "category_match": item.category_match,
                    "filter_reasons": item.filter_reasons,
                    "content_hash": item.content_hash,
                    "fetched_content": (
                        item.fetched_content.model_dump(mode="json")
                        if item.fetched_content
                        else None
                    ),
                    "verification": (
                        verification.model_dump(mode="json") if verification else None
                    ),
                }
            )
    return {
        "schema_version": "audit_evidence_v1",
        "status": research.status,
        "execution_status": research.execution_status,
        "verification_status": research.verification_status,
        "candidate_count": research.candidate_count,
        "verified_fact_count": research.verified_fact_count,
        "rejected_result_count": research.rejected_result_count,
        "content_fetch_status": research.content_fetch_status,
        "verification_execution_status": research.verification_execution_status,
        "facts": facts,
        "evidence": evidence,
    }


def _copy_artifacts(run_dir: Path, staging: Path) -> list[ArtifactManifestEntry]:
    artifact_dir = run_dir / "artifacts"
    if not artifact_dir.is_dir() or artifact_dir.is_symlink():
        raise ValueError("task has no artifact directory")
    source_files = sorted(artifact_dir.iterdir(), key=lambda path: path.name)
    if not source_files or len(source_files) > MAX_ARTIFACTS:
        raise ValueError("artifact count violates export limits")
    entries: list[ArtifactManifestEntry] = []
    total_bytes = 0
    for source in source_files:
        match = _ARTIFACT.fullmatch(source.name)
        if (
            match is None
            or not source.is_file()
            or source.is_symlink()
            or source.resolve().parent != artifact_dir.resolve()
        ):
            raise ValueError("artifact directory contains an unsafe entry")
        payload = source.read_bytes()
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ValueError("artifact exceeds export size limit")
        total_bytes += len(payload)
        if total_bytes > MAX_ARTIFACT_TOTAL_BYTES:
            raise ValueError("artifact total exceeds export size limit")
        try:
            json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"artifact is not valid UTF-8 JSON: {source.name}"
            ) from exc
        relative = f"artifacts/{source.name}"
        target = staging / Path(*PurePosixPath(relative).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        entries.append(
            ArtifactManifestEntry(
                path=relative,
                byte_size=len(payload),
                sha256=_sha256(payload),
                artifact_type=match.group("type"),
                version=int(match.group("version")),
            )
        )
    return entries


def _read_trace(thread_id: str, settings: Settings) -> list[TraceEvent]:
    trace_root = settings.trace_dir.resolve()
    raw_trace_path = trace_root / f"{thread_id}.jsonl"
    if raw_trace_path.is_symlink():
        raise ValueError("task trace cannot use a symbolic link")
    trace_path = raw_trace_path.resolve()
    if trace_path.parent != trace_root:
        raise ValueError("task trace path is unsafe")
    if not trace_path.is_file():
        return []
    if trace_path.is_symlink() or trace_path.stat().st_size > MAX_TRACE_BYTES:
        raise ValueError("task trace violates export limits")
    try:
        return [
            TraceEvent.model_validate_json(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("task trace is invalid") from exc


def _artifact_statuses(
    staging: Path, entries: list[ArtifactManifestEntry]
) -> dict[str, dict[str, str]]:
    statuses: dict[str, dict[str, str]] = {}
    for entry in entries:
        value = json.loads((staging / entry.path).read_text(encoding="utf-8"))
        current = {
            key: item
            for key, item in value.items()
            if isinstance(item, str)
            and (key == "status" or key.endswith("_status"))
            and len(item) <= 64
        }
        if current:
            statuses[entry.path] = dict(sorted(current.items()))
    return statuses


def _observed_execution(
    staging: Path, entries: list[ArtifactManifestEntry]
) -> dict[str, Any]:
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    for entry in entries:
        value = json.loads((staging / entry.path).read_text(encoding="utf-8"))
        current = latest.get(entry.artifact_type)
        if current is None or entry.version > current[0]:
            latest[entry.artifact_type] = (entry.version, value)

    analysis_types = {
        "evidence_summary",
        "query_proposal",
        "report_draft",
        "risk_narrative",
    }
    analysis_documents = [
        latest[artifact_type][1]
        for artifact_type in analysis_types
        if artifact_type in latest
    ]
    analysis_models = sorted(
        {
            model
            for document in analysis_documents
            if document.get("execution_status") != "NOT_REQUESTED"
            if isinstance((model := document.get("model_name")), str) and model
        }
    )
    analysis_statuses = {
        document.get("execution_status") for document in analysis_documents
    }
    if analysis_statuses and analysis_statuses <= {"NOT_REQUESTED"}:
        analysis_mode = "deterministic"
    elif analysis_models:
        analysis_mode = "llm"
    else:
        analysis_mode = "NOT_OBSERVED"

    research = latest.get("research_result", (0, {}))[1]
    query_results = [
        result
        for key in ("company_result", "industry_result")
        if isinstance((result := research.get(key)), dict)
    ]
    search_sources = sorted(
        {
            source
            for result in query_results
            if isinstance((source := result.get("source")), str)
            and source not in {"not_requested", "unavailable"}
        }
    )
    verifier_models: set[str] = set()
    for result in query_results:
        for fact in result.get("facts", []):
            model = fact.get("verifier_model") if isinstance(fact, dict) else None
            if isinstance(model, str) and model:
                verifier_models.add(model)
        for evidence in result.get("evidence", []):
            verification = (
                evidence.get("verification") if isinstance(evidence, dict) else None
            )
            model = (
                verification.get("verifier_model")
                if isinstance(verification, dict)
                else None
            )
            if isinstance(model, str) and model:
                verifier_models.add(model)
    return {
        "analysis_mode": analysis_mode,
        "analysis_models": analysis_models,
        "search_sources": search_sources,
        "content_fetch_status": research.get("content_fetch_status"),
        "verification_execution_status": research.get("verification_execution_status"),
        "fact_verifier_models": sorted(verifier_models),
        "content_fetch_provider": "NOT_RECORDED",
    }


def _summary_and_metrics(
    events: list[TraceEvent],
    artifact_count: int,
    research: dict[str, Any],
    artifact_statuses: dict[str, dict[str, str]],
    observed_execution: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    ordered = sorted(events, key=lambda event: event.start_time)
    node_counts = Counter(event.node for event in ordered)
    event_counts = Counter(event.event_type for event in ordered)
    status_counts = Counter(event.status.value for event in ordered)
    first = min((event.start_time for event in ordered), default=None)
    last = max((event.end_time for event in ordered), default=None)
    node_latency: defaultdict[str, int] = defaultdict(int)
    llm_attempts = 0
    input_tokens = 0
    output_tokens = 0
    for event in ordered:
        if event.event_type == "NODE_END":
            node_latency[event.node] += event.latency_ms
        if event.event_type != "LLM_CALL" or not event.output_summary:
            continue
        values = dict(_METRIC_VALUE.findall(event.output_summary))
        for field, target in (
            ("attempts", "attempts"),
            ("input_tokens", "input"),
            ("output_tokens", "output"),
        ):
            raw = values.get(field)
            if raw and raw.isdigit():
                if target == "attempts":
                    llm_attempts += int(raw)
                elif target == "input":
                    input_tokens += int(raw)
                else:
                    output_tokens += int(raw)
    waiting_ms = 0
    waiting_since: datetime | None = None
    for event in ordered:
        if event.event_type == "INTERRUPT":
            waiting_since = event.end_time
        elif event.event_type == "RESUME" and waiting_since is not None:
            waiting_ms += max(
                0, round((event.start_time - waiting_since).total_seconds() * 1000)
            )
            waiting_since = None
    trace_summary = {
        "schema_version": "trace_summary_v1",
        "event_count": len(ordered),
        "first_event_at": first.isoformat() if first else None,
        "last_event_at": last.isoformat() if last else None,
        "node_event_counts": dict(sorted(node_counts.items())),
        "event_type_counts": dict(sorted(event_counts.items())),
        "status_counts": dict(sorted(status_counts.items())),
    }
    metrics = {
        "schema_version": "runtime_metrics_v1",
        "total_elapsed_ms": (
            max(0, round((last - first).total_seconds() * 1000))
            if first and last
            else 0
        ),
        "node_latency_ms": dict(sorted(node_latency.items())),
        "search_request_count": event_counts["TOOL_CALL"] + event_counts["RETRY"],
        "search_api_credits": None,
        "llm_call_count": event_counts["LLM_CALL"],
        "llm_attempt_count": llm_attempts,
        "llm_input_tokens": input_tokens,
        "llm_output_tokens": output_tokens,
        "retry_event_count": event_counts["RETRY"],
        "interrupt_count": event_counts["INTERRUPT"],
        "resume_count": event_counts["RESUME"],
        "human_wait_time_ms": waiting_ms,
        "artifact_count": artifact_count,
        "research_status": research.get("status"),
        "research_execution_status": research.get("execution_status"),
        "verification_status": research.get("verification_status"),
        "artifact_statuses": artifact_statuses,
        "observed_execution": observed_execution,
        "degradation_used": any(
            value in _DEGRADED_STATUSES
            for statuses in artifact_statuses.values()
            for value in statuses.values()
        ),
        "limitations": [
            "Provider API credits are unavailable from the current trace contract.",
            "The content-fetch provider is not recorded by the current artifact contract.",
            "Execution modes are inferred only from persisted task artifacts, not export-time settings.",
            "Metrics aggregate bounded trace metadata and do not include raw trace lines.",
        ],
    }
    return trace_summary, metrics


def _verify_archive(path: Path, files: list[AuditFileEntry]) -> None:
    expected = {entry.path for entry in files} | {"manifest.json"}
    try:
        with zipfile.ZipFile(path) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)) or set(names) != expected:
                raise ValueError("audit archive contains unexpected members")
            if bundle.testzip() is not None:
                raise ValueError("audit archive failed CRC validation")
            for entry in files:
                payload = bundle.read(entry.path)
                if len(payload) != entry.byte_size or _sha256(payload) != entry.sha256:
                    raise ValueError("audit archive checksum validation failed")
            AuditManifest.model_validate_json(bundle.read("manifest.json"))
    except zipfile.BadZipFile as exc:
        raise ValueError("audit archive is invalid") from exc


def _file_entries(staging: Path) -> list[AuditFileEntry]:
    entries = []
    for path in sorted(staging.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = path.relative_to(staging).as_posix()
        payload = path.read_bytes()
        entries.append(
            AuditFileEntry(
                path=relative,
                byte_size=len(payload),
                sha256=_sha256(payload),
            )
        )
    return entries


def _scan_staging_for_secrets(staging: Path, settings: Settings) -> None:
    configured = [
        settings.model_api_key.get_secret_value().encode("utf-8"),
        settings.tavily_api_key.get_secret_value().encode("utf-8"),
    ]
    configured = [value for value in configured if len(value) >= 8]
    for path in staging.rglob("*"):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if any(value in payload for value in configured):
            raise ValueError("audit content contains a configured credential")
        if _CREDENTIAL_FIELD.search(payload):
            raise ValueError("audit content contains a credential-like field")
        if _WINDOWS_ABSOLUTE_PATH.search(payload):
            raise ValueError("audit content contains an absolute local path")


def export_audit_bundle(
    *,
    thread_id: str,
    settings: Settings,
    output_dir: Path,
) -> AuditExportResult:
    """Export one immutable, portable audit ZIP without raw traces or snapshots."""

    if not _THREAD_ID.fullmatch(thread_id):
        raise ValueError("thread id is unsafe for audit export")
    payload = get_task_status(thread_id=thread_id, settings=settings)
    case_id, run_id, run_dir = _safe_run(payload, settings)
    output_root = output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    thread_slug = thread_id[:48].rstrip("._-") or "task"
    archive = output_root / f"{case_id}-{thread_slug}-{run_id}-result.zip"
    if archive.exists():
        raise FileExistsError("audit archive already exists")

    with tempfile.TemporaryDirectory(prefix="audit-", dir=output_root) as temporary:
        staging = Path(temporary)
        report = _read_report(run_dir, payload["state"].get("report_artifact"))
        (staging / "report.md").write_text(report, encoding="utf-8")
        (staging / "report.html").write_text(
            report_markdown_to_html(report), encoding="utf-8"
        )

        source_payload = _read_source_manifest(settings, case_id)
        _write_json(staging / "source_manifest.json", source_payload)

        evidence = _project_evidence(payload, run_dir)
        _write_json(staging / "evidence.json", evidence)
        artifact_entries = _copy_artifacts(run_dir, staging)
        _write_json(
            staging / "artifact_manifest.json",
            {
                "schema_version": "artifact_manifest_v1",
                "artifact_count": len(artifact_entries),
                "artifacts": [
                    entry.model_dump(mode="json") for entry in artifact_entries
                ],
            },
        )
        events = _read_trace(thread_id, settings)
        artifact_statuses = _artifact_statuses(staging, artifact_entries)
        observed_execution = _observed_execution(staging, artifact_entries)
        trace_summary, metrics = _summary_and_metrics(
            events,
            len(artifact_entries),
            evidence,
            artifact_statuses,
            observed_execution,
        )
        _write_json(staging / "trace_summary.json", trace_summary)
        _write_json(staging / "runtime_metrics.json", metrics)
        _scan_staging_for_secrets(staging, settings)
        files = _file_entries(staging)
        manifest = AuditManifest(
            case_id=case_id,
            thread_id=thread_id,
            run_id=run_id,
            exported_at=datetime.now(UTC),
            files=files,
            limitations=[
                "The package excludes checkpoints, snapshots and raw trace lines.",
                "Source content is represented by manifests, hashes and bounded evidence metadata.",
            ],
        )
        _write_json(staging / "manifest.json", manifest.model_dump(mode="json"))
        _scan_staging_for_secrets(staging, settings)
        temporary_archive = output_root / f".{archive.name}.{run_id}.tmp"
        if temporary_archive.exists():
            raise FileExistsError("temporary audit archive already exists")
        try:
            with zipfile.ZipFile(
                temporary_archive, "x", compression=zipfile.ZIP_DEFLATED
            ) as bundle:
                for path in sorted(staging.rglob("*")):
                    if path.is_file():
                        bundle.write(path, path.relative_to(staging).as_posix())
            _verify_archive(temporary_archive, files)
            temporary_archive.replace(archive)
        finally:
            if temporary_archive.exists():
                temporary_archive.unlink()

    archive_payload = archive.read_bytes()
    return AuditExportResult(
        case_id=case_id,
        thread_id=thread_id,
        run_id=run_id,
        archive_path=str(archive),
        archive_sha256=_sha256(archive_payload),
        file_count=len(files) + 1,
        artifact_count=len(artifact_entries),
    )


def _read_json_bytes(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise ValueError("source manifest exceeds export size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("source manifest is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise TypeError("source manifest must be a JSON object")
    return value
