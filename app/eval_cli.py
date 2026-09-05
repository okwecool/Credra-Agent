"""CLI for isolated, offline Credra Agent business evaluations."""

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from app.evals.runner import run_eval_suite

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument(
        "--suite",
        type=Path,
        default=Path("evals/suites/byd_baseline_v1.json"),
    )
    run.add_argument("--output-dir", type=Path, default=Path("eval-results"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    suite_path = args.suite
    if not suite_path.is_absolute():
        suite_path = PROJECT_ROOT / suite_path
    try:
        result, result_path = run_eval_suite(
            suite_path,
            project_root=PROJECT_ROOT,
            output_dir=args.output_dir,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(
        json.dumps(
            {
                "suite_id": result.suite_id,
                "status": result.status,
                "passed_checks": result.passed_check_count,
                "total_checks": result.check_count,
                "external_call_count": result.external_call_count,
                "result": result_path.relative_to(PROJECT_ROOT).as_posix(),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
