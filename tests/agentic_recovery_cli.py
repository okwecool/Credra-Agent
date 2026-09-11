"""Offline subprocess harness for P14 crash-window acceptance tests."""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from app.config import Settings
from app.llm.gateway import StructuredModelResult
from app.runtime.tasks import resume_agentic_task, start_agentic_task
from credra_agent.execution.executor import (
    ActionExecutor,
    ExecutionOutcome,
    HandlerDefinition,
)
from credra_agent.intent.models import Period, Question, SourcePolicy, TaskSpec
from credra_agent.planning.models import (
    CoordinatorLimits,
    DecisionDraft,
    RunAuthorization,
)


class StateAwareModel:
    model_name = "p14-offline-subprocess"

    def generate(self, **kwargs):
        observations = kwargs["payload"]["observation_index"]
        if observations:
            output = DecisionDraft(
                decision="FINISH",
                finish_reason="ANSWERED",
                reason_summary="跨进程恢复后，必答问题已经覆盖。",
            )
        else:
            output = DecisionDraft(
                decision="ACTION",
                tool="search_evidence",
                arguments={
                    "query": "比亚迪 新增监管消息",
                    "subject_id": "002594.SZ",
                    "subject_name": "比亚迪",
                    "period": {"start": "2024-01-01", "end": "2025-12-31"},
                    "source_policy": {
                        "preferred": ["exchange"],
                        "allowed": ["exchange", "regulator"],
                        "denied": ["social_media"],
                    },
                    "category": "regulatory",
                },
                hypothesis_ids=["hyp-q-regulatory"],
                expected_observation="取得监管来源材料",
                reason_summary="先核验监管渠道。",
            )
        return StructuredModelResult(
            output=output,
            model_name=self.model_name,
            attempts=1,
            latency_ms=1,
            input_tokens=8,
            output_tokens=12,
            accounting_complete=True,
        )


def build_task() -> TaskSpec:
    return TaskSpec(
        version=1,
        source_message_id="p14-subprocess-message",
        operation="start",
        subject_id="002594.SZ",
        subject_name="比亚迪",
        case_id="case_byd_002594",
        as_of=date(2025, 12, 31),
        periods=[Period(start=date(2024, 1, 1), end=date(2025, 12, 31))],
        source_policy=SourcePolicy(
            preferred=["exchange"],
            allowed=["exchange", "regulator"],
            denied=["social_media"],
        ),
        questions=[
            Question(
                question_id="q-regulatory",
                text="调查近期新增监管和负面消息",
                completion_criteria="形成带来源的结论或明确缺口",
                focus="regulatory",
            )
        ],
        readiness="READY",
    )


def build_authorization() -> RunAuthorization:
    return RunAuthorization(
        authorization_id="p14-subprocess-authorization",
        authorized_by="OFFLINE_TEST",
        task_spec_version=1,
        approval="APPROVED",
        external_request_limit=20,
        token_limit=1000,
        active_seconds_limit=60,
        limits=CoordinatorLimits(
            max_decisions=4,
            no_progress_limit=2,
            model_attempt_reservation=2,
            decision_token_reservation=100,
            decision_max_output_tokens=50,
        ),
    )


def build_executor(counter: Path) -> ActionExecutor:
    def search(arguments):
        counter.parent.mkdir(parents=True, exist_ok=True)
        with counter.open("a", encoding="utf-8") as stream:
            stream.write(arguments.query + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return ExecutionOutcome(
            status="SUCCESS",
            summary="跨进程工具取得一条监管材料。",
            payload={"evidence": [{"id": "evidence-cross-process"}]},
            novelty_keys=["evidence-cross-process"],
            answered_question_ids=["q-regulatory"],
            actual_external_requests=1,
        )

    return ActionExecutor(
        {"search_evidence": HandlerDefinition(search, external_request_reservation=1)}
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["start", "resume"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--thread-id", required=True)
    parser.add_argument(
        "--crash-at",
        choices=["after_action_reserved", "after_request", "after_result_stored"],
    )
    args = parser.parse_args()
    source = args.root / "data" / "case_byd_002594" / "source"
    source.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        data_dir=args.root / "data",
        checkpoint_db_path=args.root / "checkpoints" / "tasks.db",
        trace_dir=args.root / "traces",
        service_log_dir=args.root / "logs",
    )
    model = StateAwareModel()
    executor = build_executor(args.root / "external_calls.txt")

    def crash(boundary, action):
        if boundary == args.crash_at:
            os._exit(91)

    if args.command == "start":
        result = start_agentic_task(
            thread_id=args.thread_id,
            task_spec=build_task(),
            authorization=build_authorization(),
            settings=settings,
            model=model,
            executor=executor,
            fault_hook=crash if args.crash_at else None,
        )
    else:
        result = resume_agentic_task(
            thread_id=args.thread_id,
            settings=settings,
            model=model,
            executor=executor,
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
