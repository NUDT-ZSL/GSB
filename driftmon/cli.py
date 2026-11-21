"""Command line interface and reproducible demo."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_bytes
from .errors import AppError
from .service import MonitoringService
from .web import create_app


def _print(value: Any) -> None:
    if isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(value)


def _run_summary(result) -> dict[str, Any]:
    public = result.public()
    return {
        "cursor": public["cursor"],
        "config_version": public["config_version"],
        "window": {
            "monitor_start": public["window_report"].get("window_start_batch"),
            "monitor_end": public["window_report"].get("window_end_batch"),
            "removed_batches": public["window_report"].get("removed_batch_count"),
            "removed_samples": public["window_report"].get("removed_sample_count"),
        },
        "affected": public["incremental"]["affected_products"],
        "recomputed": public["incremental"]["recomputed_products"],
        "reused": public["incremental"]["reused_products"],
        "coverage": public["incremental"]["coverage"],
        "results": [
            {
                "feature": item["feature"],
                "drift": item["drift"],
                "threshold": item["threshold"],
                "detected": item["detected"],
                "severity": item["severity"],
                "cache": item["product_cache_status"],
            }
            for item in public["results"]
        ],
        "alert": public["alert"],
        "snapshot_sha256": public["snapshot_sha256"],
    }


def cmd_demo(args: argparse.Namespace) -> int:
    root = Path(args.root or "runtime/demo")
    if args.fresh and root.exists():
        shutil.rmtree(root)
    service = MonitoringService(root, initial_cursor=2)
    initial = service.reset(clear=True)
    steps: list[dict[str, Any]] = [
        {
            "step": "initial_build",
            "summary": _run_summary_from_snapshot(initial),
        }
    ]

    injected = service.inject_drift_and_advance()
    steps.append({"step": "inject_and_advance", "summary": _run_summary(injected)})

    latest_alert = service.current_alert()
    if latest_alert is None:
        raise AppError("E_DEMO_INVALID", "注入漂移后未生成告警")
    trace = service.trace_alert(latest_alert["alert_id"])
    steps.append(
        {
            "step": "trace_alert",
            "alert_id": trace["alert_id"],
            "matches": trace["matches"],
            "checksum": trace["replayed_checksum"],
        }
    )

    advanced = service.advance()
    steps.append({"step": "slide_reference_window", "summary": _run_summary(advanced)})

    windowed = service.set_window(2)
    steps.append({"step": "window_size_2", "summary": _run_summary(windowed)})

    stat_version = service.bump_stat_version("transaction_amount")
    steps.append({"step": "stat_version_bump", "summary": _run_summary(stat_version)})

    toggled = service.toggle_feature("region")
    steps.append({"step": "disable_region", "summary": _run_summary(toggled)})

    redefined = service.redefine_feature("device_score", "category")
    steps.append({"step": "redefine_device_score", "summary": _run_summary(redefined)})

    selfcheck = service.selfcheck()
    steps.append({"step": "selfcheck", "result": selfcheck})
    snapshot = service.snapshot()
    report = {
        "demo": "deterministic-feature-drift-monitoring",
        "runtime_root": str(root),
        "initial": {
            "seed": snapshot["state"]["seed"],
            "batch_size": snapshot["state"]["batch_size"],
            "n_batches": snapshot["state"]["n_batches"],
        },
        "steps": steps,
        "alerts": snapshot["alerts"],
        "cache_entries": snapshot["cache"]["entries"],
        "recent_errors": snapshot["recent_errors"],
        "selfcheck_ok": selfcheck["ok"],
    }
    output_path = Path(args.output or "artifacts/demo_report.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(canonical_bytes(report) + b"\n")
    _print(report)
    print(f"\nDemo report written to {output_path}")
    return 0 if selfcheck["ok"] else 1


def _run_summary_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    last = snapshot.get("last_run") or {}
    return {
        "cursor": snapshot["state"]["cursor"],
        "window": last.get("window_report"),
        "affected": last.get("incremental", {}).get("affected_products", []),
        "recomputed": last.get("incremental", {}).get("recomputed_products", []),
        "reused": last.get("incremental", {}).get("reused_products", []),
        "coverage": last.get("incremental", {}).get("coverage"),
        "alert": last.get("alert"),
        "snapshot_sha256": last.get("snapshot_sha256"),
    }


def cmd_serve(args: argparse.Namespace) -> int:
    app = create_app(args.root, initial_cursor=args.initial_cursor)
    print(f"DriftMon dashboard: http://{args.host}:{args.port}")
    print(f"Runtime root: {Path(args.root).resolve()}")
    app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
    return 0


def cmd_selfcheck(args: argparse.Namespace) -> int:
    service = MonitoringService(args.root)
    check = service.selfcheck()
    _print(check)
    return 0 if check["ok"] else 1


def cmd_trace(args: argparse.Namespace) -> int:
    service = MonitoringService(args.root)
    _print(service.trace_alert(args.alert_id))
    return 0


def cmd_cache_explain(args: argparse.Namespace) -> int:
    service = MonitoringService(args.root)
    _print(service.explain_cache(args.key))
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    service = MonitoringService(args.root)
    _print(service.reset(clear=not args.keep))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="driftmon")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="run deterministic end-to-end demo")
    demo.add_argument("--root", default="runtime/demo")
    demo.add_argument("--output", default="artifacts/demo_report.json")
    demo.add_argument("--fresh", action="store_true", default=True)
    demo.set_defaults(handler=cmd_demo)

    serve = subparsers.add_parser("serve", help="start the local Flask dashboard")
    serve.add_argument("--root", default="runtime/app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--initial-cursor", type=int, default=2)
    serve.set_defaults(handler=cmd_serve)

    selfcheck = subparsers.add_parser("selfcheck", help="run consistency checks")
    selfcheck.add_argument("--root", default="runtime/app")
    selfcheck.set_defaults(handler=cmd_selfcheck)

    trace = subparsers.add_parser("trace", help="replay an alert from lineage")
    trace.add_argument("alert_id")
    trace.add_argument("--root", default="runtime/app")
    trace.set_defaults(handler=cmd_trace)

    explain = subparsers.add_parser("cache-explain", help="show cache key components")
    explain.add_argument("key")
    explain.add_argument("--root", default="runtime/app")
    explain.set_defaults(handler=cmd_cache_explain)

    reset = subparsers.add_parser("reset", help="reset the local demo state")
    reset.add_argument("--root", default="runtime/app")
    reset.add_argument("--keep", action="store_true", help="keep cache and alerts")
    reset.set_defaults(handler=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except AppError as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

