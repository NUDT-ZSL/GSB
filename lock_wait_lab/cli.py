"""Stable JSON command-line interface for the lock-wait laboratory."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Sequence

from .canonical import stable_output
from .engine import Engine
from .errors import ErrorCode, LabError
from .samples import load_bundle
from .store import Store
from .verify import verify_run


DEFAULT_SAMPLES = Path("samples")
DEFAULT_DB = Path("lock_wait_lab.sqlite3")
DEFAULT_RUN_ID = "main"


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise LabError(ErrorCode.CLI, message, field="arguments")


def _common_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--samples", type=Path, default=DEFAULT_SAMPLES)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--run-id", default=DEFAULT_RUN_ID)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog="lock-wait-lab",
        description="Deterministic lock-wait ledger and rollback adjudication engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate samples and initialize state")
    _common_parser(load)
    load.add_argument("--reset", action="store_true")
    load.add_argument("--parallelism", type=int)
    load.add_argument("--round-budget-ms", type=int)
    load.add_argument("--max-logical-ms", type=int)

    run = subparsers.add_parser("run", help="execute or continue the run")
    _common_parser(run)
    run.add_argument("--reset", action="store_true")
    run.add_argument("--parallelism", type=int)
    run.add_argument("--round-budget-ms", type=int)
    run.add_argument("--max-logical-ms", type=int)
    run.add_argument("--max-steps", type=int, default=100000)
    run.add_argument(
        "--crash-after-step",
        type=int,
        help="durably checkpoint, then terminate with SIGKILL at this step",
    )
    run.add_argument(
        "--simulate-crash",
        action="store_true",
        help="raise CrashInjected instead of sending SIGKILL",
    )
    run.add_argument(
        "--pause-after-step",
        type=int,
        help="pause after this durable checkpoint for external SIGKILL",
    )
    run.add_argument("--pause-seconds", type=float, default=30.0)

    resume = subparsers.add_parser("resume", help="resume an interrupted run")
    _common_parser(resume)
    resume.add_argument("--max-steps", type=int, default=100000)

    snapshot = subparsers.add_parser("snapshot", help="inspect or activate a policy snapshot")
    _common_parser(snapshot)
    snapshot.add_argument("--generation", type=int)

    status = subparsers.add_parser("status", help="show persisted run status")
    _common_parser(status)

    verify = subparsers.add_parser("verify", help="repeat deterministic and recovery checks")
    _common_parser(verify)
    verify.add_argument("--no-recovery", action="store_true")
    return parser


def _active_policy_data(engine: Engine) -> dict[str, Any]:
    generation = int(engine.state["policy"]["generation"])
    view = engine.bundle.policy(generation)
    return {
        "generation": generation,
        "effective_ms": view.effective_ms,
        "summary": view.summary,
        "scale": view.scale,
        "tolerance_ns": view.tolerance_ns,
        "ledger_version": engine.state["policy"]["ledger_version"],
        "last_commit_seq": engine.state["policy"]["last_commit_seq"],
    }


def _execution_fields(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "pending_transactions": summary["pending_transactions"],
        "ledger_summary": summary["ledger_summary"],
        "actual_policy_versions": summary["actual_policy_versions"],
        "error_bound": summary["error_bound"],
        "precision_sensitive_records": summary["precision_sensitive_records"],
        "decisions": summary["decisions"],
        "state_digest": summary["state_digest"],
        "ledger_digest": summary["ledger_digest"],
    }


def command_load(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_bundle(args.samples)
    store = Store(args.db)
    try:
        existed = store.get_run(args.run_id) is not None
        engine = Engine.initialize(
            bundle,
            store,
            run_id=args.run_id,
            reset=args.reset,
            parallelism=args.parallelism,
            round_budget_ms=args.round_budget_ms,
            max_logical_ms=args.max_logical_ms,
        )
        summary = engine.summary()
        data = {
            "samples": bundle.summary(),
            "run_id": args.run_id,
            "created": not existed or args.reset,
            "state": engine.state["run_status"],
            "committed_step": engine.state["step"],
            "active_policy": _active_policy_data(engine),
        }
        data.update(_execution_fields(summary))
        return data
    finally:
        store.close()


def command_run(args: argparse.Namespace, *, explicit_resume: bool) -> dict[str, Any]:
    bundle = load_bundle(args.samples)
    store = Store(args.db)
    try:
        existing = store.get_run(args.run_id)
        if explicit_resume and existing is None:
            raise LabError(
                ErrorCode.STATE_MISSING,
                f"run {args.run_id!r} has not been initialized",
                field="run_id",
                path=str(args.db),
            )
        engine = Engine.initialize(
            bundle,
            store,
            run_id=args.run_id,
            reset=getattr(args, "reset", False),
            parallelism=getattr(args, "parallelism", None),
            round_budget_ms=getattr(args, "round_budget_ms", None),
            max_logical_ms=getattr(args, "max_logical_ms", None),
        )
        was_incomplete = engine.state["run_status"] != "COMPLETED"
        was_recovering = existing is not None and (
            engine.state["step"] > 0 or engine.state["run_status"] == "RUNNING"
        )
        if explicit_resume and was_incomplete:
            engine.mark_resumed()
        elif (
            not explicit_resume
            and was_incomplete
            and was_recovering
            and engine.state["step"] > 0
        ):
            engine.mark_resumed()
        summary = engine.run(
            max_steps=args.max_steps,
            crash_after_step=getattr(args, "crash_after_step", None),
            pause_after_step=getattr(args, "pause_after_step", None),
            pause_seconds=getattr(args, "pause_seconds", 30.0),
            simulate_crash=getattr(args, "simulate_crash", False),
        )
        summary["resume_command_used"] = explicit_resume
        summary["recovered_from_persisted_state"] = (
            was_recovering and was_incomplete
        )
        return summary
    finally:
        store.close()


def command_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_bundle(args.samples)
    store = Store(args.db)
    try:
        run = store.get_run(args.run_id)
        activation: dict[str, Any] | None = None
        active: dict[str, Any] | None = None
        if run is not None:
            engine = Engine(bundle, store, args.run_id)
            active = _active_policy_data(engine)
            if args.generation is not None:
                activation = engine.activate_snapshot(args.generation)
                active = _active_policy_data(engine)
        else:
            active = {
                "generation": min(bundle.policy_views),
                "effective_ms": bundle.policy_views[
                    min(bundle.policy_views)
                ].effective_ms,
                "summary": bundle.policy_views[min(bundle.policy_views)].summary,
                "ledger_version": 0,
                "last_commit_seq": 0,
            }
            if args.generation is not None and args.generation != active["generation"]:
                raise LabError(
                    ErrorCode.STATE_MISSING,
                    "activate a snapshot after the run state is initialized",
                    field="run_id",
                )
        snapshots = []
        for generation in sorted(bundle.policy_views):
            view = bundle.policy_views[generation]
            snapshots.append(
                {
                    "generation": view.generation,
                    "effective_ms": view.effective_ms,
                    "summary": view.summary,
                    "scale": view.scale,
                    "tolerance_ns": view.tolerance_ns,
                    "active_entries": sum(
                        1
                        for entry in view.entries.values()
                        if not entry["tombstone"]
                    ),
                    "tombstones": sum(
                        1
                        for entry in view.entries.values()
                        if entry["tombstone"]
                    ),
                }
            )
        data = {
            "snapshots": snapshots,
            "active": active,
            "requested_generation": args.generation,
            "activation": activation,
        }
        if run is not None:
            data.update(_execution_fields(Engine(bundle, store, args.run_id).summary()))
        else:
            data.update(
                {
                    "pending_transactions": [],
                    "ledger_summary": {
                        "active_ledger_version": 0,
                        "entry_count": 0,
                        "closed_entry_count": 0,
                        "open_entry_count": 0,
                        "total_wait_ns": 0,
                        "total_wait_ms": "0",
                        "max_cumulative_error_bound_ns": 0,
                        "ledger_versions": [],
                        "archived_ledger_versions": [],
                    },
                    "actual_policy_versions": [],
                    "error_bound": {"max_cumulative_ns": 0, "by_entry": []},
                    "precision_sensitive_records": [],
                    "decisions": [],
                    "state_digest": None,
                    "ledger_digest": None,
                }
            )
        return data
    finally:
        store.close()


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_bundle(args.samples)
    store = Store(args.db)
    try:
        run = store.get_run(args.run_id)
        if run is None:
            raise LabError(
                ErrorCode.STATE_MISSING,
                f"run {args.run_id!r} has not been initialized",
                field="run_id",
                path=str(args.db),
            )
        engine = Engine(bundle, store, args.run_id)
        return engine.summary()
    finally:
        store.close()


def command_verify(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    bundle = load_bundle(args.samples)
    result = verify_run(
        bundle,
        args.db,
        args.run_id,
        run_recovery=not args.no_recovery,
    )
    return result, bool(result["ok"])


def dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    if args.command == "load":
        return command_load(args), True
    if args.command == "run":
        return command_run(args, explicit_resume=False), True
    if args.command == "resume":
        return command_run(args, explicit_resume=True), True
    if args.command == "snapshot":
        return command_snapshot(args), True
    if args.command == "status":
        return command_status(args), True
    if args.command == "verify":
        return command_verify(args)
    raise LabError(ErrorCode.CLI, f"unknown command {args.command!r}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        data, success = dispatch(args)
        payload = {
            "ok": success,
            "command": args.command,
            "data": data,
        }
        print(stable_output(payload))
        return 0 if success else 1
    except LabError as exc:
        print(stable_output(exc.to_dict()))
        return 2
    except KeyboardInterrupt:
        print(
            stable_output(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERRUPTED",
                        "message": "interrupted",
                    },
                }
            )
        )
        return 130
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        print(
            stable_output(
                {
                    "ok": False,
                    "error": {
                        "code": ErrorCode.INTERNAL,
                        "message": f"{type(exc).__name__}: {exc}",
                    },
                }
            )
        )
        return 3
