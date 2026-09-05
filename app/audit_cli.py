"""CLI for exporting a completed Credra Agent task as an audit ZIP."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from app.audit import export_audit_bundle
from app.config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--trace-dir", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export")
    export.add_argument("--thread-id", required=True)
    export.add_argument("--output-dir", type=Path, default=Path("audit-exports"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict[str, object] = {}
    if args.db:
        overrides["checkpoint_db_path"] = args.db
    if args.data_dir:
        overrides["data_dir"] = args.data_dir
    if args.trace_dir:
        overrides["trace_dir"] = args.trace_dir
    output_dir = args.output_dir
    if not output_dir.is_absolute():
        output_dir = PROJECT_ROOT / output_dir
    output_dir = output_dir.resolve()
    if not output_dir.is_relative_to(PROJECT_ROOT):
        print(json.dumps({"error": "output directory must be inside the project"}))
        return 2
    try:
        result = export_audit_bundle(
            thread_id=args.thread_id,
            settings=Settings(**overrides),
            output_dir=output_dir,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    payload = result.model_dump(mode="json")
    payload["archive_path"] = (
        Path(result.archive_path).relative_to(PROJECT_ROOT).as_posix()
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
