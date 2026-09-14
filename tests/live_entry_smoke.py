"""Explicitly approved P26 semantic smoke: live entry, offline investigation only."""

import argparse
import json
import re
import shutil
from datetime import date
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from app.config import Settings
from credra_agent.entry.budget import EntryBudgetStore
from credra_agent.entry.delegation import wait_for_command
from credra_agent.entry.query import TaskQueryService
from credra_agent.entry.service import execute_entry_message
from credra_agent.entry.store import EntryStore
from credra_agent.observability.runtime import config_from_settings, service_session
from credra_agent.runtime.ui_store import task_lock
from tests.entry_recovery_cli import executor_factory
from tests.test_v2_entry_delegation import CONTROLS
from tests.test_v2_ui_execution import policy_dict
from tests.ui_execution_cli import OfflineModel

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = [
    ("empty_query", "现有任务有哪些"),
    ("ordinary_chat", "你好，给我讲一个简短的笑话"),
    ("capabilities", "你可以做哪些事情，哪些还不能做？"),
    ("unknown_subject", "调查腾讯2025年的监管消息"),
    ("byd_create", "调查比亚迪2025年的监管消息，仅限交易所公告，禁止使用社交媒体"),
    ("refresh_tasks", "之前做过哪些调查？请刷新记录再回答"),
    ("saic_create", "新增上汽集团2025年的监管调查，只使用交易所公告"),
    ("byd_second", "新建另一项比亚迪2024年监管调查，只使用交易所公告"),
    ("ambiguous_resume", "继续比亚迪的调查"),
    ("second_candidate", "第二个"),
    ("pronoun_resume", "继续它"),
    (
        "source_negation",
        "不要恢复旧任务；另建比亚迪2025年监管调查，不要用社交媒体，仅限交易所公告",
    ),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--approved",
        action="store_true",
        help="Requires explicit human approval of this temporary development policy",
    )
    parser.add_argument(
        "--directory", type=Path, default=ROOT / ".test-tmp" / "p26-live-entry"
    )
    parser.add_argument(
        "--conversation", default="conversation-p26-live-" + uuid4().hex[:12]
    )
    parser.add_argument(
        "--approved-total-tokens",
        type=int,
        default=300000,
        help="Total ceiling explicitly approved by the user across all trials in this directory",
    )
    parser.add_argument(
        "--samples", nargs="+", choices=[name for name, text in SAMPLES]
    )
    args = parser.parse_args()
    if not args.approved:
        print("Human approval of the temporary development policy is required.")
        return 2
    directory = args.directory.resolve()
    if not directory.is_relative_to(ROOT / ".test-tmp"):
        raise ValueError("LIVE_TEST_DIRECTORY_OUTSIDE_WORKSPACE_TEMP")
    directory.mkdir(parents=True, exist_ok=True)
    if not re.fullmatch(r"conversation-p26-live-[a-zA-Z0-9-]{1,40}", args.conversation):
        raise ValueError("LIVE_TEST_CONVERSATION_INVALID")
    with task_lock(directory / "tasks.db", "p26-approved-development-global-budget"):
        return run_approved(
            directory, args.conversation, args.approved_total_tokens, args.samples
        )


def remaining_budget(directory, total_tokens=300000):
    store = EntryStore(directory / "tasks.db")
    EntryBudgetStore(store)
    with store.connect() as connection:
        rows = connection.execute("SELECT * FROM credra_entry_operations").fetchall()
        tokens = sum(
            row["actual_tokens"]
            if row["status"] == "SETTLED" and row["actual_tokens"] is not None
            else row["requested_tokens"]
            for row in rows
        )
        seconds = (
            sum(row["active_seconds"] for row in rows)
            + connection.execute(
                "SELECT COALESCE(SUM(active_seconds),0) FROM credra_entry_tool_results"
            ).fetchone()[0]
        )
    return {
        "token_limit": total_tokens - tokens,
        "active_seconds_limit": 1800 - seconds,
    }


def run_approved(directory, conversation, total_tokens=300000, samples=None):
    if total_tokens < 6000:
        raise ValueError("LIVE_TEST_APPROVED_CEILING_INVALID")
    remaining = remaining_budget(directory, total_tokens)
    if remaining["token_limit"] < 6000 or remaining["active_seconds_limit"] <= 0:
        print("Approved development budget exhausted; no live request dispatched.")
        return 3
    with EntryStore(directory / "tasks.db").connect() as connection:
        if connection.execute(
            "SELECT 1 FROM credra_entry_conversations WHERE conversation_id=?",
            (conversation,),
        ).fetchone():
            raise ValueError(
                "LIVE_TEST_REQUIRES_NEW_CONVERSATION_WITH_UNCHANGED_OLD_LEDGER"
            )
    print(json.dumps({"remaining_approved_budget": remaining}), flush=True)
    for case in ("case_byd_002594", "case_saic_600104"):
        shutil.copytree(
            ROOT / "data" / case / "source",
            directory / "data" / case / "source",
            dirs_exist_ok=True,
        )
    policy = {
        "policy_id": "p26-approved-development",
        "version": 1,
        "approval": "APPROVED",
        "allowed_subject_ids": ["002594", "600104"],
        "allowed_control_tools": CONTROLS,
        "external_request_limit": 300000,
        **remaining,
        "max_model_rounds": 4,
        "max_tool_calls": 3,
        "model_attempt_reservation": 2,
        "model_token_reservation": 6000,
        "max_output_tokens": 1200,
        "max_input_chars": 30000,
    }
    (directory / "entry.json").write_text(json.dumps(policy), encoding="utf-8")
    (directory / "task.json").write_text(json.dumps(policy_dict()), encoding="utf-8")
    baseline = Settings()
    settings = baseline.model_copy(
        update={
            "data_dir": directory / "data",
            "checkpoint_db_path": directory / "tasks.db",
            "trace_dir": directory / "traces",
            "service_log_dir": directory / "logs",
            "agent_ui_execution_enabled": True,
            "agent_entry_policy_path": directory / "entry.json",
            "agent_ui_policy_path": directory / "task.json",
            "analysis_mode": "llm",
            "research_provider": "mock",
            "content_fetch_provider": "disabled",
            "fact_verifier": "rules",
        }
    )
    model = OfflineModel(directory)
    evidence = []
    with service_session(
        "p26_live_entry", config_from_settings(settings), inherited=False
    ):
        for name, text in SAMPLES:
            if samples is not None and name not in samples:
                continue
            started = perf_counter()
            result = execute_entry_message(
                conversation_id=conversation,
                message_id=conversation + ":" + name,
                text=text,
                as_of=date(2026, 9, 13),
                settings=settings,
                investigation_model_factory=lambda s, p: model,
                executor_factory=executor_factory,
            )
            for tool in result.tools:
                command = tool.data.get("command")
                if command:
                    wait_for_command(
                        settings.checkpoint_db_path, command["command_id"], timeout=60
                    )
            item = {
                "sample": name,
                "query": text,
                "status": result.status,
                "text": result.text,
                "tools": [
                    {"tool": t.tool, "status": t.status, "limitations": t.limitations}
                    for t in result.tools
                ],
                "selected_task_id": result.selected_task_id,
                "limitations": result.limitations,
                "budget": result.budget,
                "elapsed_seconds": round(perf_counter() - started, 3),
            }
            evidence.append(item)
            (directory / ("semantic-results-" + conversation + ".json")).write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                json.dumps(
                    {
                        k: item[k]
                        for k in (
                            "sample",
                            "status",
                            "tools",
                            "budget",
                            "elapsed_seconds",
                        )
                    }
                ),
                flush=True,
            )
            if (
                result.status in {"CONFIGURATION_REQUIRED", "PAUSED_LOGGING"}
                or result.budget.get("usage_uncertain")
                or remaining_budget(directory, total_tokens)["token_limit"] < 6000
            ):
                break
        views = TaskQueryService(
            settings, allowed_subject_ids=["002594", "600104"]
        ).list()
        (directory / ("task-projection-" + conversation + ".json")).write_text(
            views.model_dump_json(indent=2), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
