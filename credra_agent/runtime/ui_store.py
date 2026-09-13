"""UI request replay and OS-owned task locks; SQLite remains the durable truth."""

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from credra_agent.runtime.ui_policy import UIPolicy


class UIRequestBlocked(RuntimeError):
    pass


@contextmanager
def task_lock(database_path: Path, thread_id: str):
    """Nonblocking process lock, automatically released on process exit."""
    directory = database_path.resolve().parent / "ui_task_locks"
    directory.mkdir(parents=True, exist_ok=True)
    filename = hashlib.sha256(thread_id.encode()).hexdigest() + ".lock"
    with (directory / filename).open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise UIRequestBlocked(
                "UI_TASK_BUSY: 当前任务正在执行，请稍后查看状态"
            ) from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class UITaskStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS credra_ui_policies (
                    thread_id TEXT PRIMARY KEY, policy_json TEXT NOT NULL,
                    authorization_json TEXT NOT NULL, profile_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS credra_ui_requests (
                    message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, response_json TEXT);
                CREATE TABLE IF NOT EXISTS credra_ui_model_results (
                    thread_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    PRIMARY KEY(thread_id, operation_id));
            """)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                yield connection
        finally:
            connection.close()

    def policy(self, thread_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT policy_json, authorization_json, profile_json FROM credra_ui_policies WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        if not row:
            return None
        from credra_agent.planning.models import RunAuthorization

        policy = UIPolicy.model_validate_json(row[0])
        authorization = RunAuthorization.model_validate_json(row[1])
        expected = policy.authorization(
            thread_id,
            authorization.task_spec_version,
            auth_id=authorization.authorization_id,
        )
        if authorization != expected or authorization.approval != "APPROVED":
            raise UIRequestBlocked("UI_STORED_AUTHORIZATION_MISMATCH")
        return policy, authorization, json.loads(row[2])

    def bind_policy(self, thread_id, policy, authorization, profile):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO credra_ui_policies VALUES (?, ?, ?, ?)",
                (
                    thread_id,
                    policy.model_dump_json(),
                    authorization.model_dump_json(),
                    json.dumps(profile),
                ),
            )

    def claim(self, thread_id, message_id, text):
        fingerprint = hashlib.sha256(text.encode()).hexdigest()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT thread_id, fingerprint, response_json FROM credra_ui_requests WHERE message_id=?",
                (message_id,),
            ).fetchone()
            if row:
                if row[0] != thread_id or row[1] != fingerprint:
                    raise UIRequestBlocked("SOURCE_MESSAGE_ID_CONFLICT")
                return json.loads(row[2]) if row[2] else None
            connection.execute(
                "INSERT INTO credra_ui_requests VALUES (?, ?, ?, NULL)",
                (message_id, thread_id, fingerprint),
            )
        return None

    def save_response(self, message_id, response):
        with self.connect() as connection:
            connection.execute(
                "UPDATE credra_ui_requests SET response_json=? WHERE message_id=?",
                (json.dumps(response, ensure_ascii=False), message_id),
            )

    def model_result(self, thread_id, operation_id):
        with self.connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM credra_ui_model_results WHERE thread_id=? AND operation_id=?",
                (thread_id, operation_id),
            ).fetchone()
        return json.loads(row[0]) if row else None

    def save_model_result(self, thread_id, operation_id, result):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO credra_ui_model_results VALUES (?, ?, ?)",
                (thread_id, operation_id, json.dumps(result, ensure_ascii=False)),
            )
