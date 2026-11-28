"""Command-line entry point."""

from __future__ import annotations

import argparse
import json
import sys

from .errors import ContractMigrationError, error_by_code
from .generator import (
    DEFAULT_BATCH_CAPACITY,
    DEFAULT_MIGRATION_THRESHOLD_BP,
    DEFAULT_SEED,
    DEFAULT_WINDOW_SIZE,
)
from .workflow import (
    resume_from_snapshot,
    run_scenario,
    write_json_atomic,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contract-migration",
        description=(
            "Deterministic electronic-contract hotspot detection and "
            "resumable shard migration simulator."
        ),
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--window-size", type=int, default=DEFAULT_WINDOW_SIZE)
    parser.add_argument(
        "--batch-capacity", type=int, default=DEFAULT_BATCH_CAPACITY
    )
    parser.add_argument(
        "--migration-threshold-bp",
        type=int,
        default=DEFAULT_MIGRATION_THRESHOLD_BP,
    )
    parser.add_argument(
        "--interrupt-batch-index",
        type=int,
        default=1,
        help="zero-based planned batch index used for the built-in crash test",
    )
    parser.add_argument(
        "--interrupt-after-contracts",
        type=int,
        default=3,
        help="contracts fully synced in the active batch before the crash",
    )
    parser.add_argument(
        "--interrupt-after-requests",
        type=int,
        default=20,
        help="active-batch requests processed before the crash",
    )
    parser.add_argument(
        "--snapshot",
        default="artifacts/interrupted_snapshot.json",
        help="durable crash snapshot path; use '-' to skip writing a file",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="report JSON path, or '-' for stdout",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="SNAPSHOT",
        help="resume from a persisted snapshot instead of running a new scenario",
    )
    parser.add_argument(
        "--inject-error",
        type=int,
        default=None,
        metavar="INTEGER_CODE",
        help="inject a stable integer error code to verify the error contract",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="emit compact JSON to stdout instead of indented output",
    )
    return parser


def _print_error(error: ContractMigrationError) -> int:
    print(
        json.dumps(
            error.as_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=False,
        )
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.inject_error is not None:
            raise error_by_code(args.inject_error)
        if args.resume:
            report = resume_from_snapshot(args.resume)
            output = args.output or "artifacts/resumed_contract_migration_report.json"
        else:
            snapshot_path = None if args.snapshot == "-" else args.snapshot
            report = run_scenario(
                seed=args.seed,
                window_size=args.window_size,
                batch_capacity=args.batch_capacity,
                migration_threshold_bp=args.migration_threshold_bp,
                interrupt_batch_index=args.interrupt_batch_index,
                interrupt_after_contracts=args.interrupt_after_contracts,
                interrupt_after_requests=args.interrupt_after_requests,
                snapshot_path=snapshot_path,
            )
            output = args.output or "artifacts/contract_migration_report.json"
        if output == "-":
            if args.compact:
                print(
                    json.dumps(
                        report,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                )
            else:
                print(
                    json.dumps(
                        report,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=False,
                    )
                )
        else:
            write_json_atomic(output, report)
            summary = {
                "ok": True,
                "report_path": output,
                "report_kind": report["report_kind"],
            }
            if "execution_summary" in report:
                summary["execution_summary"] = report["execution_summary"]
            else:
                summary["equivalence"] = report["equivalence_to_full_migration"][
                    "equivalent"
                ]
            print(
                json.dumps(
                    summary,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=False,
                )
            )
        return 0
    except ContractMigrationError as error:
        return _print_error(error)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
