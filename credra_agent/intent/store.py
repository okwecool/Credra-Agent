"""SQLite persistence for input messages and immutable TaskSpec snapshots."""

import json
import sqlite3
from pathlib import Path

from credra_agent.intent.models import IntentResult, TaskSpec


class IntentStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS credra_intent_messages (
            source_message_id TEXT PRIMARY KEY,
            thread_id TEXT NOT NULL,
            task_spec_version INTEGER,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_credra_intent_thread ON credra_intent_messages(thread_id, task_spec_version)"
        )
        return connection

    def find_message(self, message_id: str) -> IntentResult | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM credra_intent_messages WHERE source_message_id = ?",
                (message_id,),
            ).fetchone()
        return IntentResult.model_validate_json(row[0]) if row else None

    def latest_task_spec(self, thread_id: str) -> TaskSpec | None:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT result_json FROM credra_intent_messages
                WHERE thread_id = ? AND task_spec_version IS NOT NULL
                ORDER BY task_spec_version DESC LIMIT 1""",
                (thread_id,),
            ).fetchall()
        if not rows:
            return None
        return IntentResult.model_validate_json(rows[0][0]).task_spec

    def save(self, result: IntentResult) -> IntentResult:
        payload = result.model_dump_json()
        version = (
            result.task_spec.version
            if result.task_spec
            else result.bound_task_spec_version
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if result.task_spec:
                    latest = connection.execute(
                        """SELECT MAX(task_spec_version) FROM credra_intent_messages
                        WHERE thread_id = ?""",
                        (result.thread_id,),
                    ).fetchone()[0]
                    expected = 1 if latest is None else latest + 1
                    if result.task_spec.version != expected:
                        raise ValueError("TASK_SPEC_VERSION_CONFLICT")
                connection.execute(
                    """INSERT INTO credra_intent_messages
                    (source_message_id, thread_id, task_spec_version, result_json)
                    VALUES (?, ?, ?, ?)""",
                    (result.source_message_id, result.thread_id, version, payload),
                )
        except sqlite3.IntegrityError:
            existing = self.find_message(result.source_message_id)
            if existing is None:
                raise
            if existing.thread_id != result.thread_id:
                raise ValueError("SOURCE_MESSAGE_ID_CONFLICT")
            return existing.model_copy(update={"duplicate": True})
        return result

    def export_thread(self, thread_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT result_json FROM credra_intent_messages
                WHERE thread_id = ? ORDER BY created_at, rowid""",
                (thread_id,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]
