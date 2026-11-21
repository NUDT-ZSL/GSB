from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .errors import WorkbenchError
from .evaluator import run_evaluation
from .probe import HttpProbe, SimulatedProbe
from .report import (
    build_report,
    render_text_report,
    report_json,
    status_document,
    verify_run,
    write_report,
)
from .scenario import Scenario, load_scenario
from .storage import Store


def _resolve_run_id(
    store: Store,
    requested: str | None,
    *,
    allow_empty: bool = False,
) -> str:
    if not store.exists:
        raise WorkbenchError("E_DB_MISSING", f"database not found: {store.path}")
    if requested:
        return requested
    run_ids = store.run_ids()
    if not run_ids:
        raise WorkbenchError("E_RUN_NOT_FOUND", "database contains no runs")
    if len(run_ids) > 1:
        raise WorkbenchError(
            "E_RUN_AMBIGUOUS",
            "database contains multiple runs; pass --run-id",
            {"run_ids": run_ids},
        )
    return run_ids[0]


def _load_run_scenario(store: Store, run_id: str) -> Scenario:
    return store.load_scenario(run_id)


def _default_report_path(db_path: str, run_id: str) -> Path:
    return Path(db_path).with_name(f"{Path(db_path).stem}.{run_id}.report.json")


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _add_common_run_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", type=int, help="override scenario seed")
    parser.add_argument("--run-id", help="stable run identifier")
    parser.add_argument(
        "--transport",
        choices=("http", "simulated"),
        default="http",
        help="http targets the local service; simulated is an offline diagnostic mode",
    )
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--report", help="write stable JSON report to this path")
    parser.add_argument("--no-wait", action="store_true", help="do not wait for peer processes")
    parser.add_argument(
        "--crash-after-commits",
        type=int,
        help="test hook: hard-exit after N committed windows",
    )
    parser.add_argument(
        "--crash-after-claims",
        type=int,
        help="test hook: hard-exit after N claimed windows (leaves half-committed state)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capbench",
        description="Deterministic local multi-tenant capacity workbench",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a new evaluation or continue identical run")
    run.add_argument("--scenario", required=True)
    run.add_argument("--db", required=True)
    _add_common_run_options(run)

    resume = sub.add_parser("resume", help="recover incomplete units and continue")
    resume.add_argument("--scenario", help="optional scenario path; defaults to persisted copy")
    resume.add_argument("--db", required=True)
    _add_common_run_options(resume)

    status = sub.add_parser("status", help="show unit progress and logical clock")
    status.add_argument("--db", required=True)
    status.add_argument("--run-id")
    status.add_argument("--format", choices=("json", "text"), default="json")

    report = sub.add_parser("report", help="emit a stable diagnostic report")
    report.add_argument("--db", required=True)
    report.add_argument("--run-id")
    report.add_argument("--output")
    report.add_argument("--format", choices=("json", "text"), default="json")

    verify = sub.add_parser("verify", help="verify persisted state and report consistency")
    verify.add_argument("--db", required=True)
    verify.add_argument("--run-id")
    verify.add_argument("--report")
    return parser


def _run_command(args: argparse.Namespace, *, resume: bool) -> int:
    store = Store(args.db)
    if resume:
        if not store.exists:
            raise WorkbenchError("E_DB_MISSING", f"database not found: {store.path}")
        run_id = _resolve_run_id(store, args.run_id)
        if args.scenario:
            scenario = load_scenario(args.scenario)
            seed = args.seed if args.seed is not None else int(store.meta(run_id)["seed"])
        else:
            scenario = _load_run_scenario(store, run_id)
            seed = args.seed if args.seed is not None else int(store.meta(run_id)["seed"])
        store.initialize(scenario, run_id=run_id, seed=seed)
    else:
        scenario = load_scenario(args.scenario)
        seed = args.seed if args.seed is not None else scenario.seed
        run_id = args.run_id or scenario.default_run_id(seed)
        store.initialize(scenario, run_id=run_id, seed=seed)

    probe = HttpProbe() if args.transport == "http" else SimulatedProbe()
    result = run_evaluation(
        store=store,
        scenario=scenario,
        run_id=run_id,
        seed=seed,
        probe=probe,
        workers=args.workers,
        crash_after_commits=args.crash_after_commits,
        crash_after_claims=args.crash_after_claims,
        wait_for_peers=not args.no_wait,
    )
    report = build_report(store, run_id)
    report_path = (
        Path(args.report)
        if args.report
        else _default_report_path(args.db, run_id)
    )
    write_report(report_path, report)
    _print_json(
        {
            **result,
            "report": str(report_path),
            "budget_used_ms": report["run"]["budget_used_ms"],
            "logical_clock_ms": report["run"]["logical_clock_ms"],
        }
    )
    return (
        0
        if result["status"] in ("completed", "completed_budget_exhausted")
        else 1
    )


def _status_command(args: argparse.Namespace) -> int:
    store = Store(args.db)
    run_id = _resolve_run_id(store, args.run_id)
    document = status_document(store, run_id)
    if args.format == "json":
        _print_json(document)
    else:
        print(
            f"run={document['run_id']} status={document['status']} "
            f"clock={document['logical_clock_ms']}ms "
            f"budget={document['budget_used_ms']}/{document['global_time_budget_ms']}ms"
        )
        for unit in document["units"]:
            print(
                f"{unit['tenant']} g{unit['gradient_index']} w{unit['window_index']} "
                f"{unit['state']} attempts={unit['attempt_count']} "
                f"seq={unit['request_seq_range'][0]}..{unit['request_seq_range'][1]}"
            )
    return 0


def _report_command(args: argparse.Namespace) -> int:
    store = Store(args.db)
    run_id = _resolve_run_id(store, args.run_id)
    report = build_report(store, run_id)
    if args.output:
        write_report(args.output, report)
    if args.format == "text":
        sys.stdout.write(render_text_report(report))
    else:
        sys.stdout.write(report_json(report))
    return 0


def _verify_command(args: argparse.Namespace) -> int:
    store = Store(args.db)
    run_id = _resolve_run_id(store, args.run_id)
    result = verify_run(store, run_id, report_path=args.report)
    payload = {
        "ok": result["ok"],
        "run_id": result["run_id"],
        "checks": result["checks"],
        "errors": result["errors"],
    }
    _print_json(payload)
    return 0 if result["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _run_command(args, resume=False)
        if args.command == "resume":
            return _run_command(args, resume=True)
        if args.command == "status":
            return _status_command(args)
        if args.command == "report":
            return _report_command(args)
        if args.command == "verify":
            return _verify_command(args)
        parser.error("unknown command")
        return 2
    except WorkbenchError as exc:
        print(
            json.dumps(exc.as_dict(), ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print(
            json.dumps(
                {
                    "error": {
                        "code": "E_INTERRUPTED",
                        "message": "interrupted; use resume to continue",
                    }
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

