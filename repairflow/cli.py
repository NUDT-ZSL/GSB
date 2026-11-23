"""Offline command-line entry points."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import normalize_config
from .demo import run_demo
from .engine import RepairEngine
from .samples import load_sample


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m repairflow.cli")
    parser.add_argument(
        "--data", default=argparse.SUPPRESS, help="sample data directory"
    )
    parser.add_argument(
        "--workdir",
        default=argparse.SUPPRESS,
        help="runtime directory for cache and demo output",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo_parser = sub.add_parser("demo", help="run deterministic incremental/full acceptance demo")
    demo_parser.add_argument("--data", default=argparse.SUPPRESS)
    demo_parser.add_argument("--workdir", default=argparse.SUPPRESS)
    demo_parser.add_argument("--keep-cache", action="store_true", help="reuse existing demo cache")
    demo_parser.add_argument("--json", action="store_true", help="print the complete report")

    check_parser = sub.add_parser("check", help="run the demo and fail if acceptance checks fail")
    check_parser.add_argument("--data", default=argparse.SUPPRESS)
    check_parser.add_argument("--workdir", default=argparse.SUPPRESS)

    inspect_parser = sub.add_parser("inspect", help="query cache keys and artifact hashes")
    inspect_parser.add_argument("--data", default=argparse.SUPPRESS)
    inspect_parser.add_argument("--workdir", default=argparse.SUPPRESS)
    inspect_parser.add_argument("--region", type=int, action="append", help="region id; repeatable")
    inspect_parser.add_argument("--json", action="store_true", help="print JSON")

    gui_parser = sub.add_parser("gui", help="start the Tkinter desktop application")
    gui_parser.add_argument("--data", default=argparse.SUPPRESS)
    gui_parser.add_argument("--workdir", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    data_dir = getattr(args, "data", "samples/data")
    workdir = getattr(args, "workdir", "artifacts/runtime")

    if args.command == "demo":
        report = run_demo(workdir, data_dir, fresh_cache=not args.keep_cache)
        if args.json:
            print(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False))
        else:
            _print_demo_summary(report)
        return 0 if report["all_passed"] else 2

    if args.command == "check":
        report = run_demo(workdir, data_dir, fresh_cache=True)
        _print_demo_summary(report)
        return 0 if report["all_passed"] else 2

    if args.command == "inspect":
        base, masks, config = load_sample(data_dir)
        cache_dir = Path(workdir) / "inspect_cache"
        engine = RepairEngine(base=base, cache_dir=cache_dir, config=config)
        result = engine.submit(masks)
        region_ids = args.region or list(result.mask_state.region_ids)
        payload = {str(region_id): engine.inspect_region(region_id) for region_id in region_ids}
        if args.json:
            print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))
        else:
            for region_id, item in payload.items():
                print(f"region {region_id}: {item['region']['pixels']} px, {item['region']['bbox']}")
                for node, node_item in item["nodes"].items():
                    print(
                        f"  {node:20s} {node_item['last_status']:10s}"
                        f" key={node_item['key'][:16]}... product={node_item['artifact_hash'][:16]}..."
                    )
        return 0

    if args.command == "gui":
        try:
            from .app import run_app
            run_app(data_dir, Path(workdir) / "gui_cache")
        except Exception as exc:
            print(f"Tkinter application is unavailable: {exc}", file=sys.stderr)
            return 2
        return 0

    return 1


def _print_demo_summary(report: dict[str, Any]) -> None:
    print("RepairFlow deterministic incremental acceptance")
    print(f"  sample base hash: {report['sample']['base_hash']}")
    print("  acceptance report: <workdir>/acceptance_report.json")
    for step in report["steps"]:
        comparison = step["comparison"]
        print(
            f"  {step['name']:22s} recomputed={step['recomputed_regions']!s:12s}"
            f" reused={step['reused_regions']!s:12s}"
            f" byte_equal={comparison['byte_identical']}"
            f" artifacts_equal={comparison['artifact_hashes_equal']}"
        )
    print("  guardrails:")
    for key, value in report["acceptance"].items():
        print(f"    {key:42s} {value}")
    print(f"  all passed: {report['all_passed']}")


if __name__ == "__main__":
    raise SystemExit(main())
