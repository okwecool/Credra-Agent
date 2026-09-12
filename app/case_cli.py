"""CLI for creating, validating, and running structured Credra Cases."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from app.cases import create_case_template, validate_case
from app.config import Settings
from app.runtime.tasks import start_task
from credra_agent.observability.runtime import entrypoint


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--data-dir", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser(
        "init", help="Create an editable structured Case template."
    )
    init.add_argument("--case-id", required=True)

    validate = subparsers.add_parser(
        "validate", help="Validate a Case without running it."
    )
    validate.add_argument("--case-id", required=True)

    run = subparsers.add_parser(
        "run", help="Validate a Case then start its durable task."
    )
    run.add_argument("--case-id", required=True)
    run.add_argument("--thread-id", required=True)
    return parser


@entrypoint("case_cli")
def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict[str, object] = {}
    if args.db:
        overrides["checkpoint_db_path"] = args.db
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    settings = Settings(**overrides)

    try:
        if args.command == "init":
            case_dir = create_case_template(args.case_id, settings.data_dir)
            payload = {"case_id": args.case_id, "source_dir": str(case_dir / "source")}
        else:
            result = validate_case(args.case_id, settings.data_dir)
            if args.command == "validate":
                print(
                    json.dumps(
                        result.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0 if result.valid else 2
            if not result.valid:
                print(
                    json.dumps(
                        {
                            "error": "case validation failed",
                            "validation": result.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 2
            payload = start_task(
                thread_id=args.thread_id,
                case_id=args.case_id,
                settings=settings,
            )
            payload["validation"] = result.model_dump(mode="json")
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
