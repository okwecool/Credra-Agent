"""Conversation reservations/results survive failure; no investigation task_id rows."""

import json
import math

from credra_agent.entry.policy import EntryPolicy
from credra_agent.entry.store import EntryConflict


class EntryBudgetLimited(EntryConflict):
    pass


class EntryRequestUncertain(EntryBudgetLimited):
    pass


class EntryBudgetStore:
    def __init__(self, store):
        self.store = store
        with store.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS credra_entry_policies (
                    conversation_id TEXT PRIMARY KEY,policy_json TEXT NOT NULL,profile_ref TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS credra_entry_turns (
                    message_id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL,fingerprint TEXT NOT NULL,response_json TEXT);
                CREATE TABLE IF NOT EXISTS credra_entry_operations (
                    conversation_id TEXT NOT NULL,operation_id TEXT NOT NULL,status TEXT NOT NULL,
                    requested_external INTEGER NOT NULL,requested_tokens INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,result_json TEXT,actual_external INTEGER,actual_tokens INTEGER,
                    active_seconds REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(conversation_id,operation_id));
                CREATE TABLE IF NOT EXISTS credra_entry_tool_results (
                    conversation_id TEXT NOT NULL,operation_id TEXT NOT NULL,message_id TEXT NOT NULL,
                    status TEXT NOT NULL,result_json TEXT,active_seconds REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(conversation_id,operation_id));
            """)

    def frozen(self, conversation_id):
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT policy_json,profile_ref FROM credra_entry_policies WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None:
            return None
        policy = EntryPolicy.model_validate_json(row[0])
        if policy.approval != "APPROVED":
            raise EntryConflict("ENTRY_STORED_POLICY_INVALID")
        return policy, row[1]

    def bind(self, conversation_id, policy, profile_ref):
        if policy.approval != "APPROVED":
            raise EntryConflict("ENTRY_POLICY_NOT_APPROVED")
        with self.store.connect() as connection:
            connection.execute(
                "INSERT INTO credra_entry_policies VALUES (?,?,?)",
                (conversation_id, policy.model_dump_json(), profile_ref),
            )

    def claim_turn(self, conversation_id, message_id, text):
        import hashlib

        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM credra_entry_turns WHERE message_id=?", (message_id,)
            ).fetchone()
            if row:
                if (
                    row["conversation_id"] != conversation_id
                    or row["fingerprint"] != fingerprint
                ):
                    raise EntryConflict("ENTRY_MESSAGE_ID_CONFLICT")
                return (
                    json.loads(row["response_json"]) if row["response_json"] else None
                )
            connection.execute(
                "INSERT INTO credra_entry_turns VALUES (?,?,?,NULL)",
                (message_id, conversation_id, fingerprint),
            )
        return None

    def save_response(self, message_id, result):
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE credra_entry_turns SET response_json=? WHERE message_id=?",
                (json.dumps(result), message_id),
            )

    @staticmethod
    def _snapshot(connection, conversation_id, policy):
        rows = connection.execute(
            "SELECT * FROM credra_entry_operations WHERE conversation_id=?",
            (conversation_id,),
        ).fetchall()
        external = sum(
            row["actual_external"]
            if row["status"] == "SETTLED" and row["actual_external"] is not None
            else row["requested_external"]
            for row in rows
        )
        tokens = sum(
            row["actual_tokens"]
            if row["status"] == "SETTLED" and row["actual_tokens"] is not None
            else row["requested_tokens"]
            for row in rows
        )
        tool_time = connection.execute(
            "SELECT COALESCE(SUM(active_seconds),0) FROM credra_entry_tool_results WHERE conversation_id=?",
            (conversation_id,),
        ).fetchone()[0]
        return {
            "scope": "CONVERSATION",
            "external_spent": external,
            "external_limit": policy.external_request_limit,
            "token_spent": tokens,
            "token_limit": policy.token_limit,
            "active_seconds": sum(row["active_seconds"] for row in rows) + tool_time,
            "active_seconds_limit": policy.active_seconds_limit,
            "usage_uncertain": any(
                row["status"] in {"DISPATCHED", "UNCERTAIN"}
                or row["status"] == "SETTLED"
                and (row["actual_external"] is None or row["actual_tokens"] is None)
                for row in rows
            ),
        }

    def snapshot(self, conversation_id, policy):
        with self.store.connect() as connection:
            return self._snapshot(connection, conversation_id, policy)

    def operation(self, conversation_id, operation_id):
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM credra_entry_operations WHERE conversation_id=? AND operation_id=?",
                (conversation_id, operation_id),
            ).fetchone()
        return dict(row) if row else None

    def reserve(self, conversation_id, operation_id, payload, policy):
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT 1 FROM credra_entry_operations WHERE conversation_id=? AND operation_id=?",
                (conversation_id, operation_id),
            ).fetchone()
            if existing:
                return
            uncertain = connection.execute(
                "SELECT 1 FROM credra_entry_operations WHERE conversation_id=? AND status IN ('DISPATCHED','UNCERTAIN') LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if uncertain:
                raise EntryRequestUncertain("ENTRY_MODEL_RESULT_UNCERTAIN")
            spent = self._snapshot(connection, conversation_id, policy)
            if (
                spent["external_spent"] + policy.model_attempt_reservation
                > policy.external_request_limit
                or spent["token_spent"] + policy.model_token_reservation
                > policy.token_limit
                or spent["active_seconds"] >= policy.active_seconds_limit
            ):
                raise EntryBudgetLimited("ENTRY_CONVERSATION_BUDGET_EXHAUSTED")
            connection.execute(
                "INSERT INTO credra_entry_operations VALUES (?,?, 'RESERVED',?,?,?,NULL,NULL,NULL,0)",
                (
                    conversation_id,
                    operation_id,
                    policy.model_attempt_reservation,
                    policy.model_token_reservation,
                    json.dumps(payload),
                ),
            )

    def dispatch(self, conversation_id, operation_id):
        with self.store.connect() as connection:
            changed = connection.execute(
                "UPDATE credra_entry_operations SET status='DISPATCHED' WHERE conversation_id=? AND operation_id=? AND status='RESERVED'",
                (conversation_id, operation_id),
            ).rowcount
            if changed != 1:
                raise EntryRequestUncertain("ENTRY_OPERATION_NOT_RESERVED")

    def uncertain(self, conversation_id, operation_id):
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE credra_entry_operations SET status='UNCERTAIN' WHERE conversation_id=? AND operation_id=? AND result_json IS NULL",
                (conversation_id, operation_id),
            )

    def save_result(self, conversation_id, operation_id, result):
        with self.store.connect() as connection:
            changed = connection.execute(
                "UPDATE credra_entry_operations SET result_json=? WHERE conversation_id=? AND operation_id=? AND status='DISPATCHED' AND result_json IS NULL",
                (json.dumps(result), conversation_id, operation_id),
            ).rowcount
            if changed != 1:
                raise EntryRequestUncertain(
                    "ENTRY_RESULT_ALREADY_STORED_OR_NOT_DISPATCHED"
                )

    def settle(self, conversation_id, operation_id):
        row = self.operation(conversation_id, operation_id)
        if row is None or row["result_json"] is None:
            raise EntryRequestUncertain("ENTRY_RESULT_NOT_STORED")
        result = json.loads(row["result_json"])

        # Malformed/unknown usage never refunds a reservation. Stored model results
        # stay immutable; only the trusted accounting projection is normalized.
        def count(value):
            return value if type(value) is int and value >= 0 else None

        seconds = result["active_seconds"]
        if (
            type(seconds) not in (int, float)
            or not math.isfinite(seconds)
            or seconds < 0
        ):
            raise EntryRequestUncertain("ENTRY_DURATION_INVALID")
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE credra_entry_operations SET status='SETTLED',actual_external=?,actual_tokens=?,active_seconds=? WHERE conversation_id=? AND operation_id=?",
                (
                    count(result["external"]),
                    count(result["tokens"]),
                    seconds,
                    conversation_id,
                    operation_id,
                ),
            )
        return result

    def tool(self, conversation_id, operation_id):
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT * FROM credra_entry_tool_results WHERE conversation_id=? AND operation_id=?",
                (conversation_id, operation_id),
            ).fetchone()
        return dict(row) if row else None

    def claim_tool(self, conversation_id, operation_id, message_id, policy):
        with self.store.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT 1 FROM credra_entry_tool_results WHERE conversation_id=? AND operation_id=?",
                (conversation_id, operation_id),
            ).fetchone()
            if row:
                return
            if (
                self._snapshot(connection, conversation_id, policy)["active_seconds"]
                >= policy.active_seconds_limit
            ):
                raise EntryBudgetLimited("ENTRY_CONVERSATION_TIME_EXHAUSTED")
            count = connection.execute(
                "SELECT COUNT(*) FROM credra_entry_tool_results WHERE conversation_id=? AND message_id=?",
                (conversation_id, message_id),
            ).fetchone()[0]
            if count >= policy.max_tool_calls:
                raise EntryBudgetLimited("ENTRY_TOOL_ROUND_LIMIT")
            connection.execute(
                "INSERT INTO credra_entry_tool_results VALUES (?,?,?,'RESERVED',NULL,0)",
                (conversation_id, operation_id, message_id),
            )

    def dispatch_tool(self, conversation_id, operation_id):
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE credra_entry_tool_results SET status='DISPATCHED' WHERE conversation_id=? AND operation_id=?",
                (conversation_id, operation_id),
            )

    def save_tool(self, conversation_id, operation_id, result, seconds):
        with self.store.connect() as connection:
            connection.execute(
                "UPDATE credra_entry_tool_results SET status='ACKED',result_json=?,active_seconds=? WHERE conversation_id=? AND operation_id=?",
                (json.dumps(result), seconds, conversation_id, operation_id),
            )
