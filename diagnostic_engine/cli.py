from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .checkpoint import ForcedTermination
from .engine import run_engine
from .errors import DiagnosticError
from .generator import DEFAULT_SAMPLE_COUNT, DEFAULT_SEED, generate_dataset
from .snapshots import (
    downgrade_v2_to_v1,
    install_snapshot,
    load_snapshot,
    upgrade_v1_to_v2,
)
from .utils import pretty_json


def _print(value: object) -> None:
    print(pretty_json(value), end="")


def _precisions(value: str) -> tuple[str, ...]:
    if value == "all":
        return ("float64", "float32", "fixed")
    return tuple(item.strip() for item in value.split(",") if item.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diagnostic-engine",
        description="Deterministic regularization path diagnostics for tabular binary models",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    generate = subparsers.add_parser("generate", help="generate deterministic data and snapshots")
    generate.add_argument("--out", default="data")
    generate.add_argument("--seed", type=int, default=DEFAULT_SEED)
    generate.add_argument("--samples", type=int, default=DEFAULT_SAMPLE_COUNT)

    train = subparsers.add_parser("train", help="train and diagnose a regularization path")
    train.add_argument("--data", default="data/samples.jsonl")
    train.add_argument("--snapshot", default="data/snapshots/v2")
    train.add_argument("--out", default="artifacts/run")
    train.add_argument("--precision", default="all")
    train.add_argument("--inject", action="append", default=[])
    train.add_argument("--no-resume", action="store_true")
    train.add_argument("--abort-after-steps", type=int)
    train.add_argument("--abort-precision")

    inspect = subparsers.add_parser("inspect-snapshot", help="load and print a unified snapshot")
    inspect.add_argument("--snapshot", required=True)

    snapshots = subparsers.add_parser("snapshots", help="migrate or install snapshots")
    snapshot_commands = snapshots.add_subparsers(dest="snapshot_command", required=True)
    upgrade = snapshot_commands.add_parser("upgrade", help="migrate v1 JSON to v2 shards")
    upgrade.add_argument("--v1", required=True)
    upgrade.add_argument("--out", required=True)
    downgrade = snapshot_commands.add_parser("downgrade", help="migrate v2 shards to v1 JSON")
    downgrade.add_argument("--v2", required=True)
    downgrade.add_argument("--out", required=True)
    install = snapshot_commands.add_parser("install", help="atomically install a complete snapshot")
    install.add_argument("--source", required=True)
    install.add_argument("--store", required=True)
    install.add_argument("--slot", default="active")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_dataset(
                args.out, seed=args.seed, sample_count=args.samples
            )
            _print(result.to_dict())
            return 0
        if args.command == "train":
            report = run_engine(
                data_path=args.data,
                snapshot_path=args.snapshot,
                output_directory=args.out,
                precisions=_precisions(args.precision),
                injections=args.inject,
                resume=not args.no_resume,
                abort_after_steps=args.abort_after_steps,
                abort_precision=args.abort_precision,
            )
            summary = {
                "status": report["status"],
                "run_id": report["run_id"],
                "report": str(Path(args.out) / "run_report.json"),
                "degradation_categories": report["degradation_categories"],
                "precisions": {
                    name: {
                        "final_objective": item["run"]["steps"][-1]["objective"],
                        "converged": item["run"]["steps"][-1]["converged"],
                        "reused_steps": item["resume"]["reused_steps"],
                        "recomputed_steps": item["resume"]["recomputed_steps"],
                    }
                    for name, item in report["precisions"].items()
                },
                "validation": report["validation"]["status"],
                "inference_accuracy": report["inference"]["accuracy"],
            }
            _print(summary)
            return 0
        if args.command == "inspect-snapshot":
            _print(load_snapshot(args.snapshot).to_dict())
            return 0
        if args.command == "snapshots":
            if args.snapshot_command == "upgrade":
                _print(upgrade_v1_to_v2(args.v1, args.out).to_dict())
                return 0
            if args.snapshot_command == "downgrade":
                _print(downgrade_v2_to_v1(args.v2, args.out).to_dict())
                return 0
            if args.snapshot_command == "install":
                _print(
                    install_snapshot(
                        args.source, args.store, slot=args.slot
                    ).to_dict()
                )
                return 0
        parser.error("unhandled command")
    except ForcedTermination as exc:
        _print({"status": "terminated", "forced_termination": exc.to_dict()})
        return 75
    except DiagnosticError as exc:
        _print({"status": "error", "error": exc.to_dict()})
        if exc.code == "E_STAGE_BUSY":
            return 73
        if exc.code == "E_INCONSISTENT":
            return 65
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())

