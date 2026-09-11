"""CLI for durable Credra Agent tasks."""

import argparse
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from app.config import Settings
from app.llm.gateway import StructuredModelError
from app.runtime.tasks import get_task_status, resume_task, start_task
from credra_agent.intent.service import build_intent_model, interpret_message
from credra_agent.observability.runtime import entrypoint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--data-dir", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start")
    start.add_argument("--thread-id", required=True)
    start.add_argument("--case-id", required=True)

    status = subparsers.add_parser("status")
    status.add_argument("--thread-id", required=True)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--thread-id", required=True)
    resume.add_argument(
        "--decision", choices=("approve", "research", "resume_logging"), required=True
    )
    resume.add_argument("--comment")

    parse = subparsers.add_parser("parse")
    parse.add_argument("--thread-id", required=True)
    parse.add_argument("--message-id", required=True)
    parse.add_argument("--text", required=True)
    parse.add_argument("--as-of", type=date.fromisoformat)
    return parser


@entrypoint("task_cli")
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict[str, object] = {}
    if args.db:
        overrides["checkpoint_db_path"] = args.db
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    settings = Settings(**overrides)

    try:
        if args.command == "start":
            payload = start_task(
                thread_id=args.thread_id,
                case_id=args.case_id,
                settings=settings,
            )
        elif args.command == "status":
            payload = get_task_status(thread_id=args.thread_id, settings=settings)
        elif args.command == "resume":
            payload = resume_task(
                thread_id=args.thread_id,
                decision=args.decision,
                comment=args.comment,
                settings=settings,
            )
        else:
            payload = interpret_message(
                thread_id=args.thread_id,
                source_message_id=args.message_id,
                text=args.text,
                as_of=args.as_of or datetime.now(UTC).date(),
                data_dir=settings.data_dir,
                database_path=settings.checkpoint_db_path,
                model=build_intent_model(settings),
            ).model_dump(mode="json")
    except (StructuredModelError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
