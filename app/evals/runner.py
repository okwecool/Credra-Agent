"""Isolated offline runner for versioned Credra Agent business evaluations."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError

from app.cases import validate_case
from app.config import Settings
from app.evals.models import (
    BusinessEvalCase,
    EvalCaseResult,
    EvalCheck,
    EvalSuiteManifest,
    EvalSuiteResult,
)
from app.models.financial import FinancialAnalysis
from app.models.risk import RiskAnalysis
from app.models.search import SearchResponse
from app.runtime.tasks import resume_task, start_task
from app.search.evidence import (
    deduplicate_evidence,
    evidence_from_response,
    facts_from_evidence,
)


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"JSON object required: {path.name}")
    return value


def _resolve_project_file(project_root: Path, reference: str) -> Path:
    normalized = PurePosixPath(reference)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"eval path must be project-relative: {reference}")
    target = (project_root / Path(*normalized.parts)).resolve()
    if not target.is_relative_to(project_root) or not target.is_file():
        raise ValueError(f"eval file does not exist in project: {reference}")
    return target


def _resolve_project_dir(project_root: Path, reference: str) -> Path:
    normalized = PurePosixPath(reference)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise ValueError(f"eval path must be project-relative: {reference}")
    target = (project_root / Path(*normalized.parts)).resolve()
    if not target.is_relative_to(project_root) or not target.is_dir():
        raise ValueError(f"eval directory does not exist in project: {reference}")
    return target


def load_eval_manifest(path: Path, *, project_root: Path) -> EvalSuiteManifest:
    """Load a strict manifest that must itself reside beneath the project root."""

    project_root = project_root.resolve()
    manifest_path = path.resolve()
    if not manifest_path.is_relative_to(project_root) or not manifest_path.is_file():
        raise ValueError("eval manifest must be a file inside the project")
    try:
        manifest = EvalSuiteManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as exc:
        raise ValueError("invalid eval manifest") from exc
    for case in manifest.cases:
        _resolve_project_dir(project_root, case.source_dir)
        _resolve_project_file(project_root, case.expected_financial)
        _resolve_project_file(project_root, case.expected_anomalies)
        _resolve_project_file(project_root, case.expected_risk)
        for expectation in case.negative_evidence:
            _resolve_project_file(project_root, expectation.fixture)
    return manifest


def _check(
    check_id: str,
    category: str,
    *,
    expected: Any,
    actual: Any,
    passed: bool,
    message: str,
) -> EvalCheck:
    return EvalCheck(
        check_id=check_id,
        category=category,
        status="PASS" if passed else "FAIL",
        expected=expected,
        actual=actual,
        message=message,
    )


def _financial_checks(
    actual: FinancialAnalysis,
    expected: dict[str, Any],
    tolerance: float,
) -> tuple[list[EvalCheck], float]:
    checks: list[EvalCheck] = []
    matched_values = 0
    total_values = 0
    for metric_name, baseline in expected["metrics"].items():
        metric = actual.metrics.get(metric_name)
        expected_years = baseline["years"]
        expected_values = baseline["values"]
        years_match = metric is not None and metric.years == expected_years
        values_match = metric is not None and len(metric.values) == len(expected_values)
        if values_match:
            comparisons = [
                abs(current - target) <= tolerance
                for current, target in zip(metric.values, expected_values, strict=True)
            ]
            matched_values += sum(comparisons)
            total_values += len(comparisons)
            values_match = all(comparisons)
        else:
            total_values += len(expected_values)
        actual_value = (
            {"years": metric.years, "values": metric.values} if metric else None
        )
        checks.append(
            _check(
                f"financial.{metric_name}",
                "financial",
                expected={"years": expected_years, "values": expected_values},
                actual=actual_value,
                passed=years_match and values_match,
                message=f"{metric_name} matches the human baseline within tolerance",
            )
        )
    accuracy = matched_values / total_values if total_values else 1.0
    return checks, accuracy


def _negative_evidence_checks(
    case: BusinessEvalCase,
    project_root: Path,
) -> tuple[list[EvalCheck], float]:
    checks: list[EvalCheck] = []
    rejected = 0
    for index, expectation in enumerate(case.negative_evidence, start=1):
        response = SearchResponse.model_validate_json(
            _resolve_project_file(project_root, expectation.fixture).read_text(
                encoding="utf-8"
            )
        )
        evidence = deduplicate_evidence(evidence_from_response(response))
        facts = facts_from_evidence(evidence)
        passed = bool(evidence) and all(
            item.evidence_stage == "REJECTED"
            and item.verification_status == "UNVERIFIED"
            and expectation.expected_filter_reason in item.filter_reasons
            for item in evidence
        )
        passed = passed and not facts
        rejected += passed
        checks.append(
            _check(
                f"evidence.negative_{index}",
                "evidence",
                expected={
                    "stage": "REJECTED",
                    "verification_status": "UNVERIFIED",
                    "filter_reason": expectation.expected_filter_reason,
                    "fact_count": 0,
                },
                actual={
                    "stages": [item.evidence_stage for item in evidence],
                    "filter_reasons": [item.filter_reasons for item in evidence],
                    "fact_count": len(facts),
                },
                passed=passed,
                message="wrong-company evidence is rejected before fact creation",
            )
        )
    rate = rejected / len(case.negative_evidence) if case.negative_evidence else 1.0
    return checks, rate


def _source_contract_check(
    case: BusinessEvalCase,
    *,
    runtime_data: Path,
    runtime_source: Path,
) -> EvalCheck | None:
    if case.expected_market is None or case.expected_primary_source_host is None:
        return None
    expected = {
        "case_valid": True,
        "market": case.expected_market,
        "primary_source_host": case.expected_primary_source_host,
    }
    try:
        validation = validate_case(case.case_id, runtime_data)
        manifest = _read_json(runtime_source / "source_manifest.json")
        sources = manifest.get("sources", [])
        source_hosts = sorted(
            {
                host
                for item in sources
                if isinstance(item, dict)
                and isinstance(item.get("url"), str)
                and (host := urlparse(item["url"]).hostname)
            }
        )
        market = manifest.get("company", {}).get("market")
        actual = {
            "case_valid": validation.valid,
            "market": market,
            "primary_source_host": (
                case.expected_primary_source_host
                if case.expected_primary_source_host in source_hosts
                else None
            ),
            "source_hosts": source_hosts,
        }
        passed = (
            validation.valid
            and market == case.expected_market
            and case.expected_primary_source_host in source_hosts
        )
    except (OSError, TypeError, ValueError) as exc:
        actual = {"error": type(exc).__name__}
        passed = False
    return _check(
        "source.case_contract",
        "source",
        expected=expected,
        actual=actual,
        passed=passed,
        message="public source and market metadata satisfy the shared Case contract",
    )


def _run_case(
    case: BusinessEvalCase,
    *,
    suite_execution_id: str,
    project_root: Path,
    result_root: Path,
    tolerance: float,
) -> EvalCaseResult:
    case_root = result_root / case.case_id
    runtime_data = case_root / "runtime" / "data"
    runtime_case = runtime_data / case.case_id
    source = _resolve_project_dir(project_root, case.source_dir)
    expected_anomalies = _read_json(
        _resolve_project_file(project_root, case.expected_anomalies)
    )
    threshold_settings = expected_anomalies["settings"]
    shutil.copytree(source, runtime_case / "source")
    thread_id = f"eval-{case.case_id}-{suite_execution_id}"
    settings = Settings(
        _env_file=None,
        analysis_mode="deterministic",
        analysis_llm_enable_thinking=None,
        model_api_key="",
        research_provider="mock",
        tavily_api_key="",
        content_fetch_provider="disabled",
        fact_verifier="rules",
        data_dir=runtime_data,
        checkpoint_db_path=case_root / "runtime" / "checkpoints.db",
        trace_dir=case_root / "runtime" / "traces",
        revenue_threshold=threshold_settings["revenue_threshold"],
        cashflow_threshold=threshold_settings["cashflow_threshold"],
        debt_ratio_threshold=threshold_settings["debt_ratio_threshold"],
    )
    checks: list[EvalCheck] = []
    source_check = _source_contract_check(
        case,
        runtime_data=runtime_data,
        runtime_source=runtime_case / "source",
    )
    if source_check is not None:
        checks.append(source_check)
    started = start_task(thread_id=thread_id, case_id=case.case_id, settings=settings)
    start_state = started["state"]
    checks.append(
        _check(
            "hitl.waiting_approval",
            "hitl",
            expected={"status": "WAITING_APPROVAL", "next": ["approval"]},
            actual={"status": start_state.get("status"), "next": started["next"]},
            passed=(
                start_state.get("status") == "WAITING_APPROVAL"
                and started["next"] == ["approval"]
                and bool(started["interrupts"])
            ),
            message="real durable runtime reaches the expected human review point",
        )
    )
    allowed_decisions = (
        started["interrupts"][0]["value"].get("allowed_decisions", [])
        if started["interrupts"]
        else []
    )
    checks.append(
        _check(
            "hitl.allowed_decisions",
            "hitl",
            expected=["approve", "research"],
            actual=allowed_decisions,
            passed=allowed_decisions == ["approve", "research"],
            message="review interrupt exposes only the supported decisions",
        )
    )
    completed = resume_task(
        thread_id=thread_id,
        decision="approve",
        comment=case.approval_comment,
        settings=settings,
    )
    final_state = completed["state"]
    run_id = final_state.get("run_id")
    run_dir = runtime_case / "runs" / run_id
    checks.append(
        _check(
            "runtime.completed_after_resume",
            "runtime",
            expected={"status": "COMPLETED", "decision": "approve"},
            actual={
                "status": final_state.get("status"),
                "decision": final_state.get("human_decision"),
            },
            passed=(
                final_state.get("status") == "COMPLETED"
                and final_state.get("human_decision") == "approve"
                and completed["next"] == []
                and completed["interrupts"] == []
            ),
            message="checkpointed task resumes and reaches its terminal state",
        )
    )
    checks.append(
        _check(
            "hitl.comment_integrity",
            "hitl",
            expected=case.approval_comment,
            actual=final_state.get("human_comment"),
            passed=final_state.get("human_comment") == case.approval_comment,
            message="human review comment is preserved exactly",
        )
    )

    expected_financial = _read_json(
        _resolve_project_file(project_root, case.expected_financial)
    )
    financial = FinancialAnalysis.model_validate(
        _read_json(run_dir / final_state["financial_artifact"])
    )
    financial_checks, financial_accuracy = _financial_checks(
        financial, expected_financial, tolerance
    )
    checks.extend(financial_checks)

    expected_flags = expected_anomalies["expected_flags"]
    actual_flags = final_state.get("anomaly_flags", [])
    checks.append(
        _check(
            "anomaly.expected_flags",
            "anomaly",
            expected=expected_flags,
            actual=actual_flags,
            passed=actual_flags == expected_flags,
            message="deterministic anomaly flags match the reviewed baseline",
        )
    )
    expected_route = expected_anomalies["expected_route_after_financial"]
    checks.append(
        _check(
            "anomaly.expected_route",
            "anomaly",
            expected=expected_route,
            actual=start_state.get("current_node"),
            passed=start_state.get("current_node") == expected_route,
            message="the anomaly outcome follows the reviewed workflow route",
        )
    )

    expected_risk = _read_json(_resolve_project_file(project_root, case.expected_risk))
    risk = RiskAnalysis.model_validate(
        _read_json(run_dir / final_state["risk_artifact"])
    )
    risk_expectations = {
        "risk_level": expected_risk["expected_risk_level"],
        "requires_human_review": expected_risk["expected_requires_human_review"],
        "flag_types": expected_risk["expected_flag_types"],
    }
    actual_risk = {
        "risk_level": risk.risk_level.value,
        "requires_human_review": risk.requires_human_review,
        "flag_types": [flag.type for flag in risk.risk_flags],
    }
    checks.append(
        _check(
            "risk.reviewed_baseline",
            "risk",
            expected=risk_expectations,
            actual=actual_risk,
            passed=actual_risk == risk_expectations,
            message="risk level, review requirement and flag types match the baseline",
        )
    )

    report_path = run_dir / final_state["report_artifact"]
    report = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
    checks.append(
        _check(
            "report.generated",
            "report",
            expected=True,
            actual=report_path.is_file(),
            passed=report_path.is_file() and bool(report.strip()),
            message="the durable run produces a non-empty report",
        )
    )
    prohibited_hits = [text for text in case.prohibited_report_text if text in report]
    checks.append(
        _check(
            "report.prohibited_conclusions",
            "report",
            expected=[],
            actual=prohibited_hits,
            passed=not prohibited_hits,
            message="the report contains no prohibited automatic credit conclusion",
        )
    )
    negative_checks, negative_rejection_rate = _negative_evidence_checks(
        case, project_root
    )
    checks.extend(negative_checks)

    status = "PASS" if all(check.status == "PASS" for check in checks) else "FAIL"
    return EvalCaseResult(
        case_id=case.case_id,
        status=status,
        thread_id=thread_id,
        run_id=run_id,
        runtime_directory=(case_root / "runtime").relative_to(project_root).as_posix(),
        checks=checks,
        metrics={
            "financial_accuracy": financial_accuracy,
            "anomaly_recall": 1.0 if actual_flags == expected_flags else 0.0,
            "resume_success": (
                1.0 if final_state.get("status") == "COMPLETED" else 0.0
            ),
            "human_review_integrity": (
                1.0
                if final_state.get("human_comment") == case.approval_comment
                else 0.0
            ),
            "negative_evidence_rejection_rate": negative_rejection_rate,
        },
    )


def run_eval_suite(
    manifest_path: Path,
    *,
    project_root: Path,
    output_dir: Path,
) -> tuple[EvalSuiteResult, Path]:
    """Run one suite in a unique offline directory and persist eval_result.json."""

    started_at = datetime.now(UTC)
    project_root = project_root.resolve()
    output_root = (project_root / output_dir).resolve()
    if not output_root.is_relative_to(project_root):
        raise ValueError("eval output directory must be inside the project")
    manifest = load_eval_manifest(manifest_path, project_root=project_root)
    canonical = json.dumps(
        manifest.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    fingerprint = f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    execution_id = f"{started_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    result_root = output_root / f"{manifest.suite_id}-{execution_id}"
    result_root.mkdir(parents=True, exist_ok=False)
    case_results = [
        _run_case(
            case,
            suite_execution_id=execution_id,
            project_root=project_root,
            result_root=result_root,
            tolerance=manifest.calculation_tolerance,
        )
        for case in manifest.cases
    ]
    metrics: dict[str, float] = {}
    metric_names = sorted({name for case in case_results for name in case.metrics})
    for name in metric_names:
        values = [case.metrics[name] for case in case_results if name in case.metrics]
        metrics[name] = sum(values) / len(values)
    result = EvalSuiteResult(
        suite_id=manifest.suite_id,
        suite_fingerprint=fingerprint,
        status="PASS"
        if all(case.status == "PASS" for case in case_results)
        else "FAIL",
        cases=case_results,
        metrics=metrics,
        started_at=started_at,
    )
    result_path = result_root / "eval_result.json"
    result_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return result, result_path
