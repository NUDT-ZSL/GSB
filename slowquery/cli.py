"""Command line interface for the deterministic slow-query engine."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_json
from .dbmodel import load_db_model, validate_stats_against_model
from .errors import EngineError
from .planner import run_plan
from .samples import (
    load_json_file,
    load_samples_file,
    validate_injected_clock,
    validate_samples_document,
)
from .snapshots import (
    finalize_snapshot,
    merge_patch,
    normalize_statistics,
)
from .status import build_status
from .store import StateStore
from .verification import verify_state


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SAMPLE_DIR = PROJECT_ROOT / "samples"
DEFAULT_STATE = PROJECT_ROOT / ".slowquery" / "state.db"
DEFAULT_DB = SAMPLE_DIR / "sample.db"
DEFAULT_SAMPLES = SAMPLE_DIR / "slow_queries.json"
DEFAULT_STATS = SAMPLE_DIR / "stats_snapshot.json"
DEFAULT_PATCH = SAMPLE_DIR / "stats_patch.json"


class CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise EngineError("ERR_USAGE", message)


def _state_path(value: str | None) -> Path:
    if value:
        return Path(value).expanduser()
    return Path(os.environ.get("SLOWQUERY_STATE", str(DEFAULT_STATE))).expanduser()


def _open_existing(path: Path) -> StateStore:
    if not path.exists():
        raise EngineError(
            "ERR_STATE_NOT_INITIALIZED",
            "state database has not been initialized",
            {"path": str(path)},
        )
    return StateStore(path).open()


def _print_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n"
    )


def command_load(args: argparse.Namespace) -> dict[str, Any]:
    state_path = _state_path(args.state)
    snapshot = finalize_snapshot(
        normalize_statistics(load_json_file(args.stats), path=str(args.stats))
    )
    samples = load_samples_file(args.samples, window_ms=args.window_ms)
    db_path = Path(args.db)
    rebuilt = False
    if not db_path.exists() and db_path.resolve() == DEFAULT_DB.resolve():
        try:
            from samples.rebuild_db import rebuild

            rebuild(db_path)
            rebuilt = True
        except Exception as exc:
            raise EngineError(
                "ERR_SAMPLE_DB_BUILD",
                "cannot rebuild the default sample database",
                {"path": str(db_path), "error": str(exc)},
            ) from exc
    model = load_db_model(db_path)
    validate_stats_against_model(snapshot, model)
    store = StateStore(state_path).open()
    try:
        outcome = store.initialize(
            model=model,
            snapshot=snapshot,
            window_ms=args.window_ms,
            reset=args.reset,
        )
    finally:
        store.close()
    return {
        "ok": True,
        "command": "load",
        "state": str(state_path),
        "model_digest": model["digest"],
        "snapshot_version": snapshot["version"],
        "snapshot_digest": snapshot["digest"],
        "validated_samples": len(samples),
        "rebuilt_sample_db": rebuilt,
        **outcome,
    }


def command_sample(args: argparse.Namespace) -> dict[str, Any]:
    state_path = _state_path(args.state)
    store = _open_existing(state_path)
    try:
        watermark = store.watermark()
        samples = load_samples_file(args.samples, window_ms=watermark["window_ms"])
        error_clock = validate_injected_clock(args.clock_ms, samples)
        outcome = store.ingest_samples(samples, injected_clock_ms=error_clock)
    finally:
        store.close()
    return {
        "ok": True,
        "command": "sample",
        **outcome,
    }


def command_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    state_path = _state_path(args.state)
    store = _open_existing(state_path)
    try:
        model = store.get_db_model()
        current = store.current_snapshot()
        raw_patch = load_json_file(args.patch)
        patch = normalize_statistics(
            raw_patch, partial=True, path=str(args.patch)
        )
        merged, _classification = merge_patch(current, patch)
        validate_stats_against_model(merged, model)
        outcome = store.apply_snapshot_patch(patch, model=model)
    finally:
        store.close()
    return {
        "ok": True,
        "command": "snapshot",
        **outcome,
    }


def _default_clock(store: StateStore, clock: int | None) -> int:
    if clock is not None:
        if isinstance(clock, bool) or clock < 0:
            raise EngineError(
                "ERR_CLOCK_CONFIG",
                "logical clock must be a non-negative integer",
            )
        return clock
    return store.watermark()["last_clock_ms"]


def command_plan(args: argparse.Namespace) -> dict[str, Any]:
    state_path = _state_path(args.state)
    store = _open_existing(state_path)
    try:
        clock = _default_clock(store, args.clock_ms)
        outcome = run_plan(
            store,
            resume=False,
            max_workers=args.max_workers,
            memory_budget=args.memory_budget,
            memory_unit=args.memory_unit,
            logical_clock_ms=clock,
        )
    finally:
        store.close()
    return {"ok": True, "command": "plan", **outcome}


def command_resume(args: argparse.Namespace) -> dict[str, Any]:
    state_path = _state_path(args.state)
    store = _open_existing(state_path)
    try:
        if not args.skip_sample:
            watermark = store.watermark()
            samples = load_samples_file(
                args.samples, window_ms=watermark["window_ms"]
            )
            clock = validate_injected_clock(args.clock_ms, samples)
            sample_outcome = store.ingest_samples(
                samples, injected_clock_ms=clock
            )
        else:
            sample_outcome = {
                "inserted": 0,
                "duplicates": 0,
                "skipped": True,
            }
        latest = store.latest_run()
        max_workers = (
            args.max_workers
            if args.max_workers is not None
            else (latest["max_workers"] if latest else 2)
        )
        memory_budget = (
            args.memory_budget
            if args.memory_budget is not None
            else (latest["memory_budget"] if latest else 1024)
        )
        memory_unit = (
            args.memory_unit
            if args.memory_unit is not None
            else (latest["memory_unit"] if latest else 8)
        )
        clock = _default_clock(store, args.clock_ms)
        plan_outcome = run_plan(
            store,
            resume=True,
            max_workers=max_workers,
            memory_budget=memory_budget,
            memory_unit=memory_unit,
            logical_clock_ms=clock,
            retry_abandoned=args.retry_abandoned,
            retry_failed=args.retry_failed,
        )
    finally:
        store.close()
    return {
        "ok": True,
        "command": "resume",
        "sampling": sample_outcome,
        "planning": plan_outcome,
    }


def command_status(args: argparse.Namespace) -> dict[str, Any]:
    store = _open_existing(_state_path(args.state))
    try:
        return build_status(store)
    finally:
        store.close()


def command_verify(args: argparse.Namespace) -> dict[str, Any]:
    store = _open_existing(_state_path(args.state))
    try:
        return verify_state(store)
    finally:
        store.close()


def build_parser() -> CliParser:
    parser = CliParser(
        prog="slowquery",
        description="Deterministic local slow-query diagnosis engine",
    )
    parser.add_argument(
        "--state",
        default=None,
        help=f"state database path (default: {DEFAULT_STATE})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_state_option(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--state",
            default=argparse.SUPPRESS,
            help="state database path (also accepted before the subcommand)",
        )

    load = subparsers.add_parser("load", help="validate inputs and initialize state")
    add_state_option(load)
    load.add_argument("--db", default=str(DEFAULT_DB))
    load.add_argument("--samples", default=str(DEFAULT_SAMPLES))
    load.add_argument("--stats", default=str(DEFAULT_STATS))
    load.add_argument("--window-ms", type=int, default=1000)
    load.add_argument("--reset", action="store_true")
    load.set_defaults(handler=command_load)

    sample = subparsers.add_parser("sample", help="ingest and aggregate a sample stream")
    add_state_option(sample)
    sample.add_argument("--samples", default=str(DEFAULT_SAMPLES))
    sample.add_argument("--clock-ms", type=int, default=None)
    sample.set_defaults(handler=command_sample)

    snapshot = subparsers.add_parser(
        "snapshot", help="atomically apply a statistics patch"
    )
    add_state_option(snapshot)
    snapshot.add_argument("--patch", default=str(DEFAULT_PATCH))
    snapshot.set_defaults(handler=command_snapshot)

    plan = subparsers.add_parser("plan", help="run the bounded parallel profiler")
    add_state_option(plan)
    plan.add_argument("--max-workers", type=int, default=2)
    plan.add_argument("--memory-budget", type=int, default=1024)
    plan.add_argument("--memory-unit", type=int, default=8)
    plan.add_argument("--clock-ms", type=int, default=None)
    plan.set_defaults(handler=command_plan)

    resume = subparsers.add_parser(
        "resume", help="resume sampling and unfinished profiling work"
    )
    add_state_option(resume)
    resume.add_argument("--samples", default=str(DEFAULT_SAMPLES))
    resume.add_argument("--skip-sample", action="store_true")
    resume.add_argument("--max-workers", type=int, default=None)
    resume.add_argument("--memory-budget", type=int, default=None)
    resume.add_argument("--memory-unit", type=int, default=None)
    resume.add_argument("--clock-ms", type=int, default=None)
    resume.add_argument("--retry-abandoned", action="store_true")
    resume.add_argument("--retry-failed", action="store_true")
    resume.set_defaults(handler=command_resume)

    status = subparsers.add_parser("status", help="emit stable JSON status")
    add_state_option(status)
    status.set_defaults(handler=command_status)

    verify = subparsers.add_parser(
        "verify", help="compare persisted state with serial expectations"
    )
    add_state_option(verify)
    verify.set_defaults(handler=command_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        payload = args.handler(args)
        _print_json(payload)
        if args.command == "verify" and not payload.get("ok", False):
            return 1
        return 0
    except EngineError as exc:
        sys.stderr.write(
            json.dumps(exc.as_dict(), ensure_ascii=True, sort_keys=True) + "\n"
        )
        return 1
    except KeyboardInterrupt:
        sys.stderr.write(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "ERR_INTERRUPTED",
                        "message": "operation interrupted",
                        "details": {},
                    },
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 130
    except Exception as exc:  # pragma: no cover - final safety net
        if os.environ.get("SLOWQUERY_DEBUG") == "1":
            raise
        sys.stderr.write(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "ERR_INTERNAL",
                        "message": str(exc),
                        "details": {"exception_type": type(exc).__name__},
                    },
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            + "\n"
        )
        return 1
