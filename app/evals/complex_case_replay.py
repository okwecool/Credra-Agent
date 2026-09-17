"""Complex-case replay using frozen materials and explicit live authorization."""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path
from time import perf_counter

from app.config import Settings
from app.llm.gateway import (
    StructuredModelError,
    StructuredModelResult,
    build_analysis_model,
)
from app.runtime.tasks import run_id_for_thread, start_agentic_task
from app.tools.artifacts import ArtifactStore
from credra_agent.evidence.adapters import load_case_evidence
from credra_agent.evidence.investigation import EvidenceVerdict, VerificationDraft
from credra_agent.execution.ledger import ActionLedger, DurableBudgetExhausted
from credra_agent.execution.model_budget import external_usage, model_reservation
from credra_agent.financial.adapters import load_case_financial
from credra_agent.financial.calculations import FORMULAS
from credra_agent.intent.models import Period, Question, SourcePolicy, TaskSpec
from credra_agent.planning.models import (
    ClaimProposal,
    CoordinatorLimits,
    DecisionDraft,
    FinancialCitation,
    QuestionAssessment,
    RunAuthorization,
)
from credra_agent.runtime.executors import build_agentic_executor


def load_package(project_root: Path) -> dict:
    path = project_root / "evals/suites/byd_cash_quality_v2.json"
    package = json.loads(path.read_text(encoding="utf-8"))
    if package["schema_version"] != "complex_case_v1":
        raise ValueError("unsupported complex-case package")
    return package


def case_task(package: dict) -> TaskSpec:
    return TaskSpec(
        version=1,
        source_message_id="complex-case-frozen-query",
        operation="start",
        subject_id=package["subject_id"],
        subject_name=package["subject_name"],
        case_id=package["case_id"],
        as_of=date.fromisoformat(package["as_of"]),
        periods=[Period.model_validate(package["period"])],
        comparison_periods=[Period.model_validate(package["comparison_period"])],
        source_policy=SourcePolicy(
            preferred=["exchange_disclosure"], denied=["social_media"]
        ),
        questions=[Question.model_validate(item) for item in package["questions"]],
        readiness="READY",
    )


def offline_authorization() -> RunAuthorization:
    return RunAuthorization(
        authorization_id="complex-case-offline-replay",
        authorized_by="OFFLINE_TEST",
        task_spec_version=1,
        approval="APPROVED",
        external_request_limit=60,
        token_limit=20000,
        active_seconds_limit=120,
        limits=CoordinatorLimits(
            max_decisions=25,
            no_progress_limit=4,
            model_attempt_reservation=1,
            decision_token_reservation=500,
            decision_max_output_tokens=400,
        ),
    )


def _model_result(output, name):
    return StructuredModelResult(
        output=output,
        model_name=name,
        attempts=1,
        latency_ms=0,
        input_tokens=8,
        output_tokens=12,
        accounting_complete=True,
    )


class SnapshotVerifier:
    model_name = "complex-case-offline-verifier-snapshot"

    def __init__(self, package):
        self.rows = {
            item["proposal_id"]: item for item in package["offline_verifier_snapshots"]
        }
        self.calls = 0

    def generate(self, **kwargs):
        self.calls += 1
        payload = kwargs["payload"]
        row = self.rows[payload["claim"]["claim_id"]]
        document = next(
            item
            for item in payload["documents"]
            if item["document_id"] == row["document_id"]
        )
        fragment = document["fragments"][row["fragment_index"]]
        entity = document["fragments"][row["entity_fragment_index"]]
        return _model_result(
            VerificationDraft(
                results=[
                    EvidenceVerdict(
                        document_id=document["document_id"],
                        subject_id=payload["claim"]["subject_id"],
                        relation=row["relation"],
                        excerpt=fragment["text"],
                        location=fragment["location"],
                        entity_excerpt=row["entity_excerpt"],
                        entity_location=entity["location"],
                        reason="冻结的离线核验响应回放；仍经过实际原文和实体门禁。",
                    )
                ]
            ),
            self.model_name,
        )


class SnapshotCoordinator:
    """Select next missing receipt from current context, not a fixed action path."""

    model_name = "complex-case-offline-coordinator-snapshot"

    def __init__(self, package, *, fixed_pass=False):
        self.package, self.fixed_pass = package, fixed_pass
        self.calls = 0
        self.actions = []
        self.rows = (
            package["offline_verifier_snapshots"][:2]
            if fixed_pass
            else package["offline_verifier_snapshots"]
        )

    def generate(self, **kwargs):
        self.calls += 1
        context = kwargs["payload"]["evidence_context"]
        if not context["financial_results"]:
            output = self.action(
                "compute_metrics",
                {
                    "metric_ids": list(FORMULAS),
                    "input_refs": [context["financial_inputs"][0]["reference"]],
                    "accounting_basis": "CONSOLIDATED",
                },
            )
        else:
            claims = {
                claim["claim_id"]: (bundle["reference"], claim)
                for bundle in context["bundles"]
                for claim in bundle["claims"]
            }
            for claim in context.get("verified_claim_index", []):
                claims.setdefault(claim["claim_id"], (claim["reference"], claim))
            missing = next(
                (
                    row
                    for row in self.rows
                    if claims.get(row["proposal_id"], (None, {}))[1].get("status")
                    != "SUPPORTED"
                ),
                None,
            )
            if missing:
                bundle = next(
                    (
                        item
                        for item in reversed(context["bundles"])
                        if any(
                            doc["document_id"] == missing["document_id"]
                            for doc in item["documents"]
                        )
                    ),
                    None,
                )
                bundle_reference = (
                    bundle["reference"]
                    if bundle is not None
                    else next(
                        reference
                        for reference in reversed(
                            kwargs["payload"]["available_references"]
                        )
                        if reference.startswith("artifacts/agent_evidence_v")
                    )
                )
                proposal = ClaimProposal.model_validate(
                    {
                        key: missing[key]
                        for key in (
                            "proposal_id",
                            "question_id",
                            "statement",
                            "kind",
                            "attributed_to",
                            "finding_aspect",
                        )
                        if key in missing
                    }
                    | {"source_document_ids": [missing["document_id"]]}
                )
                if missing["document_id"] not in context["read_document_ids"]:
                    output = self.action(
                        "read_document",
                        {
                            "reference_id": bundle_reference,
                            "document_id": missing["document_id"],
                        },
                    )
                else:
                    output = self.action(
                        "verify_claim",
                        {
                            "claim_id": missing["proposal_id"],
                            "source_refs": [bundle_reference],
                            "document_ids": [missing["document_id"]],
                        },
                        [proposal],
                    )
            else:
                output = self.finish(context, claims)
        return _model_result(output, self.model_name)

    def action(self, tool, arguments, proposals=None):
        self.actions.append({"tool": tool, "arguments": arguments})
        return DecisionDraft(
            decision="ACTION",
            tool=tool,
            arguments=arguments,
            claim_proposals=proposals or [],
            expected_observation="读取实际冻结片段、保存计算或取得核验回执。",
            reason_summary="离线回放当前缺失的证据步骤。",
        )

    def finish(self, context, claims):
        result = context["financial_results"][-1]
        financial_id = next(
            question["question_id"]
            for question in self.package["questions"]
            if question["focus"] == "cash_quality"
        )
        assessments = []
        for question in self.package["questions"]:
            selected = [
                row
                for row in self.rows
                if row["question_id"] == question["question_id"]
            ]
            if not selected:
                continue
            assessments.append(
                QuestionAssessment(
                    question_id=question["question_id"],
                    status="ANSWERED"
                    if question["question_id"] == financial_id
                    else "UNRESOLVED",
                    conclusion="；".join(row["statement"] for row in selected),
                    evidence_refs=sorted(
                        {claims[row["proposal_id"]][0] for row in selected}
                    ),
                    claim_ids=[row["proposal_id"] for row in selected],
                    limitations=[]
                    if question["question_id"] == financial_id
                    else [
                        "已核验的是声明或报道的指定断言，仍缺独立因果和合同履行证据。"
                    ],
                )
            )
        return DecisionDraft(
            decision="FINISH",
            finish_reason="NEEDS_REVIEW",
            reason_summary="保存已定位的回答并披露剩余缺口，等待人工审核。",
            question_assessments=assessments,
            review_required=True,
            gap_question_ids=[
                question["question_id"]
                for question in self.package["questions"]
                if question["question_id"] != financial_id
            ],
            financial_citations=[
                FinancialCitation(
                    question_id=financial_id,
                    result_ref=result["reference"],
                    result_index=index,
                )
                for index in range(len(result["results"]))
            ],
            limitations=[
                "离线快照不能证明真实模型调查质量。",
                "不能据材料认定逾期、违法、现金危机或承诺已履行。",
            ],
        )


class PooledTrialModel:
    """Independent durable ledger for the entire explicitly approved trial."""

    def __init__(self, model, directory, authorization):
        self.model, self.authorization = model, authorization
        self.model_name = model.model_name
        self.request_reservation_multiplier = model.request_reservation_multiplier
        self.token_reservation_multiplier = model.token_reservation_multiplier
        self.ledger = ActionLedger(directory / "shared_budget.db")
        self.store = ArtifactStore(directory / "shared_calls")
        self.calls = 0
        self.started = perf_counter()

    def _request_token_bound(self, kwargs) -> int:
        """Conservatively cover every authorized attempt before dispatch."""
        output_schema = kwargs.get("output_schema")
        system_prompt = kwargs.get("system_prompt")
        if output_schema is None or not isinstance(system_prompt, str):
            return 0
        max_output = (
            kwargs.get("max_output_tokens")
            or self.authorization.limits.decision_max_output_tokens
        )
        request_body = json.dumps(
            {
                "system_prompt": system_prompt,
                "purpose": kwargs.get("purpose"),
                "prompt_version": kwargs.get("prompt_version"),
                "input": kwargs.get("payload"),
                "public_process_summary": None,
                "output_json_schema": output_schema.model_json_schema(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        # Byte length is a conservative tokenizer-independent ceiling for the
        # request text. Extra output space covers a repair turn and framing.
        per_attempt = len(request_body.encode("utf-8")) + max_output * 8 + 1_024
        return (
            per_attempt
            * self.authorization.limits.model_attempt_reservation
            * self.request_reservation_multiplier
        )

    def generate(self, **kwargs):
        from credra_agent.observability.runtime import require_logging

        require_logging()
        prior = (
            self.ledger.budget_record("complex-case-shared", f"call-{self.calls}")
            if self.calls
            else None
        )
        if prior is not None and prior.status in {"DISPATCHED", "UNCERTAIN"}:
            raise StructuredModelError(
                "MODEL_ERROR",
                "trial request result is uncertain; no new dispatch",
                external_requests=0,
            )
        if perf_counter() - self.started >= self.authorization.active_seconds_limit:
            raise StructuredModelError(
                "MODEL_ERROR", "trial active time exhausted", external_requests=0
            )
        external, tokens = model_reservation(self, self.authorization.limits)
        tokens = max(tokens, self._request_token_bound(kwargs))
        self.calls += 1
        operation = f"call-{self.calls}"
        try:
            self.ledger.reserve_budget(
                task_id="complex-case-shared",
                operation_id=operation,
                phase="MODEL",
                external=external,
                tokens=tokens,
                authorization=self.authorization,
            )
        except DurableBudgetExhausted as exc:
            raise StructuredModelError(
                "MODEL_ERROR", "trial shared budget exhausted", external_requests=0
            ) from exc
        self.ledger.mark_budget_dispatched("complex-case-shared", operation)
        started = perf_counter()
        try:
            result = self.model.generate(**kwargs)
        except StructuredModelError as exc:
            self.ledger.settle_budget(
                "complex-case-shared",
                operation,
                actual_external=exc.external_requests,
                actual_tokens=0 if exc.external_requests == 0 else None,
                active_seconds=perf_counter() - started,
            )
            raise
        except BaseException:
            self.ledger.mark_budget_uncertain("complex-case-shared", operation)
            raise
        try:
            reference = self.store.write_json(
                f"artifacts/agent_trial_call_v{self.calls}.json",
                {
                    "purpose": kwargs["purpose"],
                    "model_name": result.model_name,
                    "output": result.output.model_dump(mode="json"),
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "external_requests": result.external_requests,
                    "accounting_complete": result.accounting_complete,
                },
            )
        except BaseException:
            self.ledger.mark_budget_uncertain("complex-case-shared", operation)
            raise
        self.ledger.settle_budget(
            "complex-case-shared",
            operation,
            actual_external=external_usage(result),
            actual_tokens=result.input_tokens + result.output_tokens
            if result.accounting_complete
            and result.input_tokens is not None
            and result.output_tokens is not None
            else None,
            result_ref=reference,
            active_seconds=perf_counter() - started,
        )
        return result


class FixedTrialCoordinator(SnapshotCoordinator):
    """Fixed annual route, real verifier, one real final assessment request."""

    def __init__(self, package, model):
        super().__init__(package, fixed_pass=True)
        self.real_model = model
        self.read_documents = set()
        self.completed_verifications = set()
        self.model_name = model.model_name
        self.request_reservation_multiplier = model.request_reservation_multiplier
        self.token_reservation_multiplier = model.token_reservation_multiplier

    def generate(self, **kwargs):
        self.final_request = kwargs
        context = kwargs["payload"]["evidence_context"]
        if not context["financial_results"]:
            output = self.action(
                "compute_metrics",
                {
                    "metric_ids": list(FORMULAS),
                    "input_refs": [context["financial_inputs"][0]["reference"]],
                    "accounting_basis": "CONSOLIDATED",
                },
            )
        else:
            row = next(
                (
                    item
                    for item in self.rows
                    if item["proposal_id"] not in self.completed_verifications
                ),
                None,
            )
            if row is None:
                output = self.finish(context, {})
            else:
                bundle = next(
                    item
                    for item in reversed(context["bundles"])
                    if any(
                        doc["document_id"] == row["document_id"]
                        for doc in item["documents"]
                    )
                )
                proposal = ClaimProposal.model_validate(
                    {
                        key: row[key]
                        for key in (
                            "proposal_id",
                            "question_id",
                            "statement",
                            "kind",
                            "attributed_to",
                        )
                        if key in row
                    }
                    | {"source_document_ids": [row["document_id"]]}
                )
                if row["document_id"] not in self.read_documents:
                    self.read_documents.add(row["document_id"])
                    output = self.action(
                        "read_document",
                        {
                            "reference_id": bundle["reference"],
                            "document_id": row["document_id"],
                        },
                    )
                else:
                    self.completed_verifications.add(row["proposal_id"])
                    output = self.action(
                        "verify_claim",
                        {
                            "claim_id": row["proposal_id"],
                            "source_refs": [bundle["reference"]],
                            "document_ids": [row["document_id"]],
                        },
                        [proposal],
                    )
        result = _model_result(output, self.model_name)
        if hasattr(self, "final_result"):
            result = self.final_result
            del self.final_result
            return result
        return replace(result, input_tokens=0, output_tokens=0, external_requests=0)

    def finish(self, context, claims):
        request = {
            **self.final_request,
            "system_prompt": self.final_request["system_prompt"]
            + "\n固定单轮基线已结束预定年报核对；本轮只允许FINISH，保留未调查问题和来源缺口。不得派发新动作。",
        }
        result = self.real_model.generate(**request)
        if result.output.decision != "FINISH":
            raise StructuredModelError(
                "INVALID_OUTPUT",
                "fixed baseline attempted another action",
                external_requests=result.external_requests,
            )
        self.final_result = result
        return result.output


def check_report(
    report: dict,
    package: dict,
    *,
    required_finding_counts: dict[str, int] | None = None,
) -> list[dict]:
    """Financial truth is independently evaluated from frozen annual amounts."""
    checks = []
    finding_counts = (
        required_finding_counts
        if required_finding_counts is not None
        else {
            item["question_id"]: item["minimum_verified_findings"]
            for item in package["questions"]
            if item.get("minimum_verified_findings", 0) > 0
        }
    )
    with localcontext() as context:
        context.prec = 50
        expected = {}
        for metric_id, row in package["expected_financial"].items():
            expected[metric_id] = (
                expected["receivables_growth"] - expected["revenue_growth"]
                if metric_id == "growth_gap"
                else Decimal(row["numerator"]) / Decimal(row["denominator"])
                - int(row["subtract_one"])
            )
        actual = {
            item["metric"]["metric_id"]: item["metric"]
            for item in report["financial_citations"]
        }
        for metric_id, value in expected.items():
            metric = actual.get(metric_id, {})
            try:
                passed = (
                    metric.get("status") == "COMPUTED"
                    and metric.get("display")
                    == package["expected_financial"][metric_id]["display"]
                    and abs(Decimal(metric["value"]) - value)
                    <= Decimal(package["absolute_ratio_tolerance"])
                )
            except (DecimalException, KeyError, TypeError, ValueError):
                passed = False
            checks.append(
                {
                    "check_id": f"financial.{metric_id}",
                    "status": "PASS" if passed else "FAIL",
                }
            )
    for finding in report["findings"]:
        checks.append(
            {
                "check_id": f"evidence.{finding['claim_id']}",
                "status": "PASS"
                if (
                    finding["status"] in {"UNRESOLVED", "UNVERIFIED"}
                    and not finding["receipts"]
                )
                or (
                    finding["status"] in {"SUPPORTED", "REFUTED", "CONFLICTING"}
                    and finding["receipts"]
                    and all(
                        item["fragment_ref"]
                        and item["published_at"] <= package["as_of"]
                        and item["source_kind"] == "REAL"
                        for item in finding["receipts"]
                    )
                )
                else "FAIL",
            }
        )
    finding_claims_by_question: dict[str, set[str]] = {}
    for finding in report["findings"]:
        if (
            finding["status"] in {"SUPPORTED", "REFUTED", "CONFLICTING"}
            and finding["receipts"]
        ):
            finding_claims_by_question.setdefault(finding["question_id"], set()).add(
                finding["claim_id"]
            )
    for question_id, required in sorted(finding_counts.items()):
        actual = len(finding_claims_by_question.get(question_id, set()))
        if actual < required:
            checks.append(
                {
                    "check_id": f"evidence.required_question:{question_id}",
                    "status": "FAIL",
                    "reason": "INSUFFICIENT_VERIFIED_FINDINGS",
                    "actual": actual,
                    "required": required,
                }
            )
    receipt_requirements = (
        package.get("required_finding_receipts", [])
        if required_finding_counts != {}
        else []
    )
    for requirement in receipt_requirements:
        matched = any(
            finding["question_id"] == requirement["question_id"]
            and finding["status"] in {"SUPPORTED", "REFUTED", "CONFLICTING"}
            and any(
                receipt["document_id"] == requirement["document_id"]
                and (
                    not requirement.get("location")
                    or receipt["location"]["location"] == requirement["location"]
                )
                for receipt in finding["receipts"]
            )
            for finding in report["findings"]
        )
        checks.append(
            {
                "check_id": "evidence.required_receipt:"
                + requirement["requirement_id"],
                "status": "PASS" if matched else "FAIL",
                **({} if matched else {"reason": "MISSING_REQUIRED_RECEIPT"}),
            }
        )
    checks.append(
        {
            "check_id": "review.required",
            "status": "PASS"
            if report["approval_status"] == "NOT_REVIEWED"
            and report["coverage"]["review_required"]
            else "FAIL",
        }
    )
    return checks


def run_complex_case_replay(
    project_root: Path,
    output_dir: Path,
    *,
    include_baseline=False,
    trial_settings=None,
    trial_authorization=None,
):
    project_root, output_dir = project_root.resolve(), output_dir.resolve()
    if not output_dir.is_relative_to(project_root):
        raise ValueError("complex-case replay output must stay in the project")
    package = load_package(project_root)
    source = project_root / "data" / package["case_id"] / "source"
    fingerprint = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(source.iterdir())
        if path.is_file()
    }
    if {name: "sha256:" + value for name, value in fingerprint.items()} != package[
        "material_files"
    ]:
        raise ValueError(
            "complex-case material fingerprint differs from the frozen package"
        )
    directory = output_dir / ("complex-case-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    pooled_model = None
    if trial_settings is not None:
        if trial_authorization is None or trial_authorization.approval != "APPROVED":
            raise ValueError(
                "real complex-case trial requires explicit approved authorization"
            )
        if (
            trial_settings.analysis_llm_max_retry + 1
            > trial_authorization.limits.model_attempt_reservation
        ):
            raise ValueError(
                "trial gateway attempts exceed the approved model reservation"
            )
        model = build_analysis_model(
            trial_settings,
            process_max_output_tokens=trial_authorization.limits.decision_max_output_tokens,
            aggregate_accounting=True,
        )
        if model is None:
            raise ValueError("real complex-case trial requires configured model")
        pooled_model = PooledTrialModel(model, directory, trial_authorization)
    results = {}
    for mode in (
        ["fixed_single_pass", "agentic_replay"]
        if include_baseline
        else ["agentic_replay"]
    ):
        root = directory / mode
        target = root / "data" / package["case_id"] / "source"
        shutil.copytree(source, target)
        task = case_task(package)
        model, verifier = (
            SnapshotCoordinator(package, fixed_pass=mode == "fixed_single_pass"),
            SnapshotVerifier(package),
        )
        if pooled_model:
            model = (
                FixedTrialCoordinator(package, pooled_model)
                if mode == "fixed_single_pass"
                else pooled_model
            )
            verifier = pooled_model
        settings = Settings(
            _env_file=None,
            data_dir=root / "data",
            checkpoint_db_path=root / "tasks.db",
            trace_dir=root / "traces",
            service_log_dir=root / "logs",
            analysis_mode="deterministic",
            intent_mode="deterministic",
            research_provider="mock",
            content_fetch_provider="disabled",
            model_api_key="",
        )
        thread = "complex-case-" + mode
        if trial_settings is not None:
            settings = trial_settings.model_copy(
                update={
                    "data_dir": root / "data",
                    "checkpoint_db_path": root / "tasks.db",
                    "trace_dir": root / "traces",
                    "service_log_dir": root / "logs",
                    "research_provider": "mock",
                    "content_fetch_provider": "disabled",
                }
            )
        result = start_agentic_task(
            thread_id=thread,
            task_spec=task,
            authorization=trial_authorization or offline_authorization(),
            settings=settings,
            model=model,
            executor=build_agentic_executor(
                task, verifier_model=verifier, search_enabled=False
            ),
            initial_evidence_bundle=load_case_evidence(
                target.parent,
                as_of=task.as_of,
                subject_id=task.subject_id,
            ),
            initial_financial_input=load_case_financial(
                target.parent, subject_id=task.subject_id
            ),
        )
        state = result["state"]
        store = ArtifactStore(
            root / "data" / package["case_id"] / "runs" / run_id_for_thread(thread)
        )
        report = (
            store.read_json(state["report_ref"].replace(".md", ".json"))
            if state.get("report_ref")
            else {"findings": []}
        )
        checks = (
            check_report(
                report,
                package,
                required_finding_counts={
                    item.question_id: item.minimum_verified_findings
                    for item in task.questions
                    if item.minimum_verified_findings > 0
                }
                if mode == "agentic_replay"
                else {},
            )
            if state.get("report_ref")
            else [
                {
                    "check_id": "runtime.report",
                    "status": "FAIL",
                    "reason": state["stop_reason"],
                }
            ]
        )
        results[mode] = {
            "status": state["status"],
            "stop_reason": state["stop_reason"],
            "report_ref": state["report_ref"],
            "report_path": str(store.run_dir / state["report_ref"])
            if state.get("report_ref")
            else None,
            "checks": checks,
            "actions": ActionLedger(settings.checkpoint_db_path).dump_actions(thread),
            "model_snapshot_requests": model.calls + verifier.calls
            if not pooled_model
            else 0,
            "material_fingerprint": fingerprint,
            "claim_ids": [item["claim_id"] for item in report["findings"]],
        }
    summary = {
        "schema_version": "complex_case_replay_result_v1",
        "suite_id": package["suite_id"],
        "execution_mode": "REAL_INVESTIGATION_WITH_FROZEN_MATERIAL"
        if pooled_model
        else "OFFLINE_SNAPSHOT_REPLAY",
        "actual_provider_requests": pooled_model.ledger.budget_snapshot(
            "complex-case-shared", trial_authorization
        )["external_spent"]
        if pooled_model
        else 0,
        "human_review_status": "NOT_REVIEWED",
        "real_model_quality": "PENDING_HUMAN_REVIEW" if pooled_model else "NOT_RUN",
        "g2_status": "PENDING_HUMAN_AND_REAL_QUALITY",
        "package_hash": hashlib.sha256(
            (project_root / "evals/suites/byd_cash_quality_v2.json").read_bytes()
        ).hexdigest(),
        "runs": results,
        "technical_status": "PASS"
        if all(
            check["status"] == "PASS"
            for result in results.values()
            for check in result["checks"]
        )
        else "FAIL",
    }
    if pooled_model:
        summary["shared_budget"] = pooled_model.ledger.budget_snapshot(
            "complex-case-shared", trial_authorization
        )
    path = directory / "result.json"
    path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return summary, path
