"""Concurrency-safe reserve/settle budget accounting for agentic execution."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from time import perf_counter
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class BudgetError(RuntimeError):
    pass


class BudgetUnavailable(BudgetError):
    pass


class BudgetExhausted(BudgetError):
    pass


class BudgetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approval: Literal["UNCONFIRMED", "APPROVED"]
    external_limit: int | None
    token_limit: int | None
    active_seconds_limit: float | None
    external_spent: int
    token_spent: int
    external_reserved: int
    token_reserved: int
    remaining_external: int | None
    remaining_tokens: int | None
    active_seconds: float
    usage_uncertain: bool


class BudgetAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["budget_audit_v1"] = "budget_audit_v1"
    authorization_id: str = Field(min_length=1)
    phase: Literal["MODEL", "TOOL"]
    decision_number: int = Field(ge=1)
    action_id: str | None = None
    status: Literal["SETTLED", "SETTLED_UNCERTAIN", "REJECTED"]
    requested_external: int = Field(ge=0)
    requested_tokens: int = Field(ge=0)
    actual_external: int | None = Field(default=None, ge=0)
    actual_tokens: int | None = Field(default=None, ge=0)
    error_code: str | None = None
    before: BudgetSnapshot
    after: BudgetSnapshot


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    external: int
    tokens: int


class BudgetLedger:
    """Budget is unusable until approval and every cap are explicitly supplied."""

    def __init__(
        self,
        *,
        approval: Literal["UNCONFIRMED", "APPROVED"],
        external_limit: int | None = None,
        token_limit: int | None = None,
        active_seconds_limit: float | None = None,
        clock=perf_counter,
    ) -> None:
        if approval == "APPROVED" and (
            external_limit is None
            or token_limit is None
            or active_seconds_limit is None
        ):
            raise ValueError(
                "approved budgets require explicit external/token/time caps"
            )
        for value in (external_limit, token_limit, active_seconds_limit):
            if value is not None and value <= 0:
                raise ValueError("budget caps must be positive")
        self.approval = approval
        self.external_limit = external_limit
        self.token_limit = token_limit
        self.active_seconds_limit = active_seconds_limit
        self._clock = clock
        self._started = clock()
        self._external_spent = 0
        self._token_spent = 0
        self._reservations: dict[str, Reservation] = {}
        self._usage_uncertain = False
        self._lock = RLock()

    def reserve(self, *, external: int = 0, tokens: int = 0) -> Reservation:
        if external < 0 or tokens < 0:
            raise ValueError("reservation cannot be negative")
        with self._lock:
            if self.approval != "APPROVED":
                raise BudgetUnavailable("BUDGET_UNCONFIRMED")
            snapshot = self.snapshot()
            if (
                snapshot.active_seconds_limit is not None
                and snapshot.active_seconds >= snapshot.active_seconds_limit
            ):
                raise BudgetExhausted("ACTIVE_TIME_EXHAUSTED")
            if (
                snapshot.remaining_external is not None
                and external > snapshot.remaining_external
            ):
                raise BudgetExhausted("EXTERNAL_REQUEST_BUDGET_EXHAUSTED")
            if (
                snapshot.remaining_tokens is not None
                and tokens > snapshot.remaining_tokens
            ):
                raise BudgetExhausted("TOKEN_BUDGET_EXHAUSTED")
            item = Reservation(uuid4().hex, external, tokens)
            self._reservations[item.reservation_id] = item
            return item

    def settle(
        self,
        reservation: Reservation,
        *,
        actual_external: int | None = None,
        actual_tokens: int | None = None,
    ) -> None:
        with self._lock:
            current = self._reservations.pop(reservation.reservation_id, None)
            if current is None:
                raise BudgetError("UNKNOWN_OR_SETTLED_RESERVATION")
            external = current.external if actual_external is None else actual_external
            tokens = current.tokens if actual_tokens is None else actual_tokens
            if external < 0 or tokens < 0:
                raise ValueError("actual usage cannot be negative")
            self._external_spent += external
            self._token_spent += tokens
            if actual_external is None or actual_tokens is None:
                self._usage_uncertain = True

    def cancel(self, reservation: Reservation) -> None:
        with self._lock:
            if self._reservations.pop(reservation.reservation_id, None) is None:
                raise BudgetError("UNKNOWN_OR_SETTLED_RESERVATION")

    def snapshot(self) -> BudgetSnapshot:
        with self._lock:
            external_reserved = sum(
                item.external for item in self._reservations.values()
            )
            token_reserved = sum(item.tokens for item in self._reservations.values())
            return BudgetSnapshot(
                approval=self.approval,
                external_limit=self.external_limit,
                token_limit=self.token_limit,
                active_seconds_limit=self.active_seconds_limit,
                external_spent=self._external_spent,
                token_spent=self._token_spent,
                external_reserved=external_reserved,
                token_reserved=token_reserved,
                remaining_external=None
                if self.external_limit is None
                else max(
                    0, self.external_limit - self._external_spent - external_reserved
                ),
                remaining_tokens=None
                if self.token_limit is None
                else max(0, self.token_limit - self._token_spent - token_reserved),
                active_seconds=max(0.0, self._clock() - self._started),
                usage_uncertain=self._usage_uncertain,
            )
