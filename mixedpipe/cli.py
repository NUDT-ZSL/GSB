"""Command-line entry points for sample, run, validate, demo and serve."""

from __future__ import annotations

import argparse
import json
import multiprocessing
import sys
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .models import load_config, load_manifest
from .pipeline import Pipeline
from .samples import ensure_demo, generate_dataset
from .validator import Validator
from .web import serve


def _print_json(value: Any) -> None:
    print(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
    )


def _paths(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    root = Path(args.root).resolve()
    manifest = Path(args.manifest).resolve() if args.manifest else root / "data" / "jobs.jsonl"
    config = Path(args.config).resolve() if args.config else root / "configs" / "demo.json"
    return root, manifest, config


def _run_demo(args: argparse.Namespace) -> int:
    root, manifest_path, config_path = _paths(args)
    ensure_demo(root)
    tasks = load_manifest(manifest_path)
    config = load_config(config_path)
    run = Pipeline(root).run(tasks, config)
    validation = Validator(root, manifest_path, config_path).validate()

    rejected = [
        {
            "job_id": decision["job_id"],
            "business_code": decision["business_code"],
            "reason": decision["reason"],
        }
        for decision in run["plan"]["decisions"]
        if decision["decision"] == "rejected"
    ]
    worker_errors = [
        {
            "job_id": result["job_id"],
            "business_code": result["business_code"],
            "stage": result["stage"],
        }
        for result in run["results"]
        if result["status"] == "error"
    ]
    summary = {
        "ok": validation["ok"],
        "stage_version": run["stage_version"],
        "totals": run["plan"]["totals"],
        "rejection_counts": run["plan"]["rejection_counts"],
        "rejections": rejected,
        "worker_errors": worker_errors,
        "parallel_groups": run["plan"]["parallel_groups"],
        "ledger_totals": run["ledger"]["totals"],
        "snapshots": run["snapshots"],
        "validation": {
            "ok": validation["ok"],
            "business_code": validation["business_code"],
            "jobs_verified": validation["jobs_verified"],
            "degraded_jobs_verified": validation["degraded_jobs_verified"],
            "errors_attributed": validation["errors_attributed"],
            "checks": validation["checks"],
        },
    }
    _print_json(summary)
    return 0 if validation["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(
        prog="mixedpipe",
        description="Deterministic mixed train/infer local scheduling pipeline",
    )
    parser.add_argument("--root", default=".", help="project root")
    parser.add_argument("--manifest", help="JSONL task manifest path")
    parser.add_argument("--config", help="pipeline config path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample_parser = subparsers.add_parser("sample", help="generate fixed samples")
    sample_parser.add_argument("--no-force", action="store_true")

    subparsers.add_parser("run", help="schedule and execute configured tasks")
    subparsers.add_parser("validate", help="validate latest snapshots and baselines")
    subparsers.add_parser("demo", help="run the full demo and validation")

    serve_parser = subparsers.add_parser("serve", help="start the local dashboard")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)
    root, manifest_path, config_path = _paths(args)

    try:
        if args.command == "sample":
            _print_json(generate_dataset(root, force=not args.no_force))
            return 0
        if args.command == "run":
            tasks = load_manifest(manifest_path)
            config = load_config(config_path)
            run = Pipeline(root).run(tasks, config)
            _print_json(
                {
                    "stage_version": run["stage_version"],
                    "totals": run["plan"]["totals"],
                    "rejection_counts": run["plan"]["rejection_counts"],
                    "rejections": [
                        {
                            "job_id": decision["job_id"],
                            "business_code": decision["business_code"],
                            "reason": decision["reason"],
                        }
                        for decision in run["plan"]["decisions"]
                        if decision["decision"] == "rejected"
                    ],
                    "results": [
                        {
                            "job_id": result["job_id"],
                            "status": result["status"],
                            "business_code": result.get("business_code"),
                            "stage": result.get("stage"),
                        }
                        for result in run["results"]
                    ],
                    "snapshots": run["snapshots"],
                }
            )
            return 0
        if args.command == "validate":
            validation = Validator(root, manifest_path, config_path).validate()
            _print_json(validation)
            return 0 if validation["ok"] else 1
        if args.command == "demo":
            return _run_demo(args)
        if args.command == "serve":
            ensure_demo(root)
            serve(root, manifest_path, config_path, args.host, args.port)
            return 0
    except BusinessError as exc:
        _print_json(exc.to_dict())
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
