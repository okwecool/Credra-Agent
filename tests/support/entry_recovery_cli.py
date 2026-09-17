"""P26 independent-process fault harness; models/providers are always offline."""

import argparse
import json
import os
from datetime import date
from pathlib import Path

from app.config import Settings
from app.llm.gateway import StructuredModelResult
from credra_agent.entry.budget import EntryBudgetStore
from credra_agent.entry.context import workspace_reference
from credra_agent.entry.delegation import InvestigationDelegation, wait_for_command
from credra_agent.entry.models import EntryDecision
from credra_agent.entry.query import TaskQueryService
from credra_agent.entry.service import execute_entry_message
from credra_agent.entry.store import EntryStore
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.intent.store import IntentStore
from credra_agent.observability.runtime import config_from_settings, service_session
from credra_agent.runtime.ui_store import task_lock
from tests.support.ui_execution_cli import OfflineModel

CONVERSATION = "conversation-p26-process"
TEXT = "调查比亚迪2025年的监管消息，仅限交易所公告"


def append_call(directory, kind):
    with (directory / "entry-calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": kind}) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class EntryModel:
    model_name = "offline-entry-process"

    def __init__(self, directory, command, boundary=""):
        self.directory, self.command, self.boundary = directory, command, boundary

    def generate(self, **kwargs):
        context = kwargs["payload"]
        assert context["query"] and context["tools"] and context["permissions"]
        append_call(self.directory, "entry")
        if self.boundary == "model_returned":
            os._exit(73)
        if self.command == "resume":
            selected = context["current_task"]
            output = {
                "decision": "call_tool",
                "tool": "resume_investigation",
                "arguments": {
                    "task_id": selected["summary"]["task_id"],
                    "expected_state_ref": selected["summary"]["state_ref"],
                },
                "reason": "明确恢复原调查",
            }
        elif self.command == "hello":
            output = {
                "decision": "reply",
                "content_kind": "conversation",
                "text": "你好，有什么可以帮你？",
            }
        else:
            output = {
                "decision": "call_tool",
                "tool": "prepare_investigation",
                "arguments": {
                    "draft": {
                        "operation": "start",
                        "subject_hint": "比亚迪",
                        "years": [2025],
                        "focus": ["regulatory"],
                        "allowed_sources": ["exchange_disclosure"],
                    }
                },
                "reason": "明确调查原始公告",
            }
        return StructuredModelResult(
            output=EntryDecision.model_validate(output),
            model_name=self.model_name,
            attempts=1,
            external_requests=1,
            latency_ms=1,
            input_tokens=10,
            output_tokens=20,
            accounting_complete=True,
        )


def settings_for(directory):
    return Settings(
        _env_file=None,
        data_dir=directory / "data",
        checkpoint_db_path=directory / "tasks.db",
        trace_dir=directory / "traces",
        service_log_dir=directory / "logs",
        agent_ui_execution_enabled=True,
        agent_entry_policy_path=directory / "entry.json",
        agent_ui_policy_path=directory / "task.json",
        analysis_mode="llm",
        intent_mode="llm",
        model_name="offline-placeholder",
        model_api_key="test-placeholder",
        research_provider="mock",
        content_fetch_provider="disabled",
        fact_verifier="rules",
        analysis_llm_max_input_chars=50000,
    )


def executor_factory(settings, spec, model):
    directory = settings.checkpoint_db_path.parent

    def search(arguments):
        append_call(directory, "tool")
        return ExecutionOutcome(
            status="NO_RESULT", summary="离线原始来源缺口", actual_external_requests=1
        )

    return ActionExecutor(
        {"search_evidence": HandlerDefinition(search, external_request_reservation=1)}
    )


def install_fault(boundary):
    def after_method(cls, name, predicate=None):
        original = getattr(cls, name)

        def wrapper(self, *args, **kwargs):
            returned = original(self, *args, **kwargs)
            if predicate is None or predicate(args, kwargs):
                os._exit(73)
            return returned

        setattr(cls, name, wrapper)

    if boundary == "context_saved":
        after_method(EntryStore, "snapshot")
    elif boundary == "model_saved":
        after_method(EntryBudgetStore, "save_result")
    elif boundary == "command_reserved":
        after_method(EntryStore, "reserve_command")
    elif boundary == "intent_saved":
        after_method(IntentStore, "save")
    elif boundary == "command_queued":
        after_method(EntryStore, "enqueue_command")
    elif boundary == "command_dispatched":
        after_method(EntryStore, "set_command", lambda a, k: a[1] == "DISPATCHED")
    elif boundary == "receipt_saved":
        after_method(EntryBudgetStore, "save_response")
    elif boundary in {"graph_finished", "after_request", "after_result_stored"}:
        from credra_agent.runtime import service

        original = service.start_agentic_task

        def start(**kwargs):
            if boundary != "graph_finished":

                def fault(point, action):
                    if point == boundary:
                        os._exit(73)

                kwargs["fault_hook"] = fault
            result = original(**kwargs)
            if boundary == "graph_finished":
                os._exit(73)
            return result

        service.start_agentic_task = start


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument(
        "command", choices=("start", "replay", "recover", "resume", "hello", "hold")
    )
    parser.add_argument("--boundary", default="")
    args = parser.parse_args()
    settings = settings_for(args.directory)
    store = EntryStore(settings.checkpoint_db_path)
    if args.command == "hold":
        import time

        with task_lock(settings.checkpoint_db_path, CONVERSATION):
            (args.directory / "lock-ready").write_text("ready", encoding="utf-8")
            while not (args.directory / "release-lock").exists():
                time.sleep(0.05)
        return
    install_fault(args.boundary)
    query = TaskQueryService(settings, allowed_subject_ids=["002594", "600104"])
    model = OfflineModel(args.directory)
    delegation = InvestigationDelegation(
        settings=settings,
        query=query,
        model_factory=lambda s, p: model,
        executor_factory=executor_factory,
    )
    with service_session(
        "p26_process", config_from_settings(settings), inherited=False
    ):
        delegation.recover()
        if args.command == "recover":
            result = None
        else:
            result = execute_entry_message(
                conversation_id=CONVERSATION,
                message_id="resume"
                if args.command == "resume"
                else "hello"
                if args.command == "hello"
                else "start",
                text="继续原调查"
                if args.command == "resume"
                else "你好"
                if args.command == "hello"
                else TEXT,
                as_of=date(2026, 6, 30),
                settings=settings,
                model_factory=lambda s, p: EntryModel(
                    args.directory, args.command, args.boundary
                ),
                investigation_model_factory=lambda s, p: model,
                executor_factory=executor_factory,
            )
        with store.connect() as connection:
            commands = [
                dict(row)
                for row in connection.execute(
                    "SELECT command_id FROM credra_entry_commands"
                )
            ]
        for row in commands:
            wait_for_command(settings.checkpoint_db_path, row["command_id"])
        tasks = query.list().model_dump(mode="json")
        selected = store.conversation(CONVERSATION, workspace_reference(settings))[
            "selected_task_id"
        ]
        print(
            json.dumps(
                {
                    "result": result.model_dump(mode="json") if result else None,
                    "selected": selected,
                    "tasks": tasks,
                },
                ensure_ascii=True,
            )
        )


if __name__ == "__main__":
    main()
