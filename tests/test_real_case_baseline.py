"""M0 real-public-case baseline tests."""

import json
from pathlib import Path

from app.agents.financial import analyze_financials
from app.config import Settings
from app.graph.routing import route_after_financial
from app.graph.state import initial_state
from app.models.financial import FinancialStatement
from app.tools.anomalies import detect_anomalies

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_ID = "case_byd_002594"


def load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def default_settings() -> Settings:
    return Settings(
        _env_file=None,
        data_dir=PROJECT_ROOT / "data",
        revenue_threshold=0.20,
        cashflow_threshold=0.0,
        debt_ratio_threshold=0.15,
    )


def test_byd_financial_metrics_match_m0_manual_baseline() -> None:
    statement = FinancialStatement.model_validate(
        load_json(
            PROJECT_ROOT / "data" / CASE_ID / "source" / "financial_statement.json"
        )
    )
    expected = load_json(
        PROJECT_ROOT
        / "tests"
        / "fixtures"
        / "expected"
        / CASE_ID
        / "expected_financial.json"
    )

    analysis = analyze_financials(CASE_ID, statement)

    assert analysis.currency == expected["currency"]
    for metric_name, baseline in expected["metrics"].items():
        metric = analysis.metrics[metric_name]
        assert metric.years == baseline["years"]
        assert metric.values == baseline["values"]


def test_byd_current_anomaly_baseline_routes_to_risk_without_research() -> None:
    statement = FinancialStatement.model_validate(
        load_json(
            PROJECT_ROOT / "data" / CASE_ID / "source" / "financial_statement.json"
        )
    )
    expected = load_json(
        PROJECT_ROOT
        / "tests"
        / "fixtures"
        / "expected"
        / CASE_ID
        / "expected_anomalies.json"
    )
    flags = detect_anomalies(analyze_financials(CASE_ID, statement), default_settings())
    state = initial_state("m0-byd-baseline", CASE_ID)
    state["anomaly_flags"] = flags

    assert flags == expected["expected_flags"]
    assert route_after_financial(state) == expected["expected_route_after_financial"]


def test_byd_source_manifest_covers_case_inputs_with_authoritative_urls() -> None:
    source_dir = PROJECT_ROOT / "data" / CASE_ID / "source"
    manifest = load_json(source_dir / "source_manifest.json")
    expected_evidence = load_json(
        PROJECT_ROOT
        / "tests"
        / "fixtures"
        / "expected"
        / CASE_ID
        / "expected_evidence.json"
    )

    assert manifest["case_id"] == CASE_ID
    assert manifest["company"]["a_share_code"] == "002594"
    assert manifest["company"]["reporting_years"] == [2023, 2024, 2025]
    assert (source_dir / "business_info.md").read_text(encoding="utf-8").strip()

    sources = {source["source_id"]: source for source in manifest["sources"]}
    assert set(expected_evidence["authoritative_sources"]) <= set(sources)
    assert all(source["source_tier"] == "A" for source in sources.values())
    assert all(source["url"].startswith("https://") for source in sources.values())
    assert {item["source_id"] for item in manifest["field_lineage"]} <= set(sources)
