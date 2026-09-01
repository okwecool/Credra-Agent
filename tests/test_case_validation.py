"""M1 structured Case import and preflight validation tests."""

import json
import shutil
from pathlib import Path

from app.case_cli import main as case_cli_main
from app.cases import create_case_template, validate_case

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BYD_CASE_ID = "case_byd_002594"


def copy_case(tmp_path: Path, case_id: str) -> Path:
    data_dir = tmp_path / "data"
    shutil.copytree(
        PROJECT_ROOT / "data" / case_id / "source",
        data_dir / case_id / "source",
    )
    return data_dir


def read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def error_codes(result) -> set[str]:
    return {issue.code for issue in result.errors}


def test_real_byd_case_validates_and_normalizes_to_internal_unit(
    tmp_path: Path,
) -> None:
    data_dir = copy_case(tmp_path, BYD_CASE_ID)

    result = validate_case(BYD_CASE_ID, data_dir)

    assert result.valid is True
    assert result.status == "VALID"
    assert result.normalized_currency == "CNY_1000"
    assert result.normalization_factor == 1.0
    assert result.errors == []
    assert result.warnings == []


def test_legacy_cases_remain_valid_with_migration_warning(tmp_path: Path) -> None:
    data_dir = copy_case(tmp_path, "case_normal")

    result = validate_case("case_normal", data_dir)

    assert result.valid is True
    assert result.normalization_factor == 10.0
    assert [issue.code for issue in result.warnings] == ["LEGACY_CASE_WITHOUT_MANIFEST"]


def test_missing_source_manifest_fails_for_non_legacy_case(tmp_path: Path) -> None:
    data_dir = copy_case(tmp_path, BYD_CASE_ID)
    (data_dir / BYD_CASE_ID / "source" / "source_manifest.json").unlink()

    result = validate_case(BYD_CASE_ID, data_dir)

    assert result.valid is False
    assert "REQUIRED_FILE_MISSING" in error_codes(result)


def test_company_name_mismatch_and_unknown_currency_fail_validation(
    tmp_path: Path,
) -> None:
    data_dir = copy_case(tmp_path, BYD_CASE_ID)
    source_dir = data_dir / BYD_CASE_ID / "source"
    manifest_path = source_dir / "source_manifest.json"
    financial_path = source_dir / "financial_statement.json"
    manifest = read_json(manifest_path)
    financial = read_json(financial_path)
    manifest["company"]["legal_name"] = "名称不一致企业"
    financial["currency"] = "CNY_UNKNOWN"
    write_json(manifest_path, manifest)
    write_json(financial_path, financial)

    result = validate_case(BYD_CASE_ID, data_dir)

    assert result.valid is False
    assert {"COMPANY_NAME_MISMATCH", "UNKNOWN_CURRENCY_UNIT"} <= error_codes(result)


def test_invalid_financial_years_fail_validation(tmp_path: Path) -> None:
    data_dir = copy_case(tmp_path, BYD_CASE_ID)
    financial_path = data_dir / BYD_CASE_ID / "source" / "financial_statement.json"
    financial = read_json(financial_path)
    financial["statements"][1]["year"] = 2023
    write_json(financial_path, financial)

    result = validate_case(BYD_CASE_ID, data_dir)

    assert result.valid is False
    assert "FINANCIAL_STATEMENT_INVALID" in error_codes(result)


def test_suspicious_financial_values_require_human_confirmation(tmp_path: Path) -> None:
    data_dir = copy_case(tmp_path, BYD_CASE_ID)
    financial_path = data_dir / BYD_CASE_ID / "source" / "financial_statement.json"
    financial = read_json(financial_path)
    first_year = financial["statements"][0]
    first_year["current_assets"] = 1
    first_year["current_liabilities"] = 10
    first_year["total_assets"] = 100
    first_year["total_liabilities"] = 10
    write_json(financial_path, financial)

    result = validate_case(BYD_CASE_ID, data_dir)

    assert result.valid is True
    assert {issue.code for issue in result.confirmations} == {"VERY_LOW_CURRENT_RATIO"}


def test_case_template_is_created_without_overwriting_existing_case(
    tmp_path: Path,
) -> None:
    case_dir = create_case_template("case_new_demo", tmp_path / "data")

    assert (case_dir / "source" / "company_profile.json").is_file()
    assert (case_dir / "source" / "financial_statement.json").is_file()
    assert (case_dir / "source" / "business_info.md").is_file()
    assert (case_dir / "source" / "source_manifest.json").is_file()

    try:
        create_case_template("case_new_demo", tmp_path / "data")
    except ValueError as exc:
        assert "already exists" in str(exc)
    else:
        raise AssertionError("Case template overwrote an existing Case")


def test_case_cli_validate_and_run_reject_invalid_case_before_runtime(
    tmp_path: Path, capsys
) -> None:
    data_dir = copy_case(tmp_path, BYD_CASE_ID)
    (data_dir / BYD_CASE_ID / "source" / "source_manifest.json").unlink()
    db_path = tmp_path / "checkpoints" / "credra.db"

    validate_exit = case_cli_main(
        ["--data-dir", str(data_dir), "validate", "--case-id", BYD_CASE_ID]
    )
    validate_payload = json.loads(capsys.readouterr().out)
    run_exit = case_cli_main(
        [
            "--db",
            str(db_path),
            "--data-dir",
            str(data_dir),
            "run",
            "--case-id",
            BYD_CASE_ID,
            "--thread-id",
            "m1-invalid-case-001",
        ]
    )
    run_payload = json.loads(capsys.readouterr().out)

    assert validate_exit == 2
    assert validate_payload["valid"] is False
    assert run_exit == 2
    assert run_payload["error"] == "case validation failed"
    assert not db_path.exists()
