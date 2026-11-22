"""Command-line entry point for report generation and local validation."""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .config import load_config
from .config import normalize_config
from .errors import PipelineError
from .pipeline import run_pipeline
from .series import SeriesData, generate_series, load_series, save_series
from .utils import to_jsonable


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ts-precision-pipeline",
        description=(
            "Run the deterministic local time-series precision propagation "
            "pipeline and write a JSON validation report."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version="ts-precision-pipeline 1.0.0",
    )
    parser.add_argument(
        "--config",
        default="configs/sample_config.json",
        help="prediction config JSON path",
    )
    parser.add_argument(
        "--series",
        help="optional deterministic series JSON produced by generate_sample.py",
    )
    parser.add_argument(
        "--series-out",
        help="write the generated or loaded series to this JSON path",
    )
    parser.add_argument(
        "--output",
        default="reports/report.json",
        help="report output JSON path",
    )
    parser.add_argument(
        "--profile",
        choices=["float64", "float32", "fixed_point"],
        help="override precision.profile",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        help="override training.chunk_size",
    )
    parser.add_argument(
        "--strategy",
        choices=["strict", "tolerance_band"],
        help="override evaluation.threshold_strategy",
    )
    parser.add_argument("--threshold", type=float, help="override evaluation.threshold")
    parser.add_argument("--tolerance", type=float, help="override evaluation.tolerance")
    parser.add_argument(
        "--model-family",
        choices=["linear_autoregressive", "stump_gradient_boosting"],
        help="override model.family",
    )
    parser.add_argument(
        "--snapshot-dir",
        help="override snapshot.directory",
    )
    parser.add_argument(
        "--snapshot-version",
        type=int,
        help="explicit version for all configured snapshots",
    )
    parser.add_argument(
        "--no-snapshots",
        action="store_true",
        help="disable snapshot reads and writes",
    )
    parser.add_argument(
        "--requested-steps",
        type=int,
        help="diagnostic recursion depth; values above horizon trigger E_RECURSION_INVALID",
    )
    parser.add_argument(
        "--print-summary",
        action="store_true",
        help="print a compact report summary to stdout",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the default one-line success summary",
    )
    return parser


def _apply_overrides(config: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    result = copy.deepcopy(config)
    if args.profile:
        result["precision"]["profile"] = args.profile
    if args.chunk_size is not None:
        result["training"]["chunk_size"] = args.chunk_size
    if args.strategy:
        result["evaluation"]["threshold_strategy"] = args.strategy
    if args.threshold is not None:
        result["evaluation"]["threshold"] = args.threshold
    if args.tolerance is not None:
        result["evaluation"]["tolerance"] = args.tolerance
    if args.model_family:
        result["model"]["family"] = args.model_family
    if args.snapshot_dir:
        result["snapshot"]["directory"] = args.snapshot_dir
    if args.no_snapshots:
        result["snapshot"]["enabled"] = False
    return result


def _write_json(path: str | Path, payload: Any) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = handle.name
            json.dump(
                to_jsonable(payload),
                handle,
                ensure_ascii=False,
                allow_nan=False,
                indent=2,
                sort_keys=True,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
    return destination


def _summary(report: dict[str, Any], output_path: Path) -> dict[str, Any]:
    return {
        "business_code": report["business_code"],
        "output": str(output_path),
        "deterministic_core_sha256": report["deterministic_core_sha256"],
        "window_counts": {
            "train": report["windows"]["train_count"],
            "test": report["windows"]["test_count"],
        },
        "precision_profile": report["precision"]["profile"],
        "model_family": report["training"]["model_family"],
        "metrics": {
            "mae": report["metrics"]["mae"],
            "rmse": report["metrics"]["rmse"],
        },
        "dominant_error_source": report["error_spectrum"]["dominant_source"],
        "consistency": report["consistency"]["status"],
        "snapshots": {
            name: item["version"]
            for name, item in report["snapshots"]["items"].items()
        },
    }


def _load_series_if_requested(path: str | None, config: dict[str, Any]) -> SeriesData:
    if path:
        return load_series(path)
    return generate_series(config)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    output_path = Path(args.output)
    try:
        normalized = load_config(args.config)
        config = _apply_overrides(normalized, args)
        config = normalize_config(
            {
                key: value
                for key, value in config.items()
                if not key.startswith("_")
            }
        )
        sample = _load_series_if_requested(args.series, config)
        if args.series_out:
            save_series(sample, args.series_out)
        snapshot_versions = None
        if args.snapshot_version is not None:
            snapshot_versions = {
                name: args.snapshot_version for name in config["snapshot"]["names"]
            }
        report = run_pipeline(
            config,
            series=sample,
            requested_steps=args.requested_steps,
            snapshot_versions=snapshot_versions,
            write_snapshots=not args.no_snapshots,
        )
        _write_json(output_path, report)
        summary = _summary(report, output_path)
        if args.print_summary:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        elif not args.quiet:
            print(
                "OK "
                f"windows={report['windows']['train_count']}train/"
                f"{report['windows']['test_count']}test "
                f"profile={report['precision']['profile']} "
                f"mae={report['metrics']['mae']:.6g} "
                f"dominant={report['error_spectrum']['dominant_source']} "
                f"report={output_path}"
            )
        return 0
    except PipelineError as exc:
        error_report = {
            "schema_version": "1.0",
            "business_code": exc.code,
            "status": "error",
            "error": exc.to_dict(),
        }
        try:
            _write_json(output_path, error_report)
        except OSError:
            pass
        print(json.dumps(error_report, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    except Exception as exc:
        error_report = {
            "schema_version": "1.0",
            "business_code": "E_INTERNAL",
            "status": "error",
            "error": {"code": "E_INTERNAL", "message": str(exc)},
        }
        try:
            _write_json(output_path, error_report)
        except OSError:
            pass
        print(json.dumps(error_report, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
