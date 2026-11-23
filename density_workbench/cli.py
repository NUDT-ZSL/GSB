from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import write_json_atomic
from .config import WorkbenchConfig
from .data import load_samples
from .errors import BusinessError
from .workbench import Workbench, ensure_workspace


def _add_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--root",
        default=".",
        help="working directory containing samples.jsonl and config.json",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="density-workbench",
        description="Deterministic GMM/KDE clustering and density diagnostics",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser(
        "init", help="deterministically create samples and config"
    )
    _add_root(init_parser)
    init_parser.add_argument("--force", action="store_true")
    init_parser.add_argument("--samples", type=int, default=48)
    init_parser.add_argument("--dimensions", type=int, default=3)

    run_parser = subparsers.add_parser("run", help="run a full analysis")
    _add_root(run_parser)
    run_parser.add_argument("--force", action="store_true")
    _add_analysis_overrides(run_parser)

    query_parser = subparsers.add_parser(
        "query", help="query one sample by sample_id"
    )
    _add_root(query_parser)
    query_parser.add_argument("sample_id")
    query_parser.add_argument(
        "--budget-ms", type=float, default=None, help="override latency budget"
    )
    _add_analysis_overrides(query_parser)

    spectrum_parser = subparsers.add_parser(
        "error-spectrum", help="build a float64-referenced error spectrum"
    )
    _add_root(spectrum_parser)
    _add_analysis_overrides(spectrum_parser)

    check_parser = subparsers.add_parser(
        "self-check", help="run cross-module consistency checks"
    )
    _add_root(check_parser)
    _add_analysis_overrides(check_parser)

    demo_parser = subparsers.add_parser(
        "demo", help="generate data, analyze, query and self-check"
    )
    _add_root(demo_parser)
    demo_parser.add_argument("--force", action="store_true")
    return parser


def _add_analysis_overrides(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--precision", choices=("float64", "float32", "fixed"))
    parser.add_argument("--chunk-size", type=int)
    parser.add_argument("--model", choices=("gmm", "kde"))
    parser.add_argument("--components", type=int)
    parser.add_argument("--bandwidth", type=float)


def _workbench_from_args(args: argparse.Namespace) -> Workbench:
    paths = ensure_workspace(args.root)
    config = WorkbenchConfig.from_json_file(str(paths["config"]))
    overrides: dict[str, Any] = {}
    if getattr(args, "precision", None):
        overrides["precision"] = args.precision
    if getattr(args, "chunk_size", None):
        overrides["chunk_size"] = args.chunk_size
    if getattr(args, "model", None):
        overrides["model_family"] = args.model
    if getattr(args, "components", None):
        overrides["n_components"] = args.components
    if getattr(args, "bandwidth", None) is not None:
        overrides["bandwidth"] = args.bandwidth
    if overrides:
        config = WorkbenchConfig.from_dict({**config.to_dict(), **overrides})
    return Workbench(
        args.root,
        samples_path=paths["samples"],
        config_path=paths["config"],
        cache_dir=paths["cache_dir"],
        snapshot_dir=paths["snapshot_dir"],
        artifact_dir=paths["artifact_dir"],
        config=config,
    )


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "demo"
        args.root = "."
        args.force = False
    try:
        if args.command == "init":
            paths = ensure_workspace(
                args.root,
                force=args.force,
                n_samples=args.samples,
                dimensions=args.dimensions,
            )
            _print(
                {
                    "ok": True,
                    "root": str(paths["root"]),
                    "samples": str(paths["samples"]),
                    "config": str(paths["config"]),
                    "cache_dir": str(paths["cache_dir"]),
                    "snapshot_dir": str(paths["snapshot_dir"]),
                }
            )
            return 0
        workbench = _workbench_from_args(args)
        if args.command == "run":
            _print(workbench.analyze(force=args.force))
            return 0
        if args.command == "query":
            _print(
                workbench.query(
                    args.sample_id, latency_budget_ms=args.budget_ms
                )
            )
            return 0
        if args.command == "error-spectrum":
            _print(workbench.error_spectrum())
            return 0
        if args.command == "self-check":
            result = workbench.self_check()
            _print(result)
            return 0 if result["ok"] else 1
        if args.command == "demo":
            analysis = workbench.analyze(force=args.force)
            sample_id = workbench.sample_ids[0]
            query = workbench.query(sample_id)
            checks = workbench.self_check()
            spectrum = workbench.error_spectrum()
            summary = {
                "ok": checks["ok"],
                "root": str(workbench.root),
                "sample_count": analysis["sample_count"],
                "chunk_count": analysis["chunk_count"],
                "model_family": analysis["model_family"],
                "precision": analysis["precision"],
                "snapshot_version": analysis["snapshot"]["bundle_version"],
                "batch_checksum": analysis["batch_checksum"],
                "query_sample_id": sample_id,
                "query_component": query["component"],
                "query_density": query["density"],
                "query_elapsed_ms": query["latency"]["elapsed_ms"],
                "query_batch_max_abs_deviation": query["consistency"][
                    "max_absolute_deviation"
                ],
                "dominant_error_source": spectrum["localization"][
                    "dominant_source"
                ],
                "self_check_failures": checks["failed"],
                "artifacts": str(workbench.artifact_dir),
            }
            write_json_atomic(
                workbench.artifact_dir / "demo_summary.json",
                summary,
                indent=2,
            )
            _print(summary)
            return 0 if checks["ok"] else 1
        parser.error(f"unknown command: {args.command}")
        return 2
    except BusinessError as exc:
        _print(exc.to_dict())
        return 2
    except Exception as exc:  # defensive CLI boundary
        _print(
            {
                "ok": False,
                "error": {
                    "code": "E_INTERNAL",
                    "message": str(exc),
                    "details": {"type": type(exc).__name__},
                },
            }
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
