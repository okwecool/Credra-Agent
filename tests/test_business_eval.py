"""M5-A fixed, offline business evaluation tests."""

import json
import shutil
from pathlib import Path

import pytest

from app.evals.runner import load_eval_manifest, run_eval_suite

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUITE = Path("evals/suites/byd_baseline_v1.json")


def _copy_file(project_root: Path, reference: Path) -> None:
    target = project_root / reference
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / reference, target)


def _prepare_eval_project(tmp_path: Path) -> Path:
    project_root = tmp_path / "project"
    shutil.copytree(
        PROJECT_ROOT / "data/case_byd_002594/source",
        project_root / "data/case_byd_002594/source",
    )
    for reference in (
        Path("tests/fixtures/expected/case_byd_002594/expected_financial.json"),
        Path("tests/fixtures/expected/case_byd_002594/expected_anomalies.json"),
        Path("tests/fixtures/expected/case_byd_002594/expected_risk.json"),
        Path("tests/fixtures/search_snapshots/byd_debt_huayi_irrelevant.json"),
        Path("tests/fixtures/search_snapshots/byd_debt_yihualu_irrelevant.json"),
        SUITE,
    ):
        _copy_file(project_root, reference)
    return project_root


def test_fixed_business_eval_passes_offline_and_detects_baseline_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = _prepare_eval_project(tmp_path)
    monkeypatch.setenv("MODEL_API_KEY", "secret-model-sentinel")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-search-sentinel")
    monkeypatch.setenv("REVENUE_THRESHOLD", "0.01")
    monkeypatch.setenv("DEBT_RATIO_THRESHOLD", "0.01")

    passed, first_path = run_eval_suite(
        project_root / SUITE,
        project_root=project_root,
        output_dir=Path("eval-results"),
    )

    assert passed.status == "PASS"
    assert passed.external_call_count == 0
    assert passed.check_count == 16
    assert passed.passed_check_count == 16
    assert all(value == 1.0 for value in passed.metrics.values())
    assert first_path.is_file()
    serialized = first_path.read_text(encoding="utf-8")
    assert "secret-model-sentinel" not in serialized
    assert "secret-search-sentinel" not in serialized

    expected_path = (
        project_root / "tests/fixtures/expected/case_byd_002594/expected_financial.json"
    )
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    expected["metrics"]["revenue_growth"]["values"][0] += 0.01
    expected_path.write_text(
        json.dumps(expected, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    failed, second_path = run_eval_suite(
        project_root / SUITE,
        project_root=project_root,
        output_dir=Path("eval-results"),
    )

    assert failed.status == "FAIL"
    assert second_path.is_file()
    assert second_path != first_path
    financial_check = next(
        check
        for check in failed.cases[0].checks
        if check.check_id == "financial.revenue_growth"
    )
    assert financial_check.status == "FAIL"


def test_eval_manifest_rejects_project_path_traversal(tmp_path: Path) -> None:
    project_root = _prepare_eval_project(tmp_path)
    manifest_path = project_root / SUITE
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cases"][0]["source_dir"] = "../outside/source"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with pytest.raises(ValueError, match="project-relative"):
        load_eval_manifest(manifest_path, project_root=project_root)
