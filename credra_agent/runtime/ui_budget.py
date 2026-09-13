"""Persist intent request reservations and replay their results before graph start."""

from time import perf_counter

from app.llm.gateway import StructuredModelError, StructuredModelResult
from credra_agent.execution.ledger import ActionLedger
from credra_agent.execution.model_budget import external_usage
from credra_agent.observability.runtime import emit, require_logging
from credra_agent.runtime.ui_store import UIRequestBlocked, UITaskStore


class BudgetedIntentModel:
    def __init__(
        self, model, *, store, ledger, thread_id, message_id, policy, authorization
    ):
        self.model = model
        self.model_name = model.model_name
        self.store: UITaskStore = store
        self.ledger: ActionLedger = ledger
        self.thread_id = thread_id
        self.operation_id = f"intent:{message_id}"
        self.policy = policy
        self.authorization = authorization

    def generate(self, **kwargs):
        saved = self.store.model_result(self.thread_id, self.operation_id)
        record = self.ledger.budget_record(self.thread_id, self.operation_id)
        if saved is None:
            if record is not None and record.status in {
                "DISPATCHED",
                "UNCERTAIN",
                "SETTLED",
            }:
                if record.status == "DISPATCHED":
                    self.ledger.mark_budget_uncertain(self.thread_id, self.operation_id)
                raise UIRequestBlocked(
                    "INTENT_REQUEST_RESULT_UNCERTAIN: 不会自动重发解析请求"
                )
            require_logging()
            self.ledger.reserve_budget(
                task_id=self.thread_id,
                operation_id=self.operation_id,
                phase="MODEL",
                external=self.policy.intent_attempt_reservation,
                tokens=self.policy.intent_token_reservation,
                authorization=self.authorization,
            )
            self.ledger.mark_budget_dispatched(self.thread_id, self.operation_id)
            started = perf_counter()
            try:
                result = self.model.generate(
                    **kwargs, max_output_tokens=self.policy.intent_max_output_tokens
                )
                complete = (
                    result.accounting_complete
                    and result.input_tokens is not None
                    and result.output_tokens is not None
                )
                saved = {
                    "output": result.output.model_dump(mode="json"),
                    "error": None,
                    "external": external_usage(result),
                    "tokens": result.input_tokens + result.output_tokens
                    if complete
                    else None,
                    "attempts": result.attempts,
                    "active_seconds": perf_counter() - started,
                }
            except StructuredModelError as exc:
                saved = {
                    "output": None,
                    "error": exc.code,
                    "external": exc.external_requests,
                    "tokens": 0 if exc.external_requests == 0 else None,
                    "attempts": exc.attempts,
                    "active_seconds": perf_counter() - started,
                }
            except BaseException:
                self.ledger.mark_budget_uncertain(self.thread_id, self.operation_id)
                raise
            # Store returned output before accounting/interpretation: a crash here
            # can replay this result instead of sending another paid request.
            self.store.save_model_result(self.thread_id, self.operation_id, saved)
        self.ledger.settle_budget(
            self.thread_id,
            self.operation_id,
            actual_external=saved["external"],
            actual_tokens=saved["tokens"],
            active_seconds=saved["active_seconds"],
            result_ref=f"sqlite:credra_ui_model_results:{self.operation_id}",
        )
        emit(
            "BUDGET_STATE",
            status="UNKNOWN" if saved["tokens"] is None else "SUCCESS",
            node="intent",
            external_requests=saved["external"]
            if saved["external"] is not None
            else self.policy.intent_attempt_reservation,
            token_units=saved["tokens"]
            if saved["tokens"] is not None
            else self.policy.intent_token_reservation,
        )
        if saved["error"]:
            raise StructuredModelError(
                saved["error"],
                "受控解析失败，使用规则降级",
                attempts=saved["attempts"],
                external_requests=saved["external"],
            )
        return StructuredModelResult(
            output=kwargs["output_schema"].model_validate(saved["output"]),
            model_name=self.model_name,
            attempts=saved["attempts"],
            latency_ms=0,
            external_requests=saved["external"],
        )
