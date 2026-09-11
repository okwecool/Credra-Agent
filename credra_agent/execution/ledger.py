"""Durable, transaction-bound action and budget records for agentic recovery."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from credra_agent.execution.models import Action
from credra_agent.planning.models import RunAuthorization

ActionStatus = Literal["RESERVED", "DISPATCHED", "RESULT_STORED", "UNCERTAIN", "FAILED"]
BudgetStatus = Literal["RESERVED", "DISPATCHED", "SETTLED", "UNCERTAIN", "CANCELLED"]


class LedgerError(RuntimeError):
    pass


class InvalidLedgerTransition(LedgerError):
    pass


class DurableBudgetExhausted(LedgerError):
    pass


class ExecutionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["execution_record_v1"] = "execution_record_v1"
    task_id: str = Field(min_length=1)
    action: Action
    signature: str = Field(min_length=64, max_length=64)
    decision_ref: str = Field(min_length=1)
    budget_operation_id: str = Field(min_length=1)
    requested_external: int = Field(ge=0)
    attempt: int = Field(ge=1)
    status: ActionStatus
    observation_ref: str | None = None
    result_fingerprint: str | None = None
    usage_uncertain: bool = False
    error_code: str | None = None
    created_at: str
    updated_at: str

    @model_validator(mode="after")
    def coherent(self) -> ExecutionRecord:
        if self.status == "RESULT_STORED" and (
            not self.observation_ref or not self.result_fingerprint
        ):
            raise ValueError("stored result requires its reference and fingerprint")
        if self.status == "UNCERTAIN" and not self.usage_uncertain:
            raise ValueError("uncertain dispatch requires conservative accounting")
        return self


class BudgetRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str
    task_id: str
    phase: Literal["MODEL", "TOOL"]
    status: BudgetStatus
    requested_external: int = Field(ge=0)
    requested_tokens: int = Field(ge=0)
    actual_external: int | None = Field(default=None, ge=0)
    actual_tokens: int | None = Field(default=None, ge=0)
    active_seconds: float = Field(default=0, ge=0)
    result_ref: str | None = None
    usage_uncertain: bool = False


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ActionLedger:
    """SQLite ledger whose rows survive graph-node and process failure."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS credra_agent_actions (
                    task_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    plan_version INTEGER NOT NULL,
                    signature TEXT NOT NULL,
                    action_json TEXT NOT NULL,
                    decision_ref TEXT NOT NULL,
                    budget_operation_id TEXT NOT NULL,
                    requested_external INTEGER NOT NULL,
                    attempt INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    observation_ref TEXT,
                    result_fingerprint TEXT,
                    usage_uncertain INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, action_id),
                    UNIQUE (task_id, signature),
                    UNIQUE (task_id, plan_version)
                );
                CREATE TABLE IF NOT EXISTS credra_agent_budget (
                    task_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_external INTEGER NOT NULL,
                    requested_tokens INTEGER NOT NULL,
                    actual_external INTEGER,
                    actual_tokens INTEGER,
                    active_seconds REAL NOT NULL DEFAULT 0,
                    result_ref TEXT,
                    usage_uncertain INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, operation_id)
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(credra_agent_budget)"
                ).fetchall()
            }
            if "active_seconds" not in columns:
                connection.execute(
                    "ALTER TABLE credra_agent_budget "
                    "ADD COLUMN active_seconds REAL NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _record(row: sqlite3.Row) -> ExecutionRecord:
        return ExecutionRecord(
            task_id=row["task_id"],
            action=Action.model_validate_json(row["action_json"]),
            signature=row["signature"],
            decision_ref=row["decision_ref"],
            budget_operation_id=row["budget_operation_id"],
            requested_external=row["requested_external"],
            attempt=row["attempt"],
            status=row["status"],
            observation_ref=row["observation_ref"],
            result_fingerprint=row["result_fingerprint"],
            usage_uncertain=bool(row["usage_uncertain"]),
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _budget_record(row: sqlite3.Row) -> BudgetRecord:
        return BudgetRecord(
            operation_id=row["operation_id"],
            task_id=row["task_id"],
            phase=row["phase"],
            status=row["status"],
            requested_external=row["requested_external"],
            requested_tokens=row["requested_tokens"],
            actual_external=row["actual_external"],
            actual_tokens=row["actual_tokens"],
            active_seconds=row["active_seconds"],
            result_ref=row["result_ref"],
            usage_uncertain=bool(row["usage_uncertain"]),
        )

    def reserve_action(
        self,
        *,
        task_id: str,
        action: Action,
        signature: str,
        decision_ref: str,
        requested_external: int,
    ) -> ExecutionRecord:
        operation_id = f"action:{action.action_id}"
        now = _now()
        serialized = action.model_dump_json()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM credra_agent_actions WHERE task_id=? AND plan_version=?",
                (task_id, action.plan_version),
            ).fetchone()
            if row is not None:
                current = self._record(row)
                if current.action != action or current.signature != signature:
                    raise LedgerError("PLAN_VERSION_ALREADY_BOUND")
                connection.commit()
                return current
            try:
                connection.execute(
                    """INSERT INTO credra_agent_actions VALUES
                    (?, ?, ?, ?, ?, ?, ?, ?, 1, 'RESERVED', NULL, NULL, 0, NULL, ?, ?)""",
                    (
                        task_id,
                        action.action_id,
                        action.plan_version,
                        signature,
                        serialized,
                        decision_ref,
                        operation_id,
                        requested_external,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise LedgerError("ACTION_SIGNATURE_ALREADY_BOUND") from exc
            connection.commit()
        return self.get_action(task_id, action.action_id)

    def get_action(self, task_id: str, action_id: str) -> ExecutionRecord:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM credra_agent_actions WHERE task_id=? AND action_id=?",
                (task_id, action_id),
            ).fetchone()
        if row is None:
            raise LedgerError("ACTION_NOT_FOUND")
        return self._record(row)

    def action_for_plan(
        self, task_id: str, plan_version: int
    ) -> ExecutionRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM credra_agent_actions WHERE task_id=? AND plan_version=?",
                (task_id, plan_version),
            ).fetchone()
        return self._record(row) if row is not None else None

    def signatures(self, task_id: str) -> set[str]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT signature FROM credra_agent_actions WHERE task_id=?",
                (task_id,),
            ).fetchall()
        return {row["signature"] for row in rows}

    def _transition_action(
        self,
        task_id: str,
        action_id: str,
        *,
        expected: set[str],
        target: ActionStatus,
        observation_ref: str | None = None,
        result_fingerprint: str | None = None,
        error_code: str | None = None,
        uncertain: bool = False,
    ) -> ExecutionRecord:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM credra_agent_actions WHERE task_id=? AND action_id=?",
                (task_id, action_id),
            ).fetchone()
            if row is None:
                raise LedgerError("ACTION_NOT_FOUND")
            if row["status"] not in expected:
                raise InvalidLedgerTransition(f"{row['status']}->{target}")
            connection.execute(
                """UPDATE credra_agent_actions SET status=?, observation_ref=?,
                result_fingerprint=?, error_code=?, usage_uncertain=?, updated_at=?
                WHERE task_id=? AND action_id=?""",
                (
                    target,
                    observation_ref,
                    result_fingerprint,
                    error_code,
                    int(uncertain),
                    _now(),
                    task_id,
                    action_id,
                ),
            )
            connection.commit()
        return self.get_action(task_id, action_id)

    def mark_dispatched(self, task_id: str, action_id: str) -> ExecutionRecord:
        return self._transition_action(
            task_id, action_id, expected={"RESERVED"}, target="DISPATCHED"
        )

    def store_result(
        self,
        task_id: str,
        action_id: str,
        *,
        observation_ref: str,
        result_fingerprint: str,
    ) -> ExecutionRecord:
        return self._transition_action(
            task_id,
            action_id,
            expected={"DISPATCHED", "RESERVED"},
            target="RESULT_STORED",
            observation_ref=observation_ref,
            result_fingerprint=result_fingerprint,
        )

    def mark_uncertain(self, task_id: str, action_id: str) -> ExecutionRecord:
        current = self.get_action(task_id, action_id)
        if current.status == "UNCERTAIN":
            return current
        return self._transition_action(
            task_id,
            action_id,
            expected={"DISPATCHED"},
            target="UNCERTAIN",
            error_code="REQUEST_RESULT_UNCERTAIN",
            uncertain=True,
        )

    def mark_failed(
        self, task_id: str, action_id: str, error_code: str
    ) -> ExecutionRecord:
        return self._transition_action(
            task_id,
            action_id,
            expected={"RESERVED"},
            target="FAILED",
            error_code=error_code,
        )

    def budget_record(self, task_id: str, operation_id: str) -> BudgetRecord | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM credra_agent_budget WHERE task_id=? AND operation_id=?",
                (task_id, operation_id),
            ).fetchone()
        return self._budget_record(row) if row is not None else None

    @staticmethod
    def _usage(
        connection: sqlite3.Connection, task_id: str
    ) -> tuple[int, int, float, bool]:
        row = connection.execute(
            """SELECT
            COALESCE(SUM(CASE WHEN status='CANCELLED' THEN 0
                ELSE COALESCE(actual_external, requested_external) END), 0) AS external,
            COALESCE(SUM(CASE WHEN status='CANCELLED' THEN 0
                ELSE COALESCE(actual_tokens, requested_tokens) END), 0) AS tokens,
            COALESCE(SUM(CASE WHEN status='CANCELLED' THEN 0
                ELSE active_seconds END), 0) AS active_seconds,
            COALESCE(MAX(usage_uncertain), 0) AS uncertain
            FROM credra_agent_budget WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        return (
            int(row["external"]),
            int(row["tokens"]),
            float(row["active_seconds"]),
            bool(row["uncertain"]),
        )

    def reserve_budget(
        self,
        *,
        task_id: str,
        operation_id: str,
        phase: Literal["MODEL", "TOOL"],
        external: int,
        tokens: int,
        authorization: RunAuthorization,
    ) -> BudgetRecord:
        if authorization.approval != "APPROVED":
            raise DurableBudgetExhausted("BUDGET_UNCONFIRMED")
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM credra_agent_budget WHERE task_id=? AND operation_id=?",
                (task_id, operation_id),
            ).fetchone()
            if existing is not None:
                item = self._budget_record(existing)
                if (
                    item.phase != phase
                    or item.requested_external != external
                    or item.requested_tokens != tokens
                ):
                    raise LedgerError("BUDGET_OPERATION_ALREADY_BOUND")
                connection.commit()
                return item
            spent_external, spent_tokens, active_seconds, _ = self._usage(
                connection, task_id
            )
            if active_seconds >= float(authorization.active_seconds_limit or 0):
                raise DurableBudgetExhausted("ACTIVE_TIME_EXHAUSTED")
            if spent_external + external > int(
                authorization.external_request_limit or 0
            ):
                raise DurableBudgetExhausted("EXTERNAL_REQUEST_BUDGET_EXHAUSTED")
            if spent_tokens + tokens > int(authorization.token_limit or 0):
                raise DurableBudgetExhausted("TOKEN_BUDGET_EXHAUSTED")
            connection.execute(
                """INSERT INTO credra_agent_budget VALUES
                (?, ?, ?, 'RESERVED', ?, ?, NULL, NULL, 0, NULL, 0, ?)""",
                (task_id, operation_id, phase, external, tokens, _now()),
            )
            connection.commit()
        item = self.budget_record(task_id, operation_id)
        assert item is not None
        return item

    def mark_budget_dispatched(self, task_id: str, operation_id: str) -> BudgetRecord:
        return self._transition_budget(task_id, operation_id, "RESERVED", "DISPATCHED")

    def settle_budget(
        self,
        task_id: str,
        operation_id: str,
        *,
        actual_external: int | None,
        actual_tokens: int | None,
        result_ref: str | None = None,
        active_seconds: float = 0,
    ) -> BudgetRecord:
        current = self.budget_record(task_id, operation_id)
        if current is None:
            raise LedgerError("BUDGET_OPERATION_NOT_FOUND")
        if current.status == "SETTLED":
            return current
        uncertain = actual_external is None or actual_tokens is None
        return self._transition_budget(
            task_id,
            operation_id,
            "DISPATCHED",
            "SETTLED",
            actual_external=actual_external,
            actual_tokens=actual_tokens,
            result_ref=result_ref,
            active_seconds=active_seconds,
            uncertain=uncertain,
        )

    def attach_budget_result(
        self, task_id: str, operation_id: str, result_ref: str
    ) -> BudgetRecord:
        """Attach a durable fallback decision to an already settled model call."""

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, result_ref FROM credra_agent_budget "
                "WHERE task_id=? AND operation_id=?",
                (task_id, operation_id),
            ).fetchone()
            if row is None:
                raise LedgerError("BUDGET_OPERATION_NOT_FOUND")
            if row["status"] != "SETTLED":
                raise InvalidLedgerTransition(f"{row['status']}->ATTACH_RESULT")
            if row["result_ref"] not in (None, result_ref):
                raise LedgerError("BUDGET_RESULT_ALREADY_BOUND")
            connection.execute(
                """UPDATE credra_agent_budget SET result_ref=?, updated_at=?
                WHERE task_id=? AND operation_id=?""",
                (result_ref, _now(), task_id, operation_id),
            )
            connection.commit()
        item = self.budget_record(task_id, operation_id)
        assert item is not None
        return item

    def mark_budget_uncertain(self, task_id: str, operation_id: str) -> BudgetRecord:
        current = self.budget_record(task_id, operation_id)
        if current is None:
            raise LedgerError("BUDGET_OPERATION_NOT_FOUND")
        if current.status == "UNCERTAIN":
            return current
        return self._transition_budget(
            task_id,
            operation_id,
            "DISPATCHED",
            "UNCERTAIN",
            uncertain=True,
        )

    def _transition_budget(
        self,
        task_id: str,
        operation_id: str,
        expected: str,
        target: BudgetStatus,
        *,
        actual_external: int | None = None,
        actual_tokens: int | None = None,
        result_ref: str | None = None,
        active_seconds: float = 0,
        uncertain: bool = False,
    ) -> BudgetRecord:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM credra_agent_budget WHERE task_id=? AND operation_id=?",
                (task_id, operation_id),
            ).fetchone()
            if row is None:
                raise LedgerError("BUDGET_OPERATION_NOT_FOUND")
            if row["status"] != expected:
                raise InvalidLedgerTransition(f"{row['status']}->{target}")
            connection.execute(
                """UPDATE credra_agent_budget SET status=?, actual_external=?,
                actual_tokens=?, active_seconds=?, result_ref=?, usage_uncertain=?, updated_at=?
                WHERE task_id=? AND operation_id=?""",
                (
                    target,
                    actual_external,
                    actual_tokens,
                    max(0, active_seconds),
                    result_ref,
                    int(uncertain),
                    _now(),
                    task_id,
                    operation_id,
                ),
            )
            connection.commit()
        item = self.budget_record(task_id, operation_id)
        assert item is not None
        return item

    def budget_snapshot(
        self, task_id: str, authorization: RunAuthorization
    ) -> dict[str, int | float | bool | str | None]:
        with self._connection() as connection:
            external, tokens, active_seconds, uncertain = self._usage(
                connection, task_id
            )
        return {
            "approval": authorization.approval,
            "external_limit": authorization.external_request_limit,
            "token_limit": authorization.token_limit,
            "external_spent": external,
            "token_spent": tokens,
            "active_seconds": active_seconds,
            "active_seconds_limit": authorization.active_seconds_limit,
            "remaining_active_seconds": None
            if authorization.active_seconds_limit is None
            else max(0.0, authorization.active_seconds_limit - active_seconds),
            "remaining_external": None
            if authorization.external_request_limit is None
            else max(0, authorization.external_request_limit - external),
            "remaining_tokens": None
            if authorization.token_limit is None
            else max(0, authorization.token_limit - tokens),
            "usage_uncertain": uncertain,
        }

    def dump_actions(self, task_id: str) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM credra_agent_actions WHERE task_id=? ORDER BY plan_version",
                (task_id,),
            ).fetchall()
        return [self._record(row).model_dump(mode="json") for row in rows]
