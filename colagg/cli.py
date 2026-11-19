"""Command line interface for the local aggregation engine."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any

from .canonical import atomic_write_text, pretty_json
from .engine import Engine
from .errors import (
    ColAggError,
    E_INTERNAL,
    E_PLAN_CHANGED,
    E_VERIFY_FAILED,
)
from .schema import (
    Batch,
    PlanDefinition,
    parse_batches,
    parse_precision,
    parse_query,
    validate_plan_against_batches,
)
from .storage import current_plan_version, open_database


def _add_common_paths(parser: argparse.ArgumentParser, *, inputs: bool = True) -> None:
    parser.add_argument("--db", default="state.db", help="SQLite state database")
    parser.add_argument("--output-dir", default="output", help="result output directory")
    if inputs:
        parser.add_argument("--batches", default="samples/batches.json")
        parser.add_argument("--query", default="samples/query.json")
        parser.add_argument("--precision", default="samples/precision.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="colagg",
        description="Deterministic columnar aggregation and sliding-window engine",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    load_parser = subcommands.add_parser("load", help="validate and load a plan and batches")
    _add_common_paths(load_parser)
    load_parser.add_argument("--plan-version", type=int, default=None)
    load_parser.add_argument("--clock", type=int, default=None)

    for command_name in ("execute", "resume"):
        run_parser = subcommands.add_parser(
            command_name,
            help="execute pending batches (resume includes recovery)",
        )
        _add_common_paths(run_parser)
        run_parser.add_argument("--clock", type=int, default=None)
        run_parser.add_argument("--max-batches", type=int, default=None)
        run_parser.add_argument("--crash-after", type=int, default=None)
        run_parser.add_argument("--no-finalize", action="store_true")

    status_parser = subcommands.add_parser("status", help="show stable operator status JSON")
    _add_common_paths(status_parser, inputs=False)

    verify_parser = subcommands.add_parser(
        "verify", help="verify state, outputs, and full-recomputation truth"
    )
    _add_common_paths(verify_parser)

    plan_parser = subcommands.add_parser("plan", help="inspect or replace plan snapshots")
    plan_sub = plan_parser.add_subparsers(dest="plan_command", required=True)
    plan_show = plan_sub.add_parser("show")
    _add_common_paths(plan_show, inputs=False)
    plan_replace = plan_sub.add_parser("replace")
    _add_common_paths(plan_replace)
    plan_replace.add_argument("--plan-version", type=int, default=None)
    plan_replace.add_argument("--clock", type=int, default=None)

    clean_parser = subcommands.add_parser("clean", help="remove state and output artifacts")
    _add_common_paths(clean_parser, inputs=False)
    return parser


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    return Path(args.db), Path(args.output_dir)


def _parse_inputs(
    args: argparse.Namespace,
    *,
    verify_batch_digest: bool = True,
) -> tuple[list[Batch], PlanDefinition]:
    precision = parse_precision(Path(args.precision))
    plan = parse_query(
        Path(args.query),
        precision,
        plan_version=getattr(args, "plan_version", None),
    )
    batches = parse_batches(
        Path(args.batches), verify_digest=verify_batch_digest
    )
    validate_plan_against_batches(plan, batches)
    return batches, plan


def _assert_current_plan(database_path: Path, plan: PlanDefinition) -> None:
    connection = open_database(database_path)
    try:
        current = current_plan_version(connection)
    finally:
        connection.close()
    if current is None:
        raise ColAggError(E_PLAN_CHANGED, "no current plan; run load first")
    if current != (plan.version, plan.digest):
        raise ColAggError(
            E_PLAN_CHANGED,
            "query or precision files do not match the current persisted plan",
            details={
                "current_version": current[0],
                "current_digest": current[1],
                "incoming_version": plan.version,
                "incoming_digest": plan.digest,
            },
        )


def _write_status_file(payload: dict[str, Any], output_dir: Path) -> str:
    path = output_dir / "status.json"
    atomic_write_text(path, pretty_json(payload) + "\n")
    return str(path)


def _clean(args: argparse.Namespace) -> dict[str, Any]:
    database, output_dir = _paths(args)
    removed: list[str] = []
    for candidate in (
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
        Path(str(database) + ".lock"),
        output_dir,
    ):
        if candidate.is_dir():
            shutil.rmtree(candidate)
            removed.append(str(candidate))
        elif candidate.exists():
            candidate.unlink()
            removed.append(str(candidate))
    return {"ok": True, "command": "clean", "removed": removed}


def _run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    database, output_dir = _paths(args)
    if args.command == "clean":
        return _clean(args), 0

    if args.command == "status":
        engine = Engine(database, output_dir)
        payload = engine.status()
        payload["status_path"] = _write_status_file(payload, output_dir)
        return payload, 0

    if args.command == "plan":
        engine = Engine(database, output_dir)
        if args.plan_command == "show":
            return engine.plan_history(), 0
        batches, plan = _parse_inputs(args)
        payload = engine.load(batches, plan, clock=args.clock)
        payload["command"] = "plan replace"
        return payload, 0

    if args.command == "load":
        batches, plan = _parse_inputs(args)
        engine = Engine(database, output_dir)
        return engine.load(batches, plan, clock=args.clock), 0

    batches, plan = _parse_inputs(args, verify_batch_digest=False)
    _assert_current_plan(database, plan)
    engine = Engine(database, output_dir)
    if args.command == "verify":
        report = engine.verify(batches)
        if not report["ok"]:
            report["error"] = {
                "code": E_VERIFY_FAILED,
                "message": "verification failed",
            }
            return report, 1
        return report, 0

    result = engine.execute(
        batches,
        clock=args.clock,
        max_batches=args.max_batches,
        crash_after=args.crash_after,
        finalize=not args.no_finalize,
        command=args.command,
    )
    if not result["ok"]:
        first_failure = (
            result["failures"][0]
            if result["failures"]
            else (result["recovery_errors"][0] if result["recovery_errors"] else None)
        )
        result["error"] = {
            "code": first_failure["code"] if first_failure else E_VERIFY_FAILED,
            "message": (
                first_failure["message"]
                if first_failure
                else "execution completed with failed operators"
            ),
        }
        return result, 1
    return result, 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        payload, exit_code = _run(args)
    except ColAggError as error:
        print(pretty_json(error.as_dict()))
        return error.exit_code
    except Exception as error:  # pragma: no cover - defensive CLI boundary
        wrapped = ColAggError(
            E_INTERNAL,
            f"unexpected internal error: {error}",
            details={"type": type(error).__name__},
        )
        print(pretty_json(wrapped.as_dict()))
        if str(Path.cwd()) and os_debug():
            traceback.print_exc()
        return 3
    print(pretty_json(payload))
    return exit_code


def os_debug() -> bool:
    import os

    return os.environ.get("COLAGG_DEBUG") == "1"


if __name__ == "__main__":
    raise SystemExit(main())

