"""P24 financial arithmetic, input lineage and durable offline integration."""

import hashlib
import json
import shutil
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.runtime.tasks import resume_agentic_task, run_id_for_thread, start_agentic_task
from app.tools.artifacts import ArtifactStore
from credra_agent.evidence.adapters import load_case_evidence
from credra_agent.execution.executor import ExecutionContext
from credra_agent.execution.ledger import ActionLedger
from credra_agent.execution.models import ComputeMetricsArgs
from credra_agent.execution.registry import default_registry
from credra_agent.financial.actions import compute_metrics
from credra_agent.financial.adapters import (
    adapt_legacy_financial,
    load_case_financial,
    load_legacy_financial,
    normalize_amount,
)
from credra_agent.financial.calculations import FORMULAS, calculate_metrics
from credra_agent.financial.models import (
    BALANCE_FIELDS,
    FinancialDatum,
    FinancialInput,
    FinancialSourceRef,
)
from credra_agent.intent.models import Period, Question, SourcePolicy
from credra_agent.planning.models import DecisionDraft
from credra_agent.runtime.executors import build_agentic_executor
from tests.test_v2_agentic_recovery import FailOnce, authorization, settings, task_spec
from tests.test_v2_coordinator import FixedModel, finish_decision

ROOT = Path(__file__).resolve().parents[1]
INPUT_REF = "artifacts/agent_financial_input_v1.json"


def annual(year):
    return Period(start=date(year, 1, 1), end=date(year, 12, 31))


def dataset():
    fixture = json.loads(
        (ROOT / "tests/fixtures/v2_readiness/synthetic_ac.json").read_text(
            encoding="utf-8"
        )
    )
    datums = []
    for year, values in fixture["inputs"].items():
        for field, value in values.items():
            metric = "net_profit" if field.startswith("net_profit_") else field
            datums.append(
                FinancialDatum(
                    metric=metric,
                    period=annual(int(year)),
                    measurement="BALANCE" if metric in BALANCE_FIELDS else "FLOW",
                    receivables_basis="NET_BOOK_VALUE"
                    if metric == "receivables"
                    else "UNKNOWN",
                    accounting_basis="CONSOLIDATED",
                    source_unit="CNY_1000",
                    revision="original",
                    profit_attribution="GROUP_TOTAL"
                    if field == "net_profit_group_total"
                    else "OWNERS_OF_PARENT"
                    if field == "net_profit_owners_of_parent"
                    else "NOT_APPLICABLE",
                    value=Decimal(value),
                    source_refs=[
                        FinancialSourceRef(
                            source_id="synthetic-annual",
                            location=f"synthetic annual/{year}/{field}",
                            published_at=date(2026, 2, 1),
                            source_tags=["exchange"],
                        )
                    ],
                )
            )
    return FinancialInput(
        subject_id=fixture["subject_id"], source_kind="SYNTHETIC", datums=datums
    )


def calculate(data=None, **updates):
    kwargs = {
        "metric_ids": list(FORMULAS),
        "periods": [annual(2025)],
        "accounting_basis": "CONSOLIDATED",
        "as_of": date(2026, 9, 14),
        "source_policy": SourcePolicy(allowed=["exchange"], denied=["social_media"]),
    }
    kwargs.update(updates)
    return {
        item.metric_id: item
        for item in calculate_metrics({INPUT_REF: data or dataset()}, **kwargs)
    }


def patch_field(data, field, *, year=2025, attribution="NOT_APPLICABLE", **updates):
    raw = data.model_dump(mode="python")
    for item in raw["datums"]:
        if (
            item["metric"] == field
            and item["period"]["end"].year == year
            and item["profit_attribution"] == attribution
        ):
            item.update(updates)
    return FinancialInput.model_validate(raw)


def test_synthetic_arithmetic_keeps_group_profit_and_presentation_separate():
    with localcontext() as ctx:
        ctx.prec = 6
        result = calculate()
    assert {key: item.value for key, item in result.items()} == {
        "revenue_growth": Decimal("0.1"),
        "receivables_growth": Decimal("0.4"),
        "growth_gap": Decimal("0.3"),
        "cash_profit_ratio": Decimal("0.8"),
    }
    assert {key: item.display for key, item in result.items()} == {
        "revenue_growth": "10.00%",
        "receivables_growth": "40.00%",
        "growth_gap": "30.00 个百分点",
        "cash_profit_ratio": "0.80 倍",
    }
    assert all(
        item.input_refs and item.formula_version.endswith("_v2")
        for item in result.values()
    )


def test_rounding_only_changes_display_and_growth_gap_uses_unrounded_rates():
    data = patch_field(dataset(), "receivables", year=2024, value=Decimal(1))
    data = patch_field(data, "receivables", value=Decimal("1.00005"))
    result = calculate(data)
    assert result["receivables_growth"].value == Decimal("0.00005")
    assert result["receivables_growth"].display == "0.01%"
    assert result["growth_gap"].value == Decimal("-0.09995")
    assert result["growth_gap"].display == "-10.00 个百分点"


@pytest.mark.parametrize(
    "unit,expected",
    [
        ("CNY", "12345678901234567890123456789.0123456789"),
        ("CNY_1000", "12345678901234567890123456789012.3456789"),
        ("CNY_10K", "123456789012345678901234567890123.456789"),
    ],
)
def test_unit_normalization_is_exact_under_low_decimal_precision(unit, expected):
    with localcontext() as ctx:
        ctx.prec = 6
        assert normalize_amount(
            Decimal("12345678901234567890123456789012.3456789"), unit
        ) == Decimal(expected)


@pytest.mark.parametrize("value", [0.1, True, "NaN", "Infinity"])
def test_contract_rejects_lossy_or_nonfinite_amounts(value):
    with pytest.raises(ValidationError):
        patch_field(dataset(), "revenue", value=value)


@pytest.mark.parametrize(
    "profit,reason",
    [
        (None, "MISSING_CONSOLIDATED_PROFIT"),
        (Decimal(0), "NONPOSITIVE_CONSOLIDATED_PROFIT"),
        (Decimal(-10), "NONPOSITIVE_CONSOLIDATED_PROFIT"),
    ],
)
def test_cash_ratio_discloses_missing_or_nonpositive_group_profit(profit, reason):
    data = patch_field(
        dataset(),
        "net_profit",
        attribution="GROUP_TOTAL",
        value=profit,
        missing_reason=reason if profit is None else None,
    )
    result = calculate(data)["cash_profit_ratio"]
    assert result.status == "NOT_COMPUTABLE" and result.reason == reason
    assert result.value is None and result.display is None


def test_parent_profit_is_not_a_fallback_and_negative_cash_flow_is_valid():
    data = dataset()
    data = FinancialInput.model_validate(
        {
            **data.model_dump(mode="python"),
            "datums": [d for d in data.datums if d.profit_attribution != "GROUP_TOTAL"],
        }
    )
    assert calculate(data)["cash_profit_ratio"].reason == "MISSING_CONSOLIDATED_PROFIT"
    data = patch_field(dataset(), "operating_cash_flow", value=Decimal(-80))
    assert calculate(data)["cash_profit_ratio"].display == "-0.80 倍"


@pytest.mark.parametrize("basis", ["UNKNOWN", "GROSS_BALANCE"])
def test_receivables_are_net_book_value_without_gross_balance_substitution(basis):
    data = patch_field(dataset(), "receivables", receivables_basis=basis)
    assert calculate(data)["growth_gap"].reason == "RECEIVABLES_BASIS_UNSUPPORTED"


@pytest.mark.parametrize(
    "change,reason",
    [
        ("period", "MISSING_OPERATING_CASH_FLOW"),
        ("basis", "ACCOUNTING_BASIS_MISMATCH"),
        ("revision", "AMBIGUOUS_INPUT_REVISION"),
        ("future", "SOURCE_AFTER_AS_OF"),
        ("unknown_date", "SOURCE_PUBLICATION_DATE_UNKNOWN"),
        ("denied", "SOURCE_POLICY_MISMATCH"),
    ],
)
def test_calculation_refuses_period_basis_revision_and_source_mismatches(
    change, reason
):
    data = dataset()
    if change == "period":
        data = patch_field(data, "operating_cash_flow", period=annual(2024))
    elif change == "basis":
        data = patch_field(data, "operating_cash_flow", accounting_basis="PARENT")
    elif change == "revision":
        extra = next(
            d for d in data.datums if d.metric == "operating_cash_flow"
        ).model_copy(update={"revision": "restated"})
        data = FinancialInput.model_validate(
            {**data.model_dump(mode="python"), "datums": [*data.datums, extra]}
        )
    else:
        source = FinancialSourceRef(
            source_id="synthetic-source",
            location="synthetic/page1",
            published_at=date(2026, 10, 1)
            if change == "future"
            else None
            if change == "unknown_date"
            else date(2026, 2, 1),
            source_tags=["social_media"] if change == "denied" else ["exchange"],
        )
        data = patch_field(data, "operating_cash_flow", source_refs=[source])
    assert calculate(data)["cash_profit_ratio"].reason == reason


def test_zero_growth_base_nonannual_period_and_missing_lineage_are_explicit():
    data = patch_field(dataset(), "receivables", year=2024, value=Decimal(0))
    assert (
        calculate(data)["receivables_growth"].reason == "NONPOSITIVE_BASE_RECEIVABLES"
    )
    assert (
        calculate(periods=[Period(start=date(2025, 1, 1), end=date(2025, 6, 30))])[
            "growth_gap"
        ].reason
        == "ANNUAL_PERIOD_REQUIRED"
    )
    with pytest.raises(ValidationError):
        patch_field(dataset(), "revenue", source_refs=[])
    with pytest.raises(ValidationError):
        patch_field(dataset(), "revenue", value=None)
    datum = (
        dataset()
        .datums[0]
        .model_copy(
            update={
                "metric": "total_liabilities",
                "value": Decimal(200),
                "receivables_basis": "UNKNOWN",
            }
        )
    )
    FinancialInput(
        subject_id="SYNTHETIC-CONTROL",
        datums=[
            datum,
            datum.model_copy(update={"metric": "total_assets", "value": Decimal(100)}),
        ],
    )
    with pytest.raises(ValidationError, match="duplicate financial"):
        FinancialInput(subject_id="SYNTHETIC-CONTROL", datums=[datum, datum])


@pytest.mark.parametrize(
    "case,subject",
    [("case_byd_002594", "002594.SZ"), ("case_saic_600104", "600104.SH")],
)
def test_legacy_cases_are_read_only_and_do_not_supply_group_profit(case, subject):
    case_dir = ROOT / "data" / case
    path = case_dir / "source/financial_statement.json"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    data = load_legacy_financial(case_dir, subject_id=subject)
    profits = [
        d for d in data.datums if d.metric == "net_profit" and d.value is not None
    ]
    assert profits and all(d.profit_attribution == "OWNERS_OF_PARENT" for d in profits)
    assert all(
        d.source_refs[0].input_hash == "sha256:" + before
        and d.source_refs[0].source_hash is None
        for d in profits
    )
    assert data.source_kind == "UNKNOWN"
    assert (
        calculate(data, source_policy=SourcePolicy())["cash_profit_ratio"].reason
        == "MISSING_CONSOLIDATED_PROFIT"
    )
    assert (
        calculate(data, source_policy=SourcePolicy())["revenue_growth"].status
        == "COMPUTED"
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_legacy_normalized_import_is_not_scaled_twice_or_guessed():
    payload = {
        "currency": "CNY_1000",
        "statements": [
            {
                "year": 2025,
                "revenue": "100",
                "net_profit": "40",
                "net_profit_group_total": "50",
            }
        ],
    }
    manifest = {
        "field_lineage": [
            {
                "fields": ["financial_statement.statements[2025].revenue"],
                "source_unit": "CNY_10K",
                "normalized_unit": "CNY_1000",
                "accounting_basis": "合并",
            }
        ]
    }
    data = adapt_legacy_financial(
        payload,
        subject_id="SYNTHETIC-CONTROL",
        source_hash="sha256:" + "a" * 64,
        manifest=manifest,
    )
    revenue = data.datums[0]
    assert (
        revenue.value == Decimal(100)
        and revenue.source_refs[0].reported_unit == "CNY_10K"
    )
    assert data.datums[1].profit_attribution == "UNKNOWN"
    assert all(
        d.value is None for d in data.datums if d.profit_attribution == "GROUP_TOTAL"
    )
    payload["statements"][0]["revenue"] = 0.1
    with pytest.raises(TypeError, match="REQUIRES_DECIMAL"):
        adapt_legacy_financial(
            payload, subject_id="SYNTHETIC-CONTROL", source_hash="sha256:" + "a" * 64
        )


def financial_task():
    return task_spec().model_copy(
        update={
            "subject_id": "SYNTHETIC-CONTROL",
            "subject_name": "合成财务控制样本",
            "case_id": "case_synthetic_financial",
            "as_of": date(2026, 9, 14),
            "periods": [annual(2025)],
            "questions": [
                Question(
                    question_id="q-cash",
                    text="核对合成样本现金利润比",
                    completion_criteria="计算且披露采信缺口",
                    focus="cash_quality",
                )
            ],
        }
    )


def test_action_requires_visible_subject_scoped_input_and_zero_tool_budget(tmp_path):
    store = ArtifactStore(tmp_path)
    store.write_json(INPUT_REF, dataset())
    context = ExecutionContext(
        store, financial_task(), authorization().limits, {INPUT_REF}
    )
    args = ComputeMetricsArgs(
        metric_ids=list(FORMULAS),
        input_refs=[INPUT_REF],
        accounting_basis="CONSOLIDATED",
    )
    tools = build_agentic_executor(context.task_spec)
    assert (
        tools.reservation_for("compute_metrics", context.limits)
        == tools.token_reservation_for("compute_metrics", context.limits)
        == 0
    )
    outcome = tools.execute("compute_metrics", args, context=context)
    assert (
        outcome.status == "SUCCESS"
        and outcome.actual_external_requests == outcome.actual_tokens == 0
    )
    assert outcome.answered_question_ids == [] and outcome.gap_question_ids == [
        "q-cash"
    ]
    assert (
        compute_metrics(args, replace(context, available_refs=set())).error_code
        == "FINANCIAL_INPUT_NOT_VISIBLE"
    )
    assert (
        compute_metrics(args, replace(context, task_spec=task_spec())).error_code
        == "FINANCIAL_SUBJECT_MISMATCH"
    )
    assert (
        compute_metrics(
            args,
            replace(
                context, task_spec=financial_task().model_copy(update={"periods": []})
            ),
        ).error_code
        == "FINANCIAL_PERIOD_REQUIRED"
    )
    assert (
        tools.execute(
            "compute_metrics",
            args.model_copy(update={"metric_ids": ["invented"]}),
            context=context,
        ).status
        == "FAILED"
    )
    catalog = next(
        item
        for item in default_registry().catalog()
        if item["name"] == "compute_metrics"
    )
    assert (
        "growth_gap" in catalog["description"]
        and "value" not in catalog["arguments_schema"]["properties"]
    )


@pytest.mark.parametrize("source", ["explicit", "case"])
@pytest.mark.parametrize("crash", [False, True])
def test_durable_graph_exposes_inputs_saves_results_and_replays_without_cost(
    tmp_path, crash, source
):
    config = settings(tmp_path)
    (config.data_dir / "case_synthetic_financial/source").mkdir(parents=True)
    input_path = (
        config.data_dir / "case_synthetic_financial/source/financial_input_v2.json"
    )
    if source == "case":
        input_path.write_text(dataset().model_dump_json(indent=2), encoding="utf-8")
    initial = {"initial_financial_input": dataset()} if source == "explicit" else {}
    action = DecisionDraft(
        decision="ACTION",
        tool="compute_metrics",
        arguments={
            "metric_ids": list(FORMULAS),
            "input_refs": [INPUT_REF],
            "accounting_basis": "CONSOLIDATED",
        },
        expected_observation="取得合成算例计算结果并继续核验",
        reason_summary="使用已导入的同口径字段",
    )
    model = FixedModel([action, finish_decision("NEEDS_REVIEW")])
    tools = build_agentic_executor(financial_task())
    kwargs = {
        "thread_id": "financial-offline",
        "settings": config,
        "model": model,
        "executor": tools,
    }
    if crash:
        with pytest.raises(RuntimeError, match="after_result_stored"):
            start_agentic_task(
                **kwargs,
                task_spec=financial_task(),
                authorization=authorization(),
                **initial,
                fault_hook=FailOnce("after_result_stored"),
            )
        # Resume must consume the frozen Run input even if the source Case changes.
        if source == "case":
            input_path.write_text(
                '{"subject_id":"foreign","datums":[]}', encoding="utf-8"
            )
        result = resume_agentic_task(**kwargs)
    else:
        result = start_agentic_task(
            **kwargs,
            task_spec=financial_task(),
            authorization=authorization(),
            **initial,
        )
    store = ArtifactStore(
        config.data_dir
        / "case_synthetic_financial/runs"
        / run_id_for_thread(kwargs["thread_id"])
    )
    saved = store.read_json("artifacts/agent_tool_result_v1.json")
    assert saved["schema_version"] == "agent_financial_result_v2"
    assert saved["results"][-1]["value"] == "0.8" and saved["source_kinds"] == [
        "SYNTHETIC"
    ]
    first = model.payloads[0]["evidence_context"]["financial_inputs"][0]
    assert first["reference"] == INPUT_REF and "value" not in first["fields"][0]
    if source == "case":
        assert first["input_file_hash"].startswith("sha256:")
        assert first["input_file_ref"] == "source/financial_input_v2.json"
    assert (
        model.payloads[1]["evidence_context"]["financial_results"][0]["results"][-1][
            "display"
        ]
        == "0.80 倍"
    )
    ledger = ActionLedger(config.checkpoint_db_path)
    before = ledger.budget_snapshot(kwargs["thread_id"], authorization())
    assert before["external_spent"] == 2 and before["token_spent"] == 40
    assert resume_agentic_task(**kwargs)["state"] == result["state"]
    after = ledger.budget_snapshot(kwargs["thread_id"], authorization())
    assert (before["external_spent"], before["token_spent"]) == (
        after["external_spent"],
        after["token_spent"],
    )
    assert len(model.payloads) == 2


def test_foreign_initial_input_is_rejected_before_creating_run(tmp_path):
    config = settings(tmp_path)
    with pytest.raises(ValueError, match="FINANCIAL_SUBJECT_MISMATCH"):
        start_agentic_task(
            thread_id="foreign-input",
            task_spec=task_spec(),
            authorization=authorization(),
            settings=config,
            model=FixedModel([]),
            executor=build_agentic_executor(task_spec()),
            initial_financial_input=dataset(),
        )
    assert not (config.data_dir / "case_byd_002594/runs").exists()


def test_case_input_optional_and_snapshot_hash_is_of_raw_import_file(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    assert load_case_financial(tmp_path, subject_id="SYNTHETIC-CONTROL") is None
    path = source / "financial_input_v2.json"
    content = dataset().model_dump_json(indent=2).encode("utf-8")
    path.write_bytes(content)
    loaded = load_case_financial(tmp_path, subject_id="SYNTHETIC-CONTROL")
    assert loaded.input_file_hash == "sha256:" + hashlib.sha256(content).hexdigest()
    assert loaded.datums == dataset().datums
    assert path.read_bytes() == content


@pytest.mark.parametrize("invalid", ["json", "schema", "subject", "unit"])
def test_invalid_declared_case_input_fails_before_model_or_run_artifacts(
    tmp_path, invalid
):
    config = settings(tmp_path)
    case = config.data_dir / "case_synthetic_financial"
    (case / "source").mkdir(parents=True)
    raw = dataset().model_dump(mode="json")
    if invalid == "schema":
        raw["schema_version"] = "unknown"
    elif invalid == "subject":
        raw["subject_id"] = "SYNTHETIC-OTHER"
    elif invalid == "unit":
        raw["datums"][0]["normalized_unit"] = "USD"
    (case / "source/financial_input_v2.json").write_text(
        "{" if invalid == "json" else json.dumps(raw), encoding="utf-8"
    )
    model = FixedModel([])
    with pytest.raises(ValueError):
        start_agentic_task(
            thread_id="invalid-case-input",
            task_spec=financial_task(),
            authorization=authorization(),
            settings=config,
            model=model,
            executor=build_agentic_executor(financial_task()),
        )
    assert model.payloads == [] and not (case / "runs").exists()


def test_shared_intent_execution_automatically_imports_declared_case_input(tmp_path):
    from credra_agent.intent.models import IntentResult
    from credra_agent.runtime.service import execute_intent

    config = settings(tmp_path)
    source = config.data_dir / "case_synthetic_financial/source"
    source.mkdir(parents=True)
    (source / "financial_input_v2.json").write_text(
        dataset().model_dump_json(), encoding="utf-8"
    )
    model = FixedModel([finish_decision("NEEDS_REVIEW")])
    intent = IntentResult(
        source_message_id="synthetic-input-message",
        thread_id="synthetic-shared-runtime",
        operation="start",
        task_spec=financial_task(),
        parser_mode="RULE_FALLBACK",
    )
    result = execute_intent(
        intent=intent,
        settings=config,
        authorization=authorization(),
        coordinator_model=model,
        executor_factory=build_agentic_executor,
    )
    assert result.outcome == "TASK_STATE"
    assert (
        model.payloads[0]["evidence_context"]["financial_inputs"][0]["subject_id"]
        == "SYNTHETIC-CONTROL"
    )


DEEP_CASE = "case_byd_cash_quality_v2"


def real_task():
    return financial_task().model_copy(
        update={
            "subject_id": "002594",
            "subject_name": "比亚迪股份有限公司",
            "case_id": DEEP_CASE,
            "as_of": date(2026, 4, 2),
            "source_policy": SourcePolicy(preferred=["exchange"]),
        }
    )


def test_real_case_material_identity_cutoff_hashes_and_unverified_status():
    from app.cases import validate_case
    from credra_agent.intent.catalog import load_subject_catalog, resolve_subject

    case = ROOT / "data" / DEEP_CASE
    assert validate_case(DEEP_CASE, ROOT / "data").valid
    bundle = load_case_evidence(case, subject_id="002594", as_of=date(2026, 4, 2))
    data = load_case_financial(case, subject_id="002594")
    manifest = json.loads(
        (case / "source/source_manifest.json").read_text(encoding="utf-8")
    )
    sources = {item["source_id"]: item for item in manifest["sources"]}
    assert len(bundle.documents) == 13 and not bundle.claims and not bundle.evidence
    assert (
        len(
            {
                "media_response"
                if doc.original_publisher == "Reuters"
                else doc.source_tags[0]
                for doc in bundle.documents
            }
        )
        == 5
    )
    assert all(
        doc.source_kind == "REAL"
        and doc.published_at <= bundle.as_of
        and doc.fragments
        and doc.url == sources[doc.document_id]["url"]
        and doc.published_at.isoformat() == sources[doc.document_id]["publication_date"]
        for doc in bundle.documents
    )
    assert sum(doc.hash_scope == "RESPONSE_BYTES" for doc in bundle.documents) == 12
    extracted = next(
        doc for doc in bundle.documents if doc.hash_scope == "EXTRACTED_TEXT"
    )
    assert (
        extracted.document_hash
        == "sha256:"
        + hashlib.sha256(
            "\n".join(f.text for f in extracted.fragments).encode("utf-8")
        ).hexdigest()
    )
    snapshot_hash = (
        "sha256:"
        + hashlib.sha256(
            (case / "source/evidence_bundle_v2.json").read_bytes()
        ).hexdigest()
    )
    assert data.source_kind == "REAL" and len(data.datums) == 10
    assert all(
        ref.input_hash == snapshot_hash for d in data.datums for ref in d.source_refs
    )
    assert not load_case_financial(ROOT / "data/case_byd_002594", subject_id="002594")
    catalog = load_subject_catalog(ROOT / "data")
    assert resolve_subject("比亚迪", catalog).case_id == "case_byd_002594"
    assert resolve_subject(DEEP_CASE, catalog).case_id == DEEP_CASE
    assert resolve_subject("case_byd_002594", catalog).case_id == "case_byd_002594"


def test_real_annual_amounts_preserve_group_profit_net_receivables_and_display():
    data = load_case_financial(ROOT / "data" / DEEP_CASE, subject_id="002594")
    by_field = {
        (d.period.end.year, d.metric, d.profit_attribution): d.value
        for d in data.datums
    }
    assert by_field[2025, "net_profit", "GROUP_TOTAL"] == Decimal(33760758)
    assert by_field[2025, "net_profit", "OWNERS_OF_PARENT"] == Decimal(32619022)
    assert by_field[2024, "net_profit", "GROUP_TOTAL"] == Decimal(41587940)
    assert by_field[2025, "receivables", "NOT_APPLICABLE"] == Decimal(37004685)
    assert by_field[2024, "receivables", "NOT_APPLICABLE"] == Decimal(62298988)
    result = calculate(data, as_of=date(2026, 4, 2))
    assert all(item.status == "COMPUTED" for item in result.values())
    assert {key: item.display for key, item in result.items()} == {
        "revenue_growth": "3.46%",
        "receivables_growth": "-40.60%",
        "growth_gap": "-44.06 个百分点",
        "cash_profit_ratio": "1.75 倍",
    }
    # Independent amount cross-check, not a rounded percentage as denominator.
    with localcontext() as context:
        context.prec = 50
        assert result["cash_profit_ratio"].value == Decimal(59135544) / Decimal(
            33760758
        )


@pytest.mark.parametrize("crash", [False, True])
def test_real_case_shared_runtime_reads_then_computes_and_resume_keeps_snapshot(
    tmp_path, crash
):
    from credra_agent.intent.models import IntentResult
    from credra_agent.runtime.service import execute_intent

    config = settings(tmp_path)
    case = config.data_dir / DEEP_CASE
    shutil.copytree(ROOT / "data" / DEEP_CASE / "source", case / "source")
    source_bytes = {
        path.name: path.read_bytes() for path in (case / "source").iterdir()
    }
    read = DecisionDraft(
        decision="ACTION",
        tool="read_document",
        arguments={
            "reference_id": "artifacts/agent_evidence_v0.json",
            "document_id": "byd-2025-annual",
        },
        expected_observation="读取同一冻结年报必要原文",
        reason_summary="先核对输入来源",
    )
    compute = DecisionDraft(
        decision="ACTION",
        tool="compute_metrics",
        arguments={
            "metric_ids": list(FORMULAS),
            "input_refs": [INPUT_REF],
            "accounting_basis": "CONSOLIDATED",
        },
        expected_observation="计算真实年度指标并保留采信缺口",
        reason_summary="使用同主体同口径冻结金额",
    )
    model = FixedModel([read, compute, finish_decision("NEEDS_REVIEW")])
    intent = IntentResult(
        source_message_id="real-case-message",
        thread_id="real-case-offline",
        operation="start",
        task_spec=real_task(),
        parser_mode="RULE_FALLBACK",
    )
    if crash:
        with pytest.raises(RuntimeError, match="after_result_stored"):
            start_agentic_task(
                thread_id=intent.thread_id,
                task_spec=intent.task_spec,
                authorization=authorization(),
                settings=config,
                model=model,
                executor=build_agentic_executor(real_task()),
                fault_hook=FailOnce("after_result_stored"),
            )
        # Both declared source files become unusable; the Run remains valid.
        for name in ["financial_input_v2.json", "evidence_bundle_v2.json"]:
            (case / "source" / name).write_text("{", encoding="utf-8")
        resume_agentic_task(
            thread_id=intent.thread_id,
            settings=config,
            model=model,
            executor=build_agentic_executor(real_task()),
        )
    else:
        assert (
            execute_intent(
                intent=intent,
                settings=config,
                authorization=authorization(),
                coordinator_model=model,
                executor_factory=build_agentic_executor,
            ).outcome
            == "TASK_STATE"
        )
        assert all(
            (case / "source" / name).read_bytes() == content
            for name, content in source_bytes.items()
        )
    first = model.payloads[0]["evidence_context"]
    assert len(first["bundles"][0]["documents"]) == 13
    assert first["financial_inputs"][0]["source_kind"] == "REAL"
    source_catalog = first["financial_inputs"][0]["source_catalog"]
    assert len(source_catalog) == 1 and source_catalog[0]["source_hash"].startswith(
        "sha256:"
    )
    assert len(json.dumps(first, ensure_ascii=False)) <= 18000
    assert "text" not in first["bundles"][0]["documents"][0]
    assert model.payloads[1]["evidence_context"]["document_reads"]
    assert (
        model.payloads[2]["evidence_context"]["financial_results"][0]["results"][-1][
            "display"
        ]
        == "1.75 倍"
    )
    ledger = ActionLedger(config.checkpoint_db_path)
    budget = ledger.budget_snapshot(intent.thread_id, authorization())
    assert budget["external_spent"] == 3 and budget["token_spent"] == 60


@pytest.mark.parametrize("invalid", ["json", "subject", "cutoff"])
def test_declared_evidence_failure_blocks_run_before_model(tmp_path, invalid):
    config = settings(tmp_path)
    case = config.data_dir / DEEP_CASE
    shutil.copytree(ROOT / "data" / DEEP_CASE / "source", case / "source")
    path = case / "source/evidence_bundle_v2.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    if invalid == "subject":
        raw["entities"][0]["entity_id"] = "foreign"
    elif invalid == "cutoff":
        raw["as_of"] = "2026-04-03"
    path.write_text("{" if invalid == "json" else json.dumps(raw), encoding="utf-8")
    model = FixedModel([])
    with pytest.raises(ValueError):
        start_agentic_task(
            thread_id="invalid-evidence",
            task_spec=real_task(),
            authorization=authorization(),
            settings=config,
            model=model,
            executor=build_agentic_executor(real_task()),
        )
    assert not model.payloads and not (case / "runs").exists()


def test_undeclared_evidence_snapshot_is_compatible(tmp_path):
    assert (
        load_case_evidence(tmp_path, subject_id="002594", as_of=date(2026, 4, 2))
        is None
    )
