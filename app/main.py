"""Non-durable offline preview for the Financial -> Risk -> Report flow."""

import argparse
from pathlib import Path

from app.service import run_case
from credra_agent.observability.runtime import entrypoint


@entrypoint("main")
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    _, risk, report = run_case(args.case_dir)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report)
    print(f"risk_level={risk.risk_level.value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
