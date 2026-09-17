"""Offline P25 process-crash harness; never constructs a live model/provider."""

import argparse
import json
import os
from datetime import date
from pathlib import Path

from app.config import Settings
from app.llm.gateway import StructuredModelResult
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.intent.models import IntentDraft
from credra_agent.intent.parser import parse_draft
from credra_agent.planning.models import DecisionDraft
from credra_agent.runtime.ui_service import execute_ui_message
from credra_agent.runtime.ui_store import UITaskStore


class OfflineModel:
    model_name = "offline-p25-process"

    def __init__(self, directory, boundary=""):
        self.directory = directory
        self.boundary = boundary

    def generate(self, **kwargs):
        with (self.directory / "calls.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"type": "model", "purpose": kwargs["purpose"]}) + "\n"
            )
        if self.boundary == "intent_dispatched":
            os._exit(73)
        if kwargs["output_schema"] is IntentDraft:
            output = parse_draft(
                kwargs["payload"]["message"], catalog=[], anchor_date=date(2026, 6, 30)
            )[0]
        elif kwargs["payload"]["observation_index"]:
            output = DecisionDraft(
                decision="FINISH",
                finish_reason="NEEDS_REVIEW",
                reason_summary="离线缺口如实披露",
                review_required=True,
            )
        else:
            task = kwargs["payload"]["task_spec"]
            output = DecisionDraft(
                decision="ACTION",
                tool="search_evidence",
                arguments={
                    "query": "比亚迪 原始公告",
                    "subject_id": task["subject_id"],
                    "subject_name": task["subject_name"],
                    "period": task["periods"][0],
                    "source_policy": task["source_policy"],
                    "category": "regulatory",
                },
                expected_observation="核对原始资料",
                reason_summary="离线调查测试",
            )
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            external_requests=1,
            latency_ms=1,
            input_tokens=10,
            output_tokens=20,
            accounting_complete=True,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("command", choices=("start", "resume", "replay"))
    parser.add_argument("--boundary", default="")
    args = parser.parse_args()
    settings = Settings(
        _env_file=None,
        data_dir=args.directory / "data",
        checkpoint_db_path=args.directory / "tasks.db",
        trace_dir=args.directory / "traces",
        service_log_dir=args.directory / "logs",
        agent_ui_execution_enabled=True,
        agent_ui_policy_path=args.directory / "policy.json",
        intent_mode="llm",
        analysis_mode="llm",
        model_name="offline-placeholder",
        model_api_key="test-placeholder",
        research_provider="mock",
        content_fetch_provider="disabled",
    )
    model = OfflineModel(args.directory, args.boundary)

    def executor_factory(settings, spec, model):
        def search(arguments):
            with (args.directory / "calls.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"type": "tool", "query": arguments.query}) + "\n"
                )
            return ExecutionOutcome(
                status="NO_RESULT", summary="离线来源缺口", actual_external_requests=1
            )

        return ActionExecutor(
            {
                "search_evidence": HandlerDefinition(
                    search, external_request_reservation=1
                )
            }
        )

    if args.boundary in {
        "after_action_reserved",
        "after_request",
        "after_result_stored",
        "before_graph_start",
    }:
        from credra_agent.runtime import service

        original = service.start_agentic_task

        def start(**kwargs):
            if args.boundary == "before_graph_start":
                os._exit(73)

            def fault(boundary, action):
                if boundary == args.boundary:
                    os._exit(73)

            return original(**kwargs, fault_hook=fault)

        service.start_agentic_task = start
    elif args.boundary == "intent_result_stored":
        original = UITaskStore.save_model_result

        def store_result(self, *arguments):
            original(self, *arguments)
            os._exit(73)

        UITaskStore.save_model_result = store_result

    result = execute_ui_message(
        thread_id="ui-process-task",
        message_id="resume" if args.command == "resume" else "start",
        text="resume" if args.command == "resume" else "调查比亚迪2025年的监管消息",
        as_of=date(2026, 6, 30),
        settings=settings,
        models_factory=lambda s, p: (model, model),
        executor_factory=executor_factory,
    )
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=True))


if __name__ == "__main__":
    main()
