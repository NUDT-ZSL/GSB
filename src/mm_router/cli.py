"""Command-line entry points for sample generation and acceptance diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .errors import WorkbenchError
from .generate import DEFAULT_SEED, generate_sample
from .workbench import run_acceptance_demo


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mm-router",
        description="Deterministic multimodal adaptive-routing diagnostic workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate fixed-seed local samples")
    generate.add_argument("--out", default="artifacts/demo/sample")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--count", type=int, default=16)

    demo = subparsers.add_parser("demo", help="run the complete local acceptance demo")
    demo.add_argument("--out", default="artifacts/demo/run")
    demo.add_argument("--seed", type=int, default=DEFAULT_SEED)
    demo.add_argument(
        "--no-processes",
        action="store_true",
        help="use in-process scheduling for constrained environments",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_sample(
                Path(args.out),
                seed=args.seed,
                request_count=args.count,
            )
        elif args.command == "demo":
            result = run_acceptance_demo(
                Path(args.out),
                seed=args.seed,
                use_processes=not args.no_processes,
            )
        else:  # pragma: no cover - argparse enforces this
            raise AssertionError("unreachable command")
    except WorkbenchError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, sort_keys=True, indent=2))
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

