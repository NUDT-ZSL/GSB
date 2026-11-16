"""Command-line interface and deterministic acceptance demonstration."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .errors import E_SCRIPT, E_STORAGE, LabError
from .models import SchedulerConfig
from .replay import replay_snapshot
from .reporting import format_replay_report, format_run_summary
from .service import execute_script_file, validate_script_file
from .snapshot import list_snapshots, restore_snapshot, save_snapshot
from .storage import SQLiteStore
from .util import atomic_write_json


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INIT_SQL = PROJECT_ROOT / "samples" / "initial_rows.sql"
DEFAULT_SNAPSHOT_DIR = PROJECT_ROOT / "snapshots"
DEFAULT_RUNTIME_DIR = PROJECT_ROOT / "runtime"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except LabError as exc:
        print(
            json.dumps(
                {"status": "error", **exc.to_dict()},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "code": E_STORAGE,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="txn-lock-lab",
        description="Local SQLite transaction isolation and row-lock laboratory",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="execute one edit script")
    _add_execution_args(run_parser)
    run_parser.add_argument("--snapshot", help="save result under this snapshot name")
    run_parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    run_parser.add_argument("--report", help="write the full result JSON to this path")
    run_parser.add_argument(
        "--json",
        action="store_true",
        help="print the full execution result JSON instead of a summary",
    )
    run_parser.set_defaults(handler=cmd_run)

    validate_parser = subparsers.add_parser(
        "validate", help="validate a script without executing it"
    )
    validate_parser.add_argument("--script", required=True)
    validate_parser.add_argument("--init-sql")
    validate_parser.add_argument("--db", default=":memory:")
    validate_parser.set_defaults(handler=cmd_validate)

    demo_parser = subparsers.add_parser(
        "demo", help="run all deterministic acceptance scenarios in fixed order"
    )
    demo_parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    demo_parser.add_argument(
        "--report",
        default=str(PROJECT_ROOT / "reports" / "demo_report.json"),
    )
    demo_parser.add_argument(
        "--quiet-events",
        action="store_true",
        help="print only scenario summaries, not every event",
    )
    demo_parser.set_defaults(handler=cmd_demo)

    restore_parser = subparsers.add_parser(
        "restore", help="fully replace the database state from a snapshot"
    )
    restore_parser.add_argument("--snapshot", required=True)
    restore_parser.add_argument(
        "--db", default=str(DEFAULT_RUNTIME_DIR / "lab.sqlite3")
    )
    restore_parser.set_defaults(handler=cmd_restore)

    replay_parser = subparsers.add_parser(
        "replay", help="replay a snapshot and compare outcomes/event sequences"
    )
    replay_parser.add_argument("--snapshot", required=True)
    replay_parser.add_argument("--budget", type=int)
    replay_parser.add_argument("--concurrency", type=int)
    replay_parser.add_argument("--db", help="optional replay SQLite file")
    replay_parser.add_argument(
        "--json", action="store_true", help="print the full replay report JSON"
    )
    replay_parser.add_argument(
        "--strict",
        action="store_true",
        help="return a non-zero exit code for E_REPLAY",
    )
    replay_parser.set_defaults(handler=cmd_replay)

    snapshots_parser = subparsers.add_parser(
        "snapshots", help="list named snapshots in a directory"
    )
    snapshots_parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    snapshots_parser.set_defaults(handler=cmd_snapshots)

    gui_parser = subparsers.add_parser("gui", help="launch the Tkinter desktop UI")
    gui_parser.add_argument("--script", default=str(PROJECT_ROOT / "samples" / "normal_parallel.json"))
    gui_parser.add_argument("--init-sql", default=str(DEFAULT_INIT_SQL))
    gui_parser.add_argument("--db", default=str(DEFAULT_RUNTIME_DIR / "lab.sqlite3"))
    gui_parser.add_argument("--snapshot-dir", default=str(DEFAULT_SNAPSHOT_DIR))
    gui_parser.set_defaults(handler=cmd_gui)
    return parser


def _add_execution_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--script", required=True)
    parser.add_argument(
        "--init-sql",
        help="deterministic initial-row SQL; omit to use existing DB rows",
    )
    parser.add_argument("--db", default=str(DEFAULT_RUNTIME_DIR / "lab.sqlite3"))
    parser.add_argument("--budget", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--tick", type=int, default=10)


def _config_from_args(args: argparse.Namespace) -> SchedulerConfig:
    try:
        return SchedulerConfig(
            lock_wait_budget_ms=args.budget,
            max_concurrency=args.concurrency,
            tick_ms=args.tick,
        )
    except ValueError as exc:
        raise LabError(E_SCRIPT, f"invalid scheduler configuration: {exc}") from exc


def cmd_run(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    result = execute_script_file(
        args.script,
        db_path=args.db,
        init_sql_path=args.init_sql,
        config=config,
    )
    snapshot_path = None
    if args.snapshot:
        snapshot_path = save_snapshot(
            args.snapshot_dir,
            args.snapshot,
            result,
            initial_sql=_read_initial_sql_if_available(args.init_sql),
        )
    if args.report:
        atomic_write_json(Path(args.report), result)
    if args.json:
        payload: dict[str, Any] = {"status": "ok", "result": result}
        if snapshot_path is not None:
            payload["snapshot_path"] = str(snapshot_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_run_summary(result))
        if snapshot_path is not None:
            print(f"SNAPSHOT {snapshot_path}")
        if args.report:
            print(f"REPORT {args.report}")
    return 0 if result.get("code") in (None, "OK") else 1


def cmd_validate(args: argparse.Namespace) -> int:
    report = validate_script_file(
        args.script,
        db_path=args.db,
        init_sql_path=args.init_sql,
    )
    print(json.dumps({"status": "ok", **report}, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    with SQLiteStore(args.db) as store:
        report = restore_snapshot(args.snapshot, store)
    print(json.dumps({"status": "ok", **report}, ensure_ascii=False, sort_keys=True))
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    report = replay_snapshot(
        args.snapshot,
        lock_wait_budget_ms=args.budget,
        max_concurrency=args.concurrency,
        db_path=args.db,
    )
    if args.json:
        print(json.dumps({"status": "ok", **report}, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(format_replay_report(report))
    if args.strict and report["code"] == "E_REPLAY":
        return 1
    return 0


def cmd_snapshots(args: argparse.Namespace) -> int:
    snapshots = list_snapshots(args.snapshot_dir)
    print(json.dumps(snapshots, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    try:
        from .gui import launch_gui
    except ImportError as exc:
        raise LabError(
            E_STORAGE,
            "Tkinter is unavailable; install the Python tcl/tk component",
            details={"cause": str(exc)},
        ) from exc
    try:
        launch_gui(
            script_path=args.script,
            init_sql_path=args.init_sql,
            db_path=args.db,
            snapshot_dir=args.snapshot_dir,
        )
    except Exception as exc:
        raise LabError(
            E_STORAGE,
            f"cannot start Tkinter interface: {exc}",
        ) from exc
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    scenarios = [
        {
            "key": "normal",
            "script": PROJECT_ROOT / "samples" / "normal_parallel.json",
            "budget": 500,
            "concurrency": 2,
        },
        {
            "key": "timeout",
            "script": PROJECT_ROOT / "samples" / "lock_timeout.json",
            "budget": 40,
            "concurrency": 2,
        },
        {
            "key": "deadlock",
            "script": PROJECT_ROOT / "samples" / "deadlock.json",
            "budget": 500,
            "concurrency": 2,
        },
        {
            "key": "isolation-rc",
            "script": PROJECT_ROOT / "samples" / "isolation_rc.json",
            "budget": 500,
            "concurrency": 2,
        },
        {
            "key": "isolation-rr",
            "script": PROJECT_ROOT / "samples" / "isolation_rr.json",
            "budget": 500,
            "concurrency": 2,
        },
    ]
    run_dir = DEFAULT_RUNTIME_DIR / "demo"
    run_dir.mkdir(parents=True, exist_ok=True)
    snapshot_dir = Path(args.snapshot_dir)
    scenario_reports: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    print("SQLITE TRANSACTION LOCK LAB DEMO")
    print("ORDER normal -> timeout -> deadlock -> isolation-rc -> isolation-rr")
    for index, scenario in enumerate(scenarios, start=1):
        key = scenario["key"]
        print(f"\n=== [{index}/{len(scenarios)}] {key} ===")
        db_path = run_dir / f"{key}.sqlite3"
        result = execute_script_file(
            scenario["script"],
            db_path=db_path,
            init_sql_path=DEFAULT_INIT_SQL,
            config=SchedulerConfig(
                lock_wait_budget_ms=scenario["budget"],
                max_concurrency=scenario["concurrency"],
                tick_ms=10,
            ),
        )
        snapshot_path = save_snapshot(
            snapshot_dir,
            f"demo-{key}",
            result,
            initial_sql=DEFAULT_INIT_SQL.read_text(encoding="utf-8"),
        )
        scenario_reports.append(
            {
                "key": key,
                "script": str(scenario["script"]),
                "snapshot": str(snapshot_path),
                "result": result,
            }
        )
        print(format_run_summary(result, include_events=not args.quiet_events))
        print(f"SNAPSHOT {snapshot_path}")

    normal = _scenario_by_key(scenario_reports, "normal")["result"]
    timeout = _scenario_by_key(scenario_reports, "timeout")["result"]
    deadlock = _scenario_by_key(scenario_reports, "deadlock")["result"]
    isolation_rc = _scenario_by_key(scenario_reports, "isolation-rc")["result"]
    isolation_rr = _scenario_by_key(scenario_reports, "isolation-rr")["result"]

    _check(
        checks,
        "normal_all_commit",
        normal["summary"]["committed"] == 3
        and normal["summary"]["rolled_back"] == 0
        and normal["isolation_ok"],
        normal["summary"],
    )
    timeout_session = _session_by_id(timeout, "t2")
    _check(
        checks,
        "timeout_rolls_back_with_code",
        timeout_session["outcome"] == "rollback"
        and timeout_session["code"] == "E_LOCKTIMEOUT"
        and _session_by_id(timeout, "t1")["outcome"] == "commit",
        {
            "t1": _session_by_id(timeout, "t1")["outcome"],
            "t2": timeout_session["outcome"],
            "t2_code": timeout_session["code"],
        },
    )
    _check(
        checks,
        "deadlock_cycle_and_victim",
        bool(deadlock["deadlocks"])
        and deadlock["deadlocks"][0]["cycle_members"] == ["d1", "d2"]
        and deadlock["deadlocks"][0]["victim_session_id"] == "d1"
        and _session_by_id(deadlock, "d1")["code"] == "E_DEADLOCK",
        deadlock["deadlocks"],
    )
    rc_reader = _session_by_id(isolation_rc, "rc-reader")
    rr_reader = _session_by_id(isolation_rr, "rr-reader")
    rc_balances = [item["payload"]["balance"] for item in rc_reader["reads"]]
    rr_balances = [item["payload"]["balance"] for item in rr_reader["reads"]]
    _check(
        checks,
        "read_committed_sees_latest_committed",
        rc_balances == [100, 70],
        {"read_balances": rc_balances},
    )
    _check(
        checks,
        "repeatable_read_stable",
        rr_balances == [100, 100],
        {"read_balances": rr_balances},
    )

    normal_snapshot = _scenario_by_key(scenario_reports, "normal")["snapshot"]
    timeout_snapshot = _scenario_by_key(scenario_reports, "timeout")["snapshot"]
    deadlock_snapshot = _scenario_by_key(scenario_reports, "deadlock")["snapshot"]
    normal_replay = replay_snapshot(normal_snapshot)
    deadlock_replay = replay_snapshot(deadlock_snapshot)
    timeout_changed_replay = replay_snapshot(
        timeout_snapshot,
        lock_wait_budget_ms=500,
    )
    _check(
        checks,
        "normal_replay_identical",
        normal_replay["code"] == "OK" and not normal_replay["diverged_sessions"],
        normal_replay["event_comparison"],
    )
    _check(
        checks,
        "deadlock_replay_identical_victim",
        deadlock_replay["code"] == "OK"
        and not deadlock_replay["diverged_sessions"],
        deadlock_replay["outcome_comparison"],
    )
    _check(
        checks,
        "changed_budget_reports_e_replay",
        timeout_changed_replay["code"] == "E_REPLAY"
        and "t2" in timeout_changed_replay["diverged_sessions"],
        {
            "code": timeout_changed_replay["code"],
            "diverged_sessions": timeout_changed_replay["diverged_sessions"],
            "parameter_mismatch": timeout_changed_replay["parameter_mismatch"],
        },
    )

    print("\n=== REPLAY VERIFICATION ===")
    print(format_replay_report(normal_replay))
    print(format_replay_report(deadlock_replay))
    print(format_replay_report(timeout_changed_replay))
    print("\n=== ACCEPTANCE CHECKS ===")
    for check in checks:
        print(f"{'PASS' if check['ok'] else 'FAIL'} {check['name']}")

    report_payload = {
        "format_version": 1,
        "scenario_order": [item["key"] for item in scenario_reports],
        "scenarios": scenario_reports,
        "replay_checks": {
            "normal_same_parameters": normal_replay,
            "deadlock_same_parameters": deadlock_replay,
            "timeout_changed_budget": timeout_changed_replay,
        },
        "checks": checks,
        "all_checks_passed": all(check["ok"] for check in checks),
    }
    report_path = Path(args.report)
    atomic_write_json(report_path, report_payload)
    print(f"\nDEMO_REPORT {report_path}")
    return 0 if report_payload["all_checks_passed"] else 1


def _check(
    checks: list[dict[str, Any]],
    name: str,
    ok: bool,
    evidence: Any,
) -> None:
    checks.append({"name": name, "ok": bool(ok), "evidence": evidence})


def _scenario_by_key(scenarios: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return next(item for item in scenarios if item["key"] == key)


def _session_by_id(result: dict[str, Any], session_id: str) -> dict[str, Any]:
    return next(
        session for session in result["sessions"] if session["session_id"] == session_id
    )


def _read_initial_sql_if_available(path: str | None) -> str | None:
    if not path:
        return None
    return Path(path).read_text(encoding="utf-8")
