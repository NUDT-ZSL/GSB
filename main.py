#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from chaos_budget.bundle import load_bundle
from chaos_budget.demo import execute_manifest_workflow, run_manifest_workflow
from chaos_budget.engine import Engine
from chaos_budget.errors import ChaosError
from chaos_budget.locking import FileLock
from chaos_budget.reporting import (
    assert_verified,
    build_baseline_status,
    build_database_status,
    verify_database,
)
from chaos_budget.storage import Database


DEFAULT_SAMPLE_DIR = "sample_data"
DEFAULT_DB = Path("runtime") / "chaos_state.sqlite3"


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ChaosError("INVALID_COMMAND", message, exit_code=2)


def emit(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def clock_value(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("clock must be a non-negative integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("clock must be a non-negative integer")
    return value


def get_lock(args: argparse.Namespace, timeout: float = 30.0) -> FileLock:
    return FileLock(str(Path(args.db)) + ".lock", timeout_seconds=timeout)


def open_state_db(
    args: argparse.Namespace,
    bundle: Optional[Dict[str, Any]] = None,
    *,
    auto_initialize: bool = True,
    reset: bool = False,
) -> Database:
    db_path = Path(args.db)
    if reset:
        if bundle is None:
            bundle = load_bundle(args.sample_dir)
        return Database.initialize(db_path, bundle, reset=True)
    if not db_path.exists():
        if not auto_initialize:
            raise ChaosError(
                "STATE_NOT_FOUND",
                f"state database does not exist: {db_path}; run `python main.py load` first",
                {"db_path": str(db_path)},
                exit_code=4,
            )
        if bundle is None:
            bundle = load_bundle(args.sample_dir)
        return Database.initialize(db_path, bundle)
    return Database(db_path).connect()


def cmd_status(args: argparse.Namespace) -> Dict[str, Any]:
    bundle = load_bundle(args.sample_dir)
    db_path = Path(args.db)
    if db_path.exists():
        db = Database(db_path).connect()
        try:
            payload = build_database_status(db)
        finally:
            db.close()
    else:
        payload = build_baseline_status(bundle)
    payload["sample_dir"] = args.sample_dir
    payload["available_commands"] = ["load", "run", "observe", "resume", "status", "verify"]
    return payload


def cmd_load(args: argparse.Namespace) -> Dict[str, Any]:
    bundle = load_bundle(args.sample_dir, injection_clock_ms=args.clock)
    with get_lock(args):
        db = Database.initialize(Path(args.db), bundle, reset=bool(args.reset))
        try:
            status = build_database_status(db)
        finally:
            db.close()
    return {
        "ok": True,
        "command": "load",
        "reset": bool(args.reset),
        "fingerprint": bundle["fingerprint"],
        "policy_conversion": _v1_conversion(bundle),
        "status": status,
    }


def _v1_conversion(bundle: Dict[str, Any]) -> Any:
    for policy in bundle["policy_view"]["policies"].values():
        conversion = policy.get("time_conversion")
        if conversion:
            return conversion
    return None


def cmd_run(args: argparse.Namespace) -> Dict[str, Any]:
    bundle = None
    if not Path(args.db).exists():
        bundle = load_bundle(args.sample_dir)
    with get_lock(args):
        db = open_state_db(args, bundle)
        try:
            engine = Engine(db)
            clock = args.clock if args.clock is not None else db.watermark()
            result = engine.run_scheduling(
                clock,
                round_id=args.round_id,
                experiment_id=args.experiment,
                allow_recovered=bool(args.include_recovered),
            )
            verification_hint = verify_database(db)
        finally:
            db.close()
    return {
        "ok": verification_hint["ok"],
        "command": "run",
        "result": result,
        "invariants_ok": verification_hint["ok"],
    }


def _load_events_for_observe(args: argparse.Namespace, bundle: Dict[str, Any]) -> list[Dict[str, Any]]:
    if args.event_json:
        try:
            raw = json.loads(args.event_json)
        except json.JSONDecodeError as exc:
            raise ChaosError("MALFORMED_JSON", f"invalid --event-json: {exc.msg}") from exc
        from chaos_budget.contracts import validate_observations

        return validate_observations({"observations": [raw]}, [e["experiment_id"] for e in bundle["experiments"]])

    events_reference = args.events or getattr(args, "events_positional", None)
    events_path = Path(events_reference) if events_reference else Path(args.sample_dir) / "observations.json"
    from chaos_budget.contracts import validate_observations

    payload = _read_json(events_path)
    return validate_observations(payload, [e["experiment_id"] for e in bundle["experiments"]])


def _read_json(path: Path) -> Any:
    from chaos_budget.contracts import read_json_file

    return read_json_file(path)


def cmd_observe(args: argparse.Namespace) -> Dict[str, Any]:
    bundle = load_bundle(args.sample_dir)
    if (
        args.clock is None
        and not args.event_json
        and not args.events
        and not getattr(args, "events_positional", None)
        and bundle.get("manifest", {}).get("workflow")
    ):
        with get_lock(args):
            db = open_state_db(args, bundle)
            try:
                engine = Engine(db, bundle)
                steps = execute_manifest_workflow(engine, bundle)
                report = verify_database(db, bundle["fingerprint"])
            finally:
                db.close()
        return {
            "ok": report["ok"],
            "command": "observe",
            "mode": "idempotent_manifest_workflow",
            "workflow_steps": steps,
            "verification_ok": report["ok"],
        }

    events = _load_events_for_observe(args, bundle)
    if args.clock is not None:
        selected = [event for event in events if event["clock_ms"] <= args.clock]
        deferred = len(events) - len(selected)
    else:
        selected = events
        deferred = 0

    with get_lock(args):
        db = open_state_db(args, bundle)
        try:
            engine = Engine(db, bundle)
            results = []
            for event in selected:
                results.append(engine.submit_observation(event))
            report = verify_database(db, bundle["fingerprint"])
        finally:
            db.close()

    return {
        "ok": report["ok"],
        "command": "observe",
        "submitted_or_replayed": len(results),
        "deferred_future_clock_events": deferred,
        "results": results,
        "verification_ok": report["ok"],
    }


def cmd_resume(args: argparse.Namespace) -> Dict[str, Any]:
    bundle = None
    if not Path(args.db).exists():
        bundle = load_bundle(args.sample_dir)
    with get_lock(args):
        db = open_state_db(args, bundle)
        try:
            engine = Engine(db)
            result = engine.resume(args.clock, experiment_id=args.experiment)
            report = verify_database(db)
        finally:
            db.close()
    return {"ok": report["ok"], "command": "resume", "result": result, "verification": report}


def cmd_verify(args: argparse.Namespace) -> Dict[str, Any]:
    bundle = load_bundle(args.sample_dir) if Path(args.sample_dir).exists() else None
    db_path = Path(args.db)
    if args.ephemeral or not db_path.exists():
        if bundle is None:
            raise ChaosError("SAMPLE_DIR_NOT_FOUND", "ephemeral verification needs a valid sample directory")
        workflow = run_manifest_workflow(bundle)
        return {
            "ok": workflow["verify"]["ok"],
            "command": "verify",
            "mode": "ephemeral_manifest_workflow" if not args.ephemeral else "forced_ephemeral",
            "workflow_steps": workflow["steps"],
            "verification": workflow["verify"],
        }

    db = Database(db_path).connect()
    try:
        report = verify_database(db, bundle["fingerprint"] if bundle else None)
        if args.strict:
            assert_verified(report)
        return {"ok": report["ok"], "command": "verify", "mode": "persisted_sqlite", "verification": report}
    finally:
        db.close()


def build_parser() -> JSONArgumentParser:
    parser = JSONArgumentParser(description="Deterministic controlled-chaos budget and recovery engine")
    parser.add_argument("--sample-dir", default=DEFAULT_SAMPLE_DIR)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    sub = parser.add_subparsers(dest="command")

    status = sub.add_parser("status", help="print stable sorted JSON status")
    status.set_defaults(handler=cmd_status)

    load = sub.add_parser("load", help="validate inputs and atomically initialize SQLite state")
    load.add_argument("--clock", type=clock_value, default=None)
    load.add_argument("--reset", action="store_true")
    load.set_defaults(handler=cmd_load)

    run = sub.add_parser("run", help="schedule and admit experiments for one injected clock round")
    run.add_argument("--clock", type=clock_value, default=None)
    run.add_argument("--round-id", default=None)
    run.add_argument("--experiment", default=None)
    run.add_argument("--include-recovered", action="store_true")
    run.set_defaults(handler=cmd_run)

    observe = sub.add_parser("observe", help="submit the observation stream or one idempotent event")
    observe.add_argument("events_positional", nargs="?", default=None, help="optional path to observations JSON")
    observe.add_argument("--events", default=None)
    observe.add_argument("--event-json", default=None)
    observe.add_argument("--clock", type=clock_value, default=None)
    observe.set_defaults(handler=cmd_observe)

    resume = sub.add_parser("resume", help="replay half-committed inbox work and optionally readmit one experiment")
    resume.add_argument("--clock", type=clock_value, default=None)
    resume.add_argument("--experiment", default=None)
    resume.set_defaults(handler=cmd_resume)

    verify = sub.add_parser("verify", help="verify conservation, hash chain, uniqueness and inbox recovery")
    verify.add_argument("--ephemeral", action="store_true")
    verify.add_argument("--strict", action="store_true")
    verify.set_defaults(handler=cmd_verify)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if not getattr(args, "handler", None):
            parser.print_help(sys.stderr)
            return 1
        payload = args.handler(args)
        emit(payload)
        return 0 if payload.get("ok", True) else 6
    except ChaosError as exc:
        emit(exc.to_payload())
        return exc.exit_code
    except FileNotFoundError as exc:
        error = ChaosError("FILE_NOT_FOUND", str(exc))
        emit(error.to_payload())
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI boundary returns JSON
        error = ChaosError("INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")
        emit(error.to_payload())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
