"""Command line entrypoints for data generation, execution and diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cache import cache_diagnose
from .engine import execute
from .generate_data import generate_dataset
from .injection import inject_diagnose
from .selfcheck import equivalence_report
from .util import atomic_write_json


def _json_argument(value: str | None) -> Any:
    if value is None:
        return None
    path = Path(value)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value)


def _write_or_print(value: Any, output: str | None) -> None:
    if output:
        atomic_write_json(output, value)
        print(json.dumps({"written": output, "ok": value.get("ok")}, sort_keys=True))
    else:
        print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", default="data/manifest.jsonl")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--cache-dir", default="cache")
    parser.add_argument("--snapshot-dir", default="snapshots")
    parser.add_argument("--limits-json", default=None, help="JSON object or path")
    parser.add_argument("--json-out", default=None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m transformer_fusion")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate deterministic JSONL + NPZ samples")
    generate.add_argument("--data-dir", default="data")
    generate.add_argument("--manifest", default=None)
    generate.add_argument("--seed", type=int, default=20261124)
    generate.add_argument("--case-count", type=int, default=None)
    generate.add_argument("--json-out", default=None)

    run = sub.add_parser("execute", help="execute a manifest")
    _add_common(run)
    run.add_argument("--abs-tol", type=float, default=None)
    run.add_argument("--rel-tol", type=float, default=None)
    run.add_argument("--no-cache", action="store_true")
    run.add_argument("--no-selfcheck", action="store_true")
    run.add_argument("--repair-corrupt-snapshots", action="store_true")
    run.add_argument("--case-ids", default=None, help="comma-separated subset")

    inject = sub.add_parser("inject", help="run input/parameter-driven injections")
    _add_common(inject)
    inject.add_argument("--injections-json", default=None, help="JSON array or path")

    cache = sub.add_parser("cache", help="diagnose cache reuse and partial invalidation")
    _add_common(cache)
    cache.add_argument("--changed-manifest", default=None)
    cache.add_argument("--changes-json", default=None, help="case_id -> update object")
    cache.add_argument("--no-execute-changed", action="store_true")
    cache.add_argument("--abs-tol", type=float, default=None)
    cache.add_argument("--rel-tol", type=float, default=None)

    equiv = sub.add_parser("equivalence", help="run the public equivalence report")
    _add_common(equiv)
    equiv.add_argument("--abs-tol", type=float, default=None)
    equiv.add_argument("--rel-tol", type=float, default=None)

    demo = sub.add_parser("demo", help="run the complete local acceptance demo")
    demo.add_argument("--output-dir", default="artifacts")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "generate":
        metadata = generate_dataset(
            args.data_dir,
            manifest_path=args.manifest,
            seed=args.seed,
            case_count=args.case_count,
        )
        _write_or_print(metadata, args.json_out)
        return 0
    if args.command == "demo":
        from .demo import run_demo

        result = run_demo(args.output_dir)
        print(json.dumps(result["summary"], indent=2, sort_keys=True))
        return 0 if result["ok"] else 1

    limits = _json_argument(args.limits_json)
    if args.command == "execute":
        case_ids = args.case_ids.split(",") if args.case_ids else None
        report = execute(
            args.manifest,
            data_dir=args.data_dir,
            cache_dir=args.cache_dir,
            snapshot_dir=args.snapshot_dir,
            limits=limits,
            abs_tol=args.abs_tol,
            rel_tol=args.rel_tol,
            use_cache=not args.no_cache,
            repair_corrupt_snapshots=args.repair_corrupt_snapshots,
            case_ids=case_ids,
            run_selfcheck=not args.no_selfcheck,
        )
        _write_or_print(report.to_dict(), args.json_out)
        return 0 if report.ok else 1
    if args.command == "inject":
        specs = _json_argument(args.injections_json)
        report = inject_diagnose(
            args.manifest,
            injections=specs,
            data_dir=args.data_dir,
            cache_dir=args.cache_dir,
            snapshot_dir=args.snapshot_dir,
            limits=limits,
        )
        _write_or_print(report, args.json_out)
        return 0 if report.get("ok") else 1
    if args.command == "cache":
        changes = _json_argument(args.changes_json)
        report = cache_diagnose(
            args.manifest,
            args.changed_manifest,
            changes=changes,
            data_dir=args.data_dir,
            cache_dir=args.cache_dir,
            snapshot_dir=args.snapshot_dir,
            limits=limits,
            execute_changed=not args.no_execute_changed,
            abs_tol=args.abs_tol,
            rel_tol=args.rel_tol,
        )
        _write_or_print(report, args.json_out)
        return 0 if report.get("ok") else 1
    if args.command == "equivalence":
        report = equivalence_report(
            args.manifest,
            data_dir=args.data_dir,
            cache_dir=args.cache_dir,
            snapshot_dir=args.snapshot_dir,
            limits=limits,
            abs_tol=args.abs_tol,
            rel_tol=args.rel_tol,
        )
        _write_or_print(report, args.json_out)
        return 0 if report.get("ok") else 1
    parser = build_parser()
    parser.print_help()
    return 2
