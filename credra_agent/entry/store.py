"""Local conversation snapshots and rebuildable task projection, with no LLM IO."""

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from credra_agent.entry.models import EntryAskUser, EntryContext, TaskSummary


class EntryConflict(ValueError):
    pass


def now():
    return datetime.now(UTC)


class EntryStore:
    def __init__(self, database_path: Path):
        self.path = database_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS credra_entry_conversations (
                    conversation_id TEXT PRIMARY KEY, workspace_ref TEXT NOT NULL,
                    selected_task_id TEXT, pending_question_json TEXT);
                CREATE TABLE IF NOT EXISTS credra_entry_messages (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL, message_id TEXT NOT NULL UNIQUE,
                    turn_id TEXT NOT NULL, role TEXT NOT NULL, payload_json TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_entry_messages
                    ON credra_entry_messages(conversation_id, seq);
                CREATE TABLE IF NOT EXISTS credra_entry_contexts (
                    conversation_id TEXT NOT NULL, message_id TEXT NOT NULL,
                    context_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(conversation_id, message_id, context_id));
                CREATE TABLE IF NOT EXISTS credra_entry_task_index (
                    task_id TEXT PRIMARY KEY, checkpoint_id TEXT,
                    summary_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS credra_entry_query_meta (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), payload_json TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def ensure_conversation(self, conversation_id, workspace_ref):
        if not conversation_id.startswith("conversation-") or not workspace_ref:
            raise EntryConflict("ENTRY_CONVERSATION_SCOPE_INVALID")
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO credra_entry_conversations VALUES (?, ?, NULL, NULL)",
                (conversation_id, workspace_ref),
            )
        self.conversation(conversation_id, workspace_ref)

    def conversation(self, conversation_id, workspace_ref):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM credra_entry_conversations WHERE conversation_id=?",
                (conversation_id,),
            ).fetchone()
        if row is None or row["workspace_ref"] != workspace_ref:
            raise EntryConflict("ENTRY_CONVERSATION_NOT_VISIBLE")
        return {
            "selected_task_id": row["selected_task_id"],
            "pending_question": EntryAskUser.model_validate_json(
                row["pending_question_json"]
            )
            if row["pending_question_json"]
            else None,
        }

    def select(self, conversation_id, workspace_ref, task_id):
        self.conversation(conversation_id, workspace_ref)
        with self.connect() as connection:
            connection.execute(
                "UPDATE credra_entry_conversations SET selected_task_id=? WHERE conversation_id=?",
                (task_id, conversation_id),
            )

    def pending(self, conversation_id, workspace_ref, question):
        self.conversation(conversation_id, workspace_ref)
        with self.connect() as connection:
            connection.execute(
                "UPDATE credra_entry_conversations SET pending_question_json=? WHERE conversation_id=?",
                (question.model_dump_json() if question else None, conversation_id),
            )

    def append(
        self, *, conversation_id, workspace_ref, message_id, turn_id, role, payload
    ):
        self.conversation(conversation_id, workspace_ref)
        if role not in {"user", "assistant", "tool"}:
            raise EntryConflict("ENTRY_MESSAGE_ROLE_INVALID")
        value = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(value) > 100_000:
            raise EntryConflict("ENTRY_MESSAGE_TOO_LARGE")
        fingerprint = hashlib.sha256(value.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM credra_entry_messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if row:
                if (
                    row["conversation_id"],
                    row["turn_id"],
                    row["role"],
                    row["fingerprint"],
                ) != (conversation_id, turn_id, role, fingerprint):
                    raise EntryConflict("ENTRY_MESSAGE_ID_CONFLICT")
                return False
            connection.execute(
                "INSERT INTO credra_entry_messages (conversation_id,message_id,turn_id,role,payload_json,fingerprint,created_at) VALUES (?,?,?,?,?,?,?)",
                (
                    conversation_id,
                    message_id,
                    turn_id,
                    role,
                    value,
                    fingerprint,
                    now().isoformat(),
                ),
            )
        return True

    def history(
        self, conversation_id, workspace_ref, *, max_messages=12, exclude_turn=None
    ):
        self.conversation(conversation_id, workspace_ref)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT turn_id,role,payload_json FROM credra_entry_messages WHERE conversation_id=? AND (? IS NULL OR turn_id != ?) ORDER BY seq DESC LIMIT ?",
                (conversation_id, exclude_turn, exclude_turn, max_messages + 1),
            ).fetchall()
        limited = len(rows) > max_messages
        selected = rows[:max_messages]
        # Never retain only the response half of a tool pair/turn.
        if limited and selected and rows[-1]["turn_id"] == selected[-1]["turn_id"]:
            selected = [
                row for row in selected if row["turn_id"] != rows[-1]["turn_id"]
            ]
        return [
            {
                "turn_id": row["turn_id"],
                "role": row["role"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in reversed(selected)
        ], limited

    def snapshot(self, context: EntryContext):
        context_id = "context-" + uuid4().hex
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO credra_entry_contexts VALUES (?,?,?,?)",
                (
                    context.conversation_id,
                    context.message_id,
                    context_id,
                    context.model_dump_json(),
                ),
            )
        return context_id

    def index(self, summary: TaskSummary, checkpoint_id=None):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT checkpoint_id FROM credra_entry_task_index WHERE task_id=?",
                (summary.task_id,),
            ).fetchone()
            if (
                existing
                and existing[0]
                and (checkpoint_id is None or existing[0] > checkpoint_id)
            ):
                return
            connection.execute(
                "INSERT OR REPLACE INTO credra_entry_task_index VALUES (?,?,?)",
                (summary.task_id, checkpoint_id, summary.model_dump_json()),
            )

    def indexed(
        self,
        *,
        allowed_subject_ids,
        hidden_cases,
        subject_id=None,
        status=None,
        year=None,
        after=None,
        limit=21,
    ):
        with self.connect() as connection:
            predicates = [
                "(json_extract(summary_json,'$.subject_id') IS NULL OR json_extract(summary_json,'$.subject_id') IN (SELECT value FROM json_each(?)))",
                "COALESCE(json_extract(summary_json,'$.case_id'),'') NOT IN (SELECT value FROM json_each(?))",
            ]
            arguments = [
                json.dumps(sorted(allowed_subject_ids)),
                json.dumps(sorted(hidden_cases)),
            ]
            for field, value in (("subject_id", subject_id), ("status", status)):
                if value is not None:
                    predicates.append(f"json_extract(summary_json,'$.{field}')=?")
                    arguments.append(value)
            if year is not None:
                predicates.append(
                    "? IN (SELECT value FROM json_each(json_extract(summary_json,'$.years')))"
                )
                arguments.append(year)
            if after is not None:
                predicates.append(
                    "(COALESCE(json_extract(summary_json,'$.checkpoint_at'),''),task_id)<(?,?)"
                )
                arguments.extend(after)
            rows = connection.execute(
                "SELECT summary_json FROM credra_entry_task_index WHERE "
                + " AND ".join(predicates)
                + " ORDER BY COALESCE(json_extract(summary_json,'$.checkpoint_at'),'') DESC,task_id DESC LIMIT ?",
                [*arguments, limit],
            ).fetchall()
        return [TaskSummary.model_validate_json(row[0]) for row in rows]

    def reset_projection(self):
        with self.connect() as connection:
            connection.execute("DELETE FROM credra_entry_task_index")
            connection.execute("DELETE FROM credra_entry_query_meta")

    def query_meta(self, value=None):
        with self.connect() as connection:
            if value is not None:
                connection.execute(
                    "INSERT OR REPLACE INTO credra_entry_query_meta VALUES (1,?)",
                    (json.dumps(value),),
                )
            row = connection.execute(
                "SELECT payload_json FROM credra_entry_query_meta WHERE singleton=1"
            ).fetchone()
        return (
            json.loads(row[0])
            if row
            else {"cursor": None, "complete": False, "watermark": None}
        )
