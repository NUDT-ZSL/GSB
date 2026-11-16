"""Command line entry for dataset generation and reasoning runs."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import ReasoningEngine
from .persistence import SnapshotError
from .sample import generate_dataset


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mm-pipeline",
        description="Deterministic local multimodal reasoning pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    gen = sub.add_parser("generate", help="generate deterministic dataset")
    gen.add_argument("--input-dir", required=True)
    gen.add_argument("--seed", type=int, default=20261117)

    run = sub.add_parser("run", help="run or resume reasoning")
    run.add_argument("--input-dir", required=True)
    run.add_argument("--snapshot-dir", required=True)
    run.add_argument("--report", required=True)
    run.add_argument("--exit-after-round", type=int, default=None,
                     help="simulate graceful exit after this round")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        result = generate_dataset(args.input_dir, args.seed)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    engine = ReasoningEngine(
        input_dir=args.input_dir,
        snapshot_dir=args.snapshot_dir,
        report_path=args.report,
        exit_after_round=args.exit_after_round,
    )
    try:
        result = engine.run()
    except SnapshotError as exc:
        message = str(exc)
        try:
            detail = json.loads(message)
        except json.JSONDecodeError:
            detail = {"code": "E_SNAPSHOT_INVALID", "message": message}
        print(json.dumps(detail, ensure_ascii=False, indent=2),
              file=sys.stderr)
        return 2
    print(json.dumps({
        "report_path": str(Path(args.report)),
        "graceful_exit": result["graceful_exit"],
        "resumed": result["resumed"],
        "completed": result["report"]["completed"],
        "snapshot_version": result["report"]["snapshot_version"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
