from __future__ import annotations

import argparse
import json
import platform
import sys
from pathlib import Path
from typing import Any

import numpy as np

from .config import load_config
from .errors import PipelineError
from .pipeline import run_pipeline
from .samples import generate_dataset, load_manifest
from .utils import canonical_json_bytes, file_sha256, json_sha256, write_json


def _relative_label(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "numpy": np.__version__,
        "platform": platform.system(),
    }


def _prepare(config_path: Path, data_dir: Path) -> tuple[dict[str, Any], list[dict[str, Any]], Path, dict[str, Any]]:
    config = load_config(config_path)
    manifest_path = generate_dataset(config, data_dir)
    samples, manifest_dir = load_manifest(manifest_path)
    manifest_info = {
        "manifest": _relative_label(manifest_path),
        "manifest_sha256": file_sha256(manifest_path),
        "sample_count": len(samples),
        "data_directory": _relative_label(data_dir),
    }
    return config, samples, manifest_dir, manifest_info


def _run_twice(
    config: dict[str, Any],
    samples: list[dict[str, Any]],
    manifest_dir: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    first = run_pipeline(samples, config, manifest_dir)
    second = run_pipeline(samples, config, manifest_dir)
    return first, second


def build_experiment(
    baseline_config_path: Path,
    scale_config_path: Path,
    data_root: Path,
) -> dict[str, Any]:
    baseline_config, baseline_samples, baseline_dir, baseline_manifest = _prepare(
        baseline_config_path,
        data_root / "baseline",
    )
    scale_config, scale_samples, scale_dir, scale_manifest = _prepare(
        scale_config_path,
        data_root / "scale_trace",
    )
    baseline_first, baseline_second = _run_twice(
        baseline_config, baseline_samples, baseline_dir
    )
    scale_first, scale_second = _run_twice(scale_config, scale_samples, scale_dir)

    baseline_reproducible = (
        baseline_first["report_sha256"] == baseline_second["report_sha256"]
        and canonical_json_bytes(baseline_first)
        == canonical_json_bytes(baseline_second)
    )
    scale_reproducible = (
        scale_first["report_sha256"] == scale_second["report_sha256"]
        and canonical_json_bytes(scale_first) == canonical_json_bytes(scale_second)
    )
    combined_reproducible = baseline_reproducible and scale_reproducible
    self_check_ok = (
        baseline_first["self_check"]["ok"] and scale_first["self_check"]["ok"]
    )
    payload = {
        "schema_version": "1.0",
        "status": "ok" if combined_reproducible and self_check_ok else "failed",
        "environment": _environment(),
        "inputs": {
            "baseline_config": _relative_label(baseline_config_path),
            "scale_trace_config": _relative_label(scale_config_path),
            "baseline": baseline_manifest,
            "scale_trace": scale_manifest,
        },
        "baseline": baseline_first,
        "scale_trace": scale_first,
        "reproducibility": {
            "seed": int(baseline_config["seed"]),
            "runs_per_experiment": 2,
            "baseline_report_sha256": baseline_first["report_sha256"],
            "baseline_second_report_sha256": baseline_second["report_sha256"],
            "scale_trace_report_sha256": scale_first["report_sha256"],
            "scale_trace_second_report_sha256": scale_second["report_sha256"],
            "baseline_matched": baseline_reproducible,
            "scale_trace_matched": scale_reproducible,
            "all_matched": combined_reproducible,
        },
        "self_check": {
            "ok": self_check_ok,
            "baseline": baseline_first["self_check"],
            "scale_trace": scale_first["self_check"],
        },
    }
    payload["content_sha256"] = json_sha256(payload)
    return payload


def _error_report(error: PipelineError) -> dict[str, Any]:
    report = {
        "schema_version": "1.0",
        "status": "error",
        "business_codes": [error.to_dict()],
    }
    report["content_sha256"] = json_sha256(report)
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the deterministic pose pipeline and scale-trace experiment."
    )
    parser.add_argument(
        "--baseline-config",
        default="configs/baseline.json",
        help="baseline JSON configuration",
    )
    parser.add_argument(
        "--scale-config",
        default="configs/scale_trace.json",
        help="scale-trace JSON configuration",
    )
    parser.add_argument(
        "--data-root",
        default="data",
        help="directory for deterministic manifests and heatmaps",
    )
    parser.add_argument(
        "--out",
        default="reports/experiment_report.json",
        help="combined JSON report path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    out_path = Path(args.out)
    try:
        report = build_experiment(
            Path(args.baseline_config),
            Path(args.scale_config),
            Path(args.data_root),
        )
    except PipelineError as exc:
        report = _error_report(exc)
        write_json(out_path, report)
        print(
            json.dumps(
                {
                    "status": "error",
                    "business_codes": report["business_codes"],
                    "report": str(out_path),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2

    write_json(out_path, report)
    summary = {
        "status": report["status"],
        "report": str(out_path),
        "baseline": {
            "images": report["baseline"]["summary"]["image_count"],
            "paths": report["baseline"]["summary"]["selected_path_counts"],
            "pck": report["baseline"]["summary"]["metrics"]["pck"],
            "degraded_images": report["baseline"]["summary"]["degraded_image_count"],
        },
        "scale_trace": {
            "images": report["scale_trace"]["summary"]["image_count"],
            "paths": report["scale_trace"]["summary"]["selected_path_counts"],
            "pck": report["scale_trace"]["summary"]["metrics"]["pck"],
            "degraded_images": report["scale_trace"]["summary"]["degraded_image_count"],
        },
        "self_check": report["self_check"]["ok"],
        "reproducible": report["reproducibility"]["all_matched"],
        "content_sha256": report["content_sha256"],
    }
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
