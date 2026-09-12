"""Contract rejection cases, approved scoring and frozen cross-process legacy resume."""

import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from spikes.v2_readiness import contracts
from spikes.v2_readiness.evaluation import DIMENSIONS, HARD_FAILURES, Review

FIXTURES = Path(__file__).parent / "fixtures/v2_readiness"
EXAMPLES = json.loads((FIXTURES / "contract_examples.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", EXAMPLES)
def test_published_contract_examples_validate_and_roundtrip(name):
    adapter = TypeAdapter(getattr(contracts, name))
    value = adapter.validate_python(EXAMPLES[name])
    assert adapter.validate_json(adapter.dump_json(value)) == value


@pytest.mark.parametrize(
    "name,patch",
    [
        ("TaskSpec", {"subject_id": None}),
        ("TaskSpec", {"unresolved_fields": ["subject_id"]}),
        ("TaskSpec", {"as_of": "2024-01-01"}),
        (
            "TaskSpec",
            {"source_policy": {"preferred": ["media"], "allowed": ["official"]}},
        ),
        ("TaskSpec", {"source_policy": {"allowed": ["media"], "denied": ["media"]}}),
        ("Action", {"tool": "execute_shell"}),
        ("Action", {"arguments": {"query": "q"}}),
        ("Observation", {"status": "FAILED", "error_code": None}),
        ("Observation", {"status": "SUCCESS"}),
        ("Coverage", {"gap_question_ids": []}),
        ("Coverage", {"unresolved_conflicts": ["c1"], "review_required": False}),
        ("BudgetLedger", {"approval": "APPROVED"}),
        (
            "BudgetLedger",
            {"external_limit": 1, "external_spent": 1, "external_reserved": 1},
        ),
        ("Claim", {"refuting_evidence_ids": []}),
        ("Assessment", {"review_required": False}),
        ("FinancialDatum", {"missing_reason": None}),
        ("FinancialDatum", {"profit_attribution": "NOT_APPLICABLE"}),
        ("FinancialDatum", {"currency": "USD"}),
        ("Evidence", {"verification_status": "ACCEPTED"}),
        ("MetricResult", {"value": "0"}),
        ("GraphVersion", {"execution_mode": "baseline"}),
    ],
)
def test_contracts_reject_silent_scope_financial_and_review_errors(name, patch):
    with pytest.raises(ValidationError):
        TypeAdapter(getattr(contracts, name)).validate_python(
            {**EXAMPLES[name], **patch}
        )


def test_legacy_version_resolution_is_read_only_and_unknown_versions_fail():
    state = json.loads((FIXTURES / "legacy_waiting.json").read_text(encoding="utf-8"))[
        "state"
    ]
    before = deepcopy(state)
    assert contracts.resolve_graph_version(state).graph_version == "legacy_v1"
    assert state == before
    with pytest.raises(ValueError):
        contracts.resolve_graph_version({"execution_mode": "agentic"})
    with pytest.raises(ValidationError):
        contracts.resolve_graph_version(
            {"graph_version": "future_v3", "execution_mode": "agentic"}
        )


def test_actual_budget_overrun_can_be_recorded_but_not_reserved_again():
    ledger = {**EXAMPLES["BudgetLedger"], "token_limit": 10, "tokens_spent": 12}
    assert contracts.BudgetLedger(**ledger).tokens_spent == 12
    with pytest.raises(ValidationError):
        contracts.BudgetLedger(**{**ledger, "tokens_reserved": 1})


def test_financial_contract_preserves_negative_equity_and_missing_profit():
    datum = {
        **EXAMPLES["FinancialDatum"],
        "metric": "equity",
        "profit_attribution": "NOT_APPLICABLE",
        "value": "-100",
        "missing_reason": None,
        "source_refs": ["synthetic-equity"],
    }
    assert contracts.FinancialDatum(**datum).value == Decimal(-100)
    assert contracts.FinancialDatum(**EXAMPLES["FinancialDatum"]).value is None


@pytest.mark.parametrize("failure", sorted(HARD_FAILURES))
def test_hard_failure_overrides_perfect_scores(failure):
    review = Review(
        **dict.fromkeys(DIMENSIONS, 5),
        hard_failures=[failure],
        evidence_refs=["test-observation"],
        reviewer="synthetic-reviewer",
    )
    assert review.outcome() == {"mean": 5.0, "passed": False}


def test_equal_weight_rubric_boundary():
    data = {
        **dict.fromkeys(DIMENSIONS, 4),
        "hard_failures": [],
        "evidence_refs": ["test-observation"],
        "reviewer": "synthetic-reviewer",
    }
    assert Review(**data).outcome() == {"mean": 4.0, "passed": True}
    assert not Review(**{**data, DIMENSIONS[0]: 3}).outcome()["passed"]


def test_golden_inventory_and_synthetic_human_arithmetic():
    golden = json.loads(
        (FIXTURES / "natural_language_goldens.json").read_text(encoding="utf-8")
    )
    cases = golden["cases"]
    assert len(cases) >= 30 and len({c["id"] for c in cases}) == len(cases)
    assert all(c["expected"] and c["must_not"] and c["text"] for c in cases)
    assert {
        "source_only",
        "source_preference",
        "condition",
        "negation",
        "ambiguous_subject",
        "pause",
        "resume",
        "approval_invalidation",
        "contradiction",
    } <= {c["category"] for c in cases}
    pack = json.loads((FIXTURES / "synthetic_ac.json").read_text(encoding="utf-8"))
    assert pack["source_kind"] == "SYNTHETIC"
    last, now = (
        {k: Decimal(v) for k, v in pack["inputs"][year].items()}
        for year in ("2024", "2025")
    )
    expected = pack["questions"][0]["expected"]
    ar = now["receivables"] / last["receivables"] - 1
    revenue = now["revenue"] / last["revenue"] - 1
    assert ar == Decimal(expected["receivables_growth"])
    assert revenue == Decimal(expected["revenue_growth"])
    assert ar - revenue == Decimal(expected["growth_gap"])
    assert now["operating_cash_flow"] / now["net_profit_group_total"] == Decimal(
        expected["cash_profit_ratio"]
    )
    # Checking fixtures and hand arithmetic is not a parser/tool accuracy result.


def test_frozen_legacy_bundle_resumes_in_new_python_process(tmp_path):
    archive = FIXTURES / "legacy_bundle.zip"
    manifest = json.loads((FIXTURES / "legacy_manifest.json").read_text())
    assert (
        hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive_sha256"]
    )
    with zipfile.ZipFile(archive) as bundle:
        assert set(bundle.namelist()) == set(manifest["files"])
        for name in bundle.namelist():
            destination = (tmp_path / name).resolve()
            assert destination.is_relative_to(tmp_path.resolve())
        bundle.extractall(tmp_path)
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((tmp_path / name).read_bytes()).hexdigest() == digest
    shutil.copyfile(tmp_path / "legacy_frozen.sqlite", tmp_path / "checkpoints.sqlite")
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "spikes.v2_readiness.legacy_capture",
            "verify",
            str(tmp_path),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["resumed_status"] == "COMPLETED"
    assert (
        hashlib.sha256(archive.read_bytes()).hexdigest() == manifest["archive_sha256"]
    )
