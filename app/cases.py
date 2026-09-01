"""Case-template creation and import preflight validation."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.models.case import (
    CASE_ID_PATTERN,
    CaseValidationIssue,
    CaseValidationResult,
    SourceManifest,
)
from app.models.company import CompanyProfile
from app.models.financial import FinancialStatement

LEGACY_CASE_IDS = frozenset({"case_normal", "case_risky"})
CANONICAL_CURRENCY = "CNY_1000"
CURRENCY_FACTORS = {
    "CNY": 0.001,
    "CNY_1000": 1.0,
    "CNY_10K": 10.0,
    "CNY_100M": 100000.0,
}
FINANCIAL_FIELDS = (
    "revenue",
    "net_profit",
    "current_assets",
    "current_liabilities",
    "total_assets",
    "total_liabilities",
    "operating_cash_flow",
)
REQUIRED_SOURCE_FILES = (
    "company_profile.json",
    "financial_statement.json",
    "business_info.md",
)


def _issue(severity: str, code: str, path: str, message: str) -> CaseValidationIssue:
    return CaseValidationIssue(
        severity=severity,  # type: ignore[arg-type]
        code=code,
        path=path,
        message=message,
    )


def _read_json(path: Path) -> tuple[dict[str, Any] | None, CaseValidationIssue | None]:
    try:
        with path.open(encoding="utf-8") as file:
            payload = json.load(file)
    except json.JSONDecodeError as exc:
        return None, _issue(
            "ERROR",
            "INVALID_JSON",
            str(path),
            f"JSON cannot be parsed: {exc.msg}",
        )
    except OSError as exc:
        return None, _issue(
            "ERROR", "SOURCE_READ_ERROR", str(path), f"Cannot read source file: {exc}"
        )
    if not isinstance(payload, dict):
        return None, _issue(
            "ERROR", "INVALID_JSON_OBJECT", str(path), "Expected a JSON object."
        )
    return payload, None


def validate_case_id(case_id: str) -> None:
    """Reject path-like Case IDs before resolving a filesystem path."""

    if not CASE_ID_PATTERN.fullmatch(case_id):
        raise ValueError(
            "case_id must match ^[a-z][a-z0-9_]{2,63}$ and cannot contain a path"
        )


def _financial_paths(statement: FinancialStatement) -> set[str]:
    return {
        f"financial_statement.statements[{year.year}].{field}"
        for year in statement.statements
        for field in FINANCIAL_FIELDS
    }


def _check_financial_reasonableness(
    statement: FinancialStatement,
    errors: list[CaseValidationIssue],
    confirmations: list[CaseValidationIssue],
) -> None:
    for item in statement.statements:
        prefix = f"financial_statement.statements[{item.year}]"
        if item.revenue <= 0:
            errors.append(
                _issue(
                    "ERROR",
                    "NON_POSITIVE_REVENUE",
                    f"{prefix}.revenue",
                    "Revenue must be greater than zero for downstream financial metrics.",
                )
            )
        if item.current_assets > item.total_assets:
            errors.append(
                _issue(
                    "ERROR",
                    "CURRENT_ASSETS_EXCEED_TOTAL_ASSETS",
                    f"{prefix}.current_assets",
                    "Current assets cannot exceed total assets.",
                )
            )
        if item.current_liabilities > item.total_liabilities:
            errors.append(
                _issue(
                    "ERROR",
                    "CURRENT_LIABILITIES_EXCEED_TOTAL_LIABILITIES",
                    f"{prefix}.current_liabilities",
                    "Current liabilities cannot exceed total liabilities.",
                )
            )
        if item.current_assets / item.current_liabilities < 0.5:
            confirmations.append(
                _issue(
                    "CONFIRMATION",
                    "VERY_LOW_CURRENT_RATIO",
                    prefix,
                    "Current ratio is below 0.5; confirm the reported current-assets and current-liabilities values.",
                )
            )
        if item.total_liabilities / item.total_assets > 0.9:
            confirmations.append(
                _issue(
                    "CONFIRMATION",
                    "VERY_HIGH_DEBT_RATIO",
                    prefix,
                    "Debt ratio is above 0.9; confirm the reported total-assets and total-liabilities values.",
                )
            )


def _validate_manifest(
    manifest: SourceManifest,
    company: CompanyProfile,
    statement: FinancialStatement,
    errors: list[CaseValidationIssue],
    warnings: list[CaseValidationIssue],
) -> None:
    if manifest.case_id != "":
        # The caller validates the concrete requested ID; this branch documents
        # that the model itself intentionally has no filesystem knowledge.
        pass
    if manifest.company.legal_name != company.company_name:
        errors.append(
            _issue(
                "ERROR",
                "COMPANY_NAME_MISMATCH",
                "source_manifest.company.legal_name",
                "Manifest legal_name must equal company_profile.company_name.",
            )
        )
    years = [item.year for item in statement.statements]
    if manifest.company.reporting_years != years:
        errors.append(
            _issue(
                "ERROR",
                "REPORTING_YEARS_MISMATCH",
                "source_manifest.company.reporting_years",
                "Manifest reporting_years must equal financial_statement years.",
            )
        )

    expected_fields = _financial_paths(statement)
    covered_fields = {
        field for lineage in manifest.field_lineage for field in lineage.fields
    }
    missing = sorted(expected_fields - covered_fields)
    if missing:
        errors.append(
            _issue(
                "ERROR",
                "FINANCIAL_SOURCE_COVERAGE_INCOMPLETE",
                "source_manifest.field_lineage",
                "Missing source lineage for: " + ", ".join(missing),
            )
        )

    for lineage in manifest.field_lineage:
        financial_lineage = any(
            field.startswith("financial_statement.statements[")
            for field in lineage.fields
        )
        if not financial_lineage:
            continue
        if lineage.legacy_compact_field:
            warnings.append(
                _issue(
                    "WARNING",
                    "LEGACY_COMPACT_FIELD_LINEAGE",
                    "source_manifest.field_lineage",
                    "v1 compact field lineage was expanded during validation; use explicit fields in new Cases.",
                )
            )
        if manifest.schema_version == "source_manifest_v2":
            missing_metadata = [
                name
                for name, value in (
                    ("document_page", lineage.document_page),
                    ("table_name", lineage.table_name),
                    ("source_unit", lineage.source_unit),
                    ("normalized_unit", lineage.normalized_unit),
                    ("accounting_basis", lineage.accounting_basis),
                )
                if not value
            ]
            if missing_metadata:
                errors.append(
                    _issue(
                        "ERROR",
                        "FINANCIAL_LINEAGE_METADATA_INCOMPLETE",
                        "source_manifest.field_lineage",
                        "Financial lineage is missing: " + ", ".join(missing_metadata),
                    )
                )
            elif lineage.normalized_unit != CANONICAL_CURRENCY:
                errors.append(
                    _issue(
                        "ERROR",
                        "INVALID_NORMALIZED_UNIT",
                        "source_manifest.field_lineage",
                        f"normalized_unit must be {CANONICAL_CURRENCY}.",
                    )
                )


def validate_case(case_id: str, data_dir: Path) -> CaseValidationResult:
    """Validate a Case without changing its input files or runtime state."""

    validate_case_id(case_id)
    errors: list[CaseValidationIssue] = []
    warnings: list[CaseValidationIssue] = []
    confirmations: list[CaseValidationIssue] = []
    case_dir = (data_dir / case_id).resolve()
    source_dir = case_dir / "source"
    if not source_dir.is_dir():
        errors.append(
            _issue(
                "ERROR",
                "CASE_NOT_FOUND",
                str(source_dir),
                "Case source directory is missing.",
            )
        )
        return CaseValidationResult.from_issues(
            case_id=case_id,
            errors=errors,
            warnings=warnings,
            confirmations=confirmations,
            normalization_factor=None,
        )

    for name in REQUIRED_SOURCE_FILES:
        path = source_dir / name
        if not path.is_file():
            errors.append(
                _issue(
                    "ERROR",
                    "REQUIRED_FILE_MISSING",
                    str(path),
                    "Required source file is missing.",
                )
            )
    business_path = source_dir / "business_info.md"
    if (
        business_path.is_file()
        and not business_path.read_text(encoding="utf-8").strip()
    ):
        errors.append(
            _issue(
                "ERROR",
                "BUSINESS_INFO_EMPTY",
                str(business_path),
                "business_info.md cannot be empty.",
            )
        )
    if errors:
        return CaseValidationResult.from_issues(
            case_id=case_id,
            errors=errors,
            warnings=warnings,
            confirmations=confirmations,
            normalization_factor=None,
        )

    company_payload, company_error = _read_json(source_dir / "company_profile.json")
    financial_payload, financial_error = _read_json(
        source_dir / "financial_statement.json"
    )
    if company_error:
        errors.append(company_error)
    if financial_error:
        errors.append(financial_error)
    company: CompanyProfile | None = None
    statement: FinancialStatement | None = None
    if company_payload is not None:
        try:
            company = CompanyProfile.model_validate(company_payload)
        except ValidationError as exc:
            errors.append(
                _issue(
                    "ERROR",
                    "COMPANY_PROFILE_INVALID",
                    str(source_dir / "company_profile.json"),
                    str(exc),
                )
            )
    if financial_payload is not None:
        try:
            statement = FinancialStatement.model_validate(financial_payload)
        except ValidationError as exc:
            errors.append(
                _issue(
                    "ERROR",
                    "FINANCIAL_STATEMENT_INVALID",
                    str(source_dir / "financial_statement.json"),
                    str(exc),
                )
            )
    if statement is not None:
        _check_financial_reasonableness(statement, errors, confirmations)
    factor = CURRENCY_FACTORS.get(statement.currency) if statement else None
    if statement is not None and factor is None:
        errors.append(
            _issue(
                "ERROR",
                "UNKNOWN_CURRENCY_UNIT",
                "financial_statement.currency",
                "Supported units are: " + ", ".join(sorted(CURRENCY_FACTORS)),
            )
        )

    manifest_path = source_dir / "source_manifest.json"
    if manifest_path.is_file():
        manifest_payload, manifest_error = _read_json(manifest_path)
        if manifest_error:
            errors.append(manifest_error)
        elif manifest_payload is not None:
            try:
                manifest = SourceManifest.model_validate(manifest_payload)
            except ValidationError as exc:
                errors.append(
                    _issue(
                        "ERROR",
                        "SOURCE_MANIFEST_INVALID",
                        str(manifest_path),
                        str(exc),
                    )
                )
            else:
                if manifest.case_id != case_id:
                    errors.append(
                        _issue(
                            "ERROR",
                            "CASE_ID_MISMATCH",
                            "source_manifest.case_id",
                            "Manifest case_id must equal the requested case_id.",
                        )
                    )
                if company is not None and statement is not None:
                    _validate_manifest(manifest, company, statement, errors, warnings)
    elif case_id in LEGACY_CASE_IDS:
        warnings.append(
            _issue(
                "WARNING",
                "LEGACY_CASE_WITHOUT_MANIFEST",
                str(manifest_path),
                "Legacy fixture remains runnable; create a source manifest before using it as a real public Case.",
            )
        )
    else:
        errors.append(
            _issue(
                "ERROR",
                "REQUIRED_FILE_MISSING",
                str(manifest_path),
                "source_manifest.json is required for a non-legacy Case.",
            )
        )

    return CaseValidationResult.from_issues(
        case_id=case_id,
        errors=errors,
        warnings=warnings,
        confirmations=confirmations,
        normalization_factor=factor,
    )


def create_case_template(case_id: str, data_dir: Path) -> Path:
    """Create an editable v2 Case template without overwriting an existing Case."""

    validate_case_id(case_id)
    case_dir = (data_dir / case_id).resolve()
    if case_dir.exists():
        raise ValueError(f"case already exists: {case_id}")
    source_dir = case_dir / "source"
    source_dir.mkdir(parents=True)
    template_company = {
        "company_name": "待填写企业名称",
        "industry": "待填写行业",
        "registered_capital": "待填写注册资本",
        "established_date": "待填写成立日期",
        "shareholders": [],
        "business_scope": "待填写主营业务",
        "major_customers": [],
        "major_suppliers": [],
    }
    template_financial = {
        "currency": CANONICAL_CURRENCY,
        "statements": [
            {
                "year": 2024,
                "revenue": 1,
                "net_profit": 0,
                "current_assets": 1,
                "current_liabilities": 1,
                "total_assets": 1,
                "total_liabilities": 0,
                "operating_cash_flow": 0,
            },
            {
                "year": 2025,
                "revenue": 1,
                "net_profit": 0,
                "current_assets": 1,
                "current_liabilities": 1,
                "total_assets": 1,
                "total_liabilities": 0,
                "operating_cash_flow": 0,
            },
        ],
    }
    template_manifest = {
        "schema_version": "source_manifest_v2",
        "case_id": case_id,
        "company": {
            "legal_name": "待填写企业名称",
            "a_share_code": None,
            "market": None,
            "reporting_years": [2024, 2025],
        },
        "scope": {
            "data_authorization": "待填写资料获取与保存授权范围",
            "sensitive_data": "不包含未获授权敏感数据",
            "accessed_at": datetime.now(UTC).date().isoformat(),
        },
        "sources": [
            {
                "source_id": "replace-with-public-source",
                "source_tier": "A",
                "source_type": "待填写来源类型",
                "publisher": "待填写发布方",
                "title": "待填写文件标题",
                "url": "https://example.com/replace-me",
                "publication_date": None,
                "reporting_period": None,
                "used_for": ["待填写用途"],
            }
        ],
        "field_lineage": [],
    }
    (source_dir / "company_profile.json").write_text(
        json.dumps(template_company, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (source_dir / "financial_statement.json").write_text(
        json.dumps(template_financial, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (source_dir / "source_manifest.json").write_text(
        json.dumps(template_manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (source_dir / "business_info.md").write_text(
        "# 经营信息\n\n请填写公开披露的主营业务、经营观察点和信息披露边界。\n",
        encoding="utf-8",
    )
    return case_dir


def normalize_financial_statement(statement: FinancialStatement) -> FinancialStatement:
    """Return an in-memory financial statement expressed in CNY_1000.

    This conversion is non-mutating. The Case source remains unchanged so its
    original public-report unit stays available for source reconciliation.
    """

    factor = CURRENCY_FACTORS.get(statement.currency)
    if factor is None:
        raise ValueError(f"Unsupported currency unit: {statement.currency}")
    normalized_years = [
        item.model_copy(
            update={field: getattr(item, field) * factor for field in FINANCIAL_FIELDS}
        )
        for item in statement.statements
    ]
    return statement.model_copy(
        update={"currency": CANONICAL_CURRENCY, "statements": normalized_years}
    )
