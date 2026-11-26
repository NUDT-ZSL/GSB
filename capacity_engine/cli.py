from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import CapacityError, ErrorCode
from .seed import generate_seed_directory
from .validation import load_input
from .engine import CapacityEngine
from .verify import verify_output_directory


def _print(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="capacity-engine", description="Deterministic capacity diagnostic engine")
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="generate the fixed-seed local sample")
    seed.add_argument("--dir", required=True)
    seed.add_argument("--no-clean", action="store_true")

    validate = sub.add_parser("validate", help="validate all input files without writing state")
    validate.add_argument("--input", required=True)

    run = sub.add_parser("run", help="run baseline and all committed patches")
    run.add_argument("--input", required=True)
    run.add_argument("--out", required=True)
    run.add_argument("--crash-at", choices=["window_aggregation", "fixedpoint_conversion", "threshold_merge", "patch_commit"])
    run.add_argument("--crash-patch")

    verify = sub.add_parser("verify", help="verify SQLite and report precision invariants")
    verify.add_argument("--out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "seed":
            path = generate_seed_directory(args.dir, clean=not args.no_clean)
            _print({"ok": True, "generated": str(path), "seed": 20261127})
            return 0
        if args.command == "validate":
            data = load_input(args.input)
            _print(
                {
                    "ok": True,
                    "services": len(data.scenario["services"]),
                    "traces": len(data.traces),
                    "v2_shards": len(data.v2_shards),
                    "patches": len(data.patches),
                }
            )
            return 0

        if args.command == "run":
            data = load_input(args.input)
            engine = CapacityEngine(
                data,
                args.input,
                args.out,
                crash_at=args.crash_at,
                crash_patch=args.crash_patch,
            )
            summary = engine.run()
            _print(summary)
            return 0
        if args.command == "verify":
            result = verify_output_directory(args.out)
            _print(result)
            return 0 if result["ok"] else 2
        raise CapacityError(ErrorCode.INTERNAL, f"unknown command {args.command}")
    except CapacityError as exc:
        _print(exc.to_dict())
        return 2
    except Exception as exc:
        error = CapacityError(ErrorCode.INTERNAL, str(exc), {"type": type(exc).__name__})
        _print(error.to_dict())
        return 2


if __name__ == "__main__":
    sys.exit(main())
