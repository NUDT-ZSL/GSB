from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .analysis import build_report
from .engine import Engine
from .errors import BizError
from .scene import load_scene
from .store import Store
from .util import pretty_json, write_text_atomic
from .verification import verify_database


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            return _run(args)
        if args.command == "resume":
            return _resume(args)
        if args.command == "status":
            return _status(args)
        if args.command == "report":
            return _report(args)
        if args.command == "verify":
            return _verify(args)
        parser.print_help()
        return 2
    except BizError as exc:
        print(pretty_json(exc.as_dict()), file=sys.stderr)
        return int(exc.exit_code or 4)
    except KeyboardInterrupt:
        print(pretty_json({"ok": False, "error": {"code": "E_INTERNAL", "message": "interrupted"}}), file=sys.stderr)
        return 4
    except Exception as exc:
        print(
            pretty_json(
                {
                    "ok": False,
                    "error": {
                        "code": "E_INTERNAL",
                        "message": str(exc),
                        "details": {"exception_type": type(exc).__name__},
                    },
                }
            ),
            file=sys.stderr,
        )
        return 4


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="capacity-engine",
        description="Deterministic multi-tenant capacity evaluation engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="initialize a run and process all claimable units")
    run.add_argument("--scene", required=True, help="scene JSON file")
    run.add_argument("--db", required=True, help="SQLite state file")
    run.add_argument("--results", help="result directory for report.json")
    run.add_argument(
        "--crash-after-windows",
        type=int,
        default=None,
        help="test hook: force process exit after N newly committed windows",
    )

    resume = subparsers.add_parser("resume", help="resume an existing run")
    resume.add_argument("--db", required=True, help="SQLite state file")
    resume.add_argument("--scene", help="scene JSON file; defaults to the stored snapshot")
    resume.add_argument("--results", help="result directory for report.json")
    resume.add_argument(
        "--crash-after-windows",
        type=int,
        default=None,
        help="test hook: force process exit after N newly committed windows",
    )

    status = subparsers.add_parser("status", help="show durable run status")
    status.add_argument("--db", required=True, help="SQLite state file")
    status.add_argument("--format", choices=("json", "text"), default="json")

    report = subparsers.add_parser("report", help="print a stable capacity report")
    report.add_argument("--db", required=True, help="SQLite state file")
    report.add_argument("--format", choices=("json", "text"), default="json")
    report.add_argument("--output", help="write report to this file")

    verify = subparsers.add_parser("verify", help="recompute and verify committed windows and conclusions")
    verify.add_argument("--db", required=True, help="SQLite state file")
    verify.add_argument("--format", choices=("json", "text"), default="json")
    return parser


def _run(args: argparse.Namespace) -> int:
    scene = load_scene(Path(args.scene))
    engine = Engine(
        scene,
        Path(args.db),
        crash_after_committed_windows=args.crash_after_windows,
    )
    output = engine.run(resume_only=False)
    report = output.pop("report")
    output["analysis_complete"] = report["analysis_complete"]
    output["precision_sensitive_count"] = len(report["precision_sensitive"])
    if args.results:
        _write_results(Path(args.results), report)
        output["report_path"] = str(Path(args.results) / "report.json")
    print(pretty_json(output))
    return 0


def _resume(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    if args.scene:
        scene = load_scene(Path(args.scene))
    else:
        scene = Store(db_path).load_scene()
    engine = Engine(
        scene,
        db_path,
        crash_after_committed_windows=args.crash_after_windows,
    )
    output = engine.run(resume_only=True)
    report = output.pop("report")
    output["analysis_complete"] = report["analysis_complete"]
    output["precision_sensitive_count"] = len(report["precision_sensitive"])
    if args.results:
        _write_results(Path(args.results), report)
        output["report_path"] = str(Path(args.results) / "report.json")
    print(pretty_json(output))
    return 0


def _status(args: argparse.Namespace) -> int:
    snapshot = Store(Path(args.db)).status_snapshot()
    if args.format == "json":
        print(pretty_json({"ok": True, **snapshot}))
    else:
        print(_status_text(snapshot))
    return 0


def _report(args: argparse.Namespace) -> int:
    report = build_report(Store(Path(args.db)))
    text = pretty_json(report) if args.format == "json" else _report_text(report)
    if args.output:
        write_text_atomic(Path(args.output), text + ("\n" if not text.endswith("\n") else ""))
    print(text)
    return 0


def _verify(args: argparse.Namespace) -> int:
    result = verify_database(Store(Path(args.db)))
    if args.format == "json":
        print(pretty_json(result))
    else:
        print(_verify_text(result))
    return 0 if result["ok"] else 4


def _write_results(directory: Path, report: dict[str, Any]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    write_text_atomic(directory / "report.json", pretty_json(report) + "\n")


def _status_text(snapshot: dict[str, Any]) -> str:
    meta = snapshot["meta"]
    lines = [
        f"scene_id={meta.get('scene_id')}",
        f"scene_sha256={meta.get('scene_sha256')}",
        f"budget_used_ms={meta.get('budget_used_ms')}/{meta.get('global_budget_ms')}",
        f"logical_clock_ms={meta.get('logical_clock_ms')}",
        f"windows_committed={snapshot['windows_committed']}",
        f"active_tenant_leases={snapshot['active_tenant_leases']}",
        "unit_statuses="
        + ",".join(f"{key}:{value}" for key, value in sorted(snapshot["unit_status_counts"].items())),
    ]
    for unit in snapshot["units"]:
        lines.append(
            "unit "
            f"{unit['unit_key']} status={unit['status']} "
            f"concurrency={unit['concurrency']} windows={unit['window_count']} reason={unit.get('reason')}"
        )
    for error in snapshot["request_error_counts"]:
        lines.append(f"request_error {error['code']}={error['n']}")
    return "\n".join(lines)


def _report_text(report: dict[str, Any]) -> str:
    lines = [
        f"scene_id={report['scene']['scene_id']}",
        f"scene_sha256={report['scene']['sha256']}",
        f"report_sha256={report['report_sha256']}",
        f"analysis_complete={str(report['analysis_complete']).lower()}",
        f"budget_used_ms={report['budget']['budget_used_ms']}/{report['budget']['global_logical_time_ms']}",
    ]
    for conclusion in report["capacity_conclusions"]:
        lines.append(
            "capacity "
            f"batch={conclusion['batch_id']} tenant={conclusion['tenant']} "
            f"status={conclusion['status']} capacity={conclusion['capacity_concurrency']} "
            f"inflection={conclusion['inflection_concurrency']}"
        )
    for gradient in report["gradients"]:
        lines.append(
            "gradient "
            f"batch={gradient['batch_id']} tenant={gradient['tenant']} "
            f"index={gradient['gradient_index']} concurrency={gradient['concurrency']} "
            f"status={gradient['status']} high_scale={gradient['high_scale_status']}"
        )
    for mark in report["precision_sensitive"]:
        lines.append(
            "sensitive "
            f"kind={mark['kind']} metric={mark['metric']} "
            f"left={mark['left_unit_key']} right={mark['right_unit_key']} "
            f"difference={mark['difference']} tolerance={mark['tolerance']} "
            f"higher_scale_flips={str(mark['higher_scale_flips']).lower()}"
        )
    for abandoned in report["abandoned_units"]:
        lines.append(
            "abandoned "
            f"unit={abandoned['unit_key']} status={abandoned['status']} reason={abandoned['reason']}"
        )
    return "\n".join(lines)


def _verify_text(result: dict[str, Any]) -> str:
    lines = [
        f"ok={str(result['ok']).lower()}",
        f"scene_id={result['scene_id']}",
        f"report_sha256={result['report_sha256']}",
        f"checks_total={result['checks_total']}",
        f"checks_failed={result['checks_failed']}",
    ]
    for issue in result["issues"]:
        lines.append(
            f"issue name={issue.get('name')} code={issue.get('code')} "
            f"details={json.dumps(issue, ensure_ascii=False, sort_keys=True)}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())

