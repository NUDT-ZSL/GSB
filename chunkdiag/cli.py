"""Command-line examples for deterministic diagnostics."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .api import run_task
from .serialization import pretty_json, to_jsonable


DEMO_TASKS: list[dict[str, Any]] = [
    {
        "job_id": "demo-conv2d",
        "op_kind": "conv2d",
        "input_shape": [1, 8, 9, 2],
        "kernel_shape": [3, 2, 3, 3],
        "stride": [1, 1],
        "padding": 1,
        "seed": 1101,
        "weight_seed": 2201,
        "dtype": "float64",
        "block_policy": {
            "block_size": [3, 4],
            "candidates": [[3, 4], [2, 2]],
            "selection": "fixed",
        },
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
    },
    {
        "job_id": "demo-nearest",
        "op_kind": "interpolate",
        "interp_mode": "nearest",
        "input_shape": [1, 7, 8, 2],
        "scale": [1.6, 1.3],
        "stride": [1, 1],
        "padding": 0,
        "seed": 1102,
        "dtype": "float64",
        "block_policy": {
            "block_size": [3, 3],
            "candidates": [[3, 3], [2, 2]],
            "selection": "fixed",
        },
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
    },
    {
        "job_id": "demo-bilinear",
        "op_kind": "interpolate",
        "interp_mode": "bilinear",
        "input_shape": [1, 7, 8, 2],
        "scale": [1.7, 1.25],
        "stride": [1, 1],
        "padding": 0,
        "seed": 1103,
        "dtype": "float64",
        "block_policy": {
            "block_size": [3, 3],
            "candidates": [[3, 3], [2, 2]],
            "selection": "fixed",
        },
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
    },
    {
        "job_id": "demo-bicubic",
        "op_kind": "interpolate",
        "interp_mode": "bicubic",
        "input_shape": [1, 6, 7, 2],
        "scale": [1.5, 1.3],
        "stride": [1, 1],
        "padding": 0,
        "seed": 1104,
        "dtype": "float64",
        "block_policy": {
            "block_size": [3, 3],
            "candidates": [[3, 3], [2, 2]],
            "selection": "fixed",
        },
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
    },
    {
        "job_id": "demo-inject-validation-nonfinite-weight",
        "op_kind": "conv2d",
        "input_shape": [1, 6, 6, 2],
        "kernel_shape": [2, 2, 3, 3],
        "stride": [1, 1],
        "padding": 1,
        "seed": 1201,
        "weight_seed": 2202,
        "dtype": "float64",
        "block_policy": {"block_size": [3, 3]},
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
        "injection": {"parameter_validation": "nonfinite_weight"},
    },
    {
        "job_id": "demo-inject-block-zero",
        "op_kind": "conv2d",
        "input_shape": [1, 6, 6, 2],
        "kernel_shape": [2, 2, 3, 3],
        "stride": [1, 1],
        "padding": 1,
        "seed": 1202,
        "weight_seed": 2203,
        "dtype": "float64",
        "block_policy": {"block_size": [3, 3], "candidates": [[3, 3], [2, 2]]},
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
        "injection": {"block_partition": "zero_block_size"},
    },
    {
        "job_id": "demo-inject-numeric-nonfinite",
        "op_kind": "conv2d",
        "input_shape": [1, 6, 6, 2],
        "kernel_shape": [2, 2, 3, 3],
        "stride": [1, 1],
        "padding": 1,
        "seed": 1203,
        "weight_seed": 2204,
        "dtype": "float64",
        "block_policy": {"block_size": [3, 3], "candidates": [[3, 3], [2, 2]]},
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
        "injection": {"numeric_compute": "nonfinite_output"},
    },
    {
        "job_id": "demo-inject-compare-tolerance",
        "op_kind": "interpolate",
        "interp_mode": "bilinear",
        "input_shape": [1, 6, 6, 2],
        "scale": [1.5, 1.5],
        "stride": [1, 1],
        "padding": 0,
        "seed": 1204,
        "dtype": "float64",
        "block_policy": {"block_size": [3, 3], "candidates": [[3, 3], [2, 2]]},
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
        "injection": {"deviation_compare": "nonpositive_tolerance"},
    },
    {
        "job_id": "demo-budget-float32-fallback",
        "op_kind": "conv2d",
        "input_shape": [1, 32, 32, 4],
        "kernel_shape": [4, 4, 3, 3],
        "stride": [1, 1],
        "padding": 1,
        "seed": 1301,
        "weight_seed": 2301,
        "dtype": "float64",
        "block_policy": {
            "block_size": [16, 16],
            "candidates": [[16, 16], [8, 8], [4, 4]],
        },
        "tolerance": {"atol": 1e-12, "rtol": 1e-9},
        "resource_limits": {
            "max_resident_bytes": 80000,
            "max_blocks": 1000,
            "max_live_intermediates": 3,
            "allow_smaller_blocks": True,
            "allow_dtype_downgrade": True,
        },
    },
]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                value = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(value, dict):
                raise ValueError(
                    f"{path}:{line_number}: each JSONL line must be a JSON object"
                )
            tasks.append(value)
    return tasks


def _write_jsonl(path: Path, tasks: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for task in tasks:
            handle.write(
                json.dumps(
                    to_jsonable(task),
                    sort_keys=True,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(pretty_json(value) + "\n", encoding="utf-8", newline="\n")


def _aggregate(
    reports: list[dict[str, Any]],
    *,
    command: str,
    task_file: str,
) -> dict[str, Any]:
    status_counts: Counter[str] = Counter()
    code_counts: Counter[str] = Counter()
    injection_stage_counts: Counter[str] = Counter()
    degradation_counts: Counter[str] = Counter()
    operator_counts: Counter[str] = Counter()
    for report in reports:
        status_counts[str(report.get("status"))] += 1
        for code in report.get("business_codes", []):
            code_counts[str(code.get("code"))] += 1
        for injection in report.get("injection_records", []):
            injection_stage_counts[str(injection.get("stage"))] += 1
        for degradation in report.get("degradation_chain", []):
            degradation_counts[str(degradation.get("category"))] += 1
        config = report.get("effective_config")
        if config:
            operator_counts[str(config.get("op_kind"))] += 1
    return {
        "schema_version": "1.0",
        "command": command,
        "task_file": task_file,
        "task_count": len(reports),
        "summary": {
            "status_counts": dict(sorted(status_counts.items())),
            "business_code_counts": dict(sorted(code_counts.items())),
            "injection_stage_counts": dict(sorted(injection_stage_counts.items())),
            "degradation_category_counts": dict(
                sorted(degradation_counts.items())
            ),
            "operator_counts": dict(sorted(operator_counts.items())),
        },
        "results": reports,
    }


def _run_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    for task in tasks:
        result = run_task(task)
        reports.append(result.report)
    return reports


def _ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
    finally:
        if probe.exists():
            probe.unlink()


def _print_summary(report: dict[str, Any], report_path: Path) -> None:
    summary = report["summary"]
    print(f"wrote {report_path}")
    print(f"tasks: {report['task_count']}")
    print(f"statuses: {summary['status_counts']}")
    print(f"business codes: {summary['business_code_counts']}")
    print(f"degradations: {summary['degradation_category_counts']}")
    print(f"injection stages: {summary['injection_stage_counts']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m chunkdiag",
        description=(
            "Deterministic chunked convolution/interpolation diagnostics with "
            "fault injection and degradation records."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="write the deterministic demo task JSONL"
    )
    generate.add_argument(
        "--out", required=True, help="output directory for demo_tasks.jsonl"
    )

    run = subparsers.add_parser("run", help="run an existing task JSONL")
    run.add_argument("--tasks", required=True, help="input JSONL task file")
    run.add_argument("--out", required=True, help="output report JSON file")

    demo = subparsers.add_parser(
        "demo", help="generate samples, run all demos, and write report.json"
    )
    demo.add_argument(
        "--out-dir", required=True, help="writable output directory"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            out_dir = Path(args.out)
            _ensure_directory(out_dir)
            tasks_path = out_dir / "demo_tasks.jsonl"
            _write_jsonl(tasks_path, DEMO_TASKS)
            print(f"wrote {tasks_path} ({len(DEMO_TASKS)} tasks)")
            return 0

        if args.command == "run":
            tasks_path = Path(args.tasks)
            tasks = _read_jsonl(tasks_path)
            reports = _run_tasks(tasks)
            aggregate = _aggregate(
                reports, command="run", task_file=str(tasks_path)
            )
            out_path = Path(args.out)
            _ensure_directory(out_path.parent)
            _write_json(out_path, aggregate)
            _print_summary(aggregate, out_path)
            return 0

        if args.command == "demo":
            out_dir = Path(args.out_dir)
            _ensure_directory(out_dir)
            tasks_path = out_dir / "demo_tasks.jsonl"
            report_path = out_dir / "report.json"
            _write_jsonl(tasks_path, DEMO_TASKS)
            reports = _run_tasks(DEMO_TASKS)
            aggregate = _aggregate(
                reports, command="demo", task_file=str(tasks_path.name)
            )
            aggregate["generated_files"] = {
                "tasks_jsonl": tasks_path.name,
                "report_json": report_path.name,
            }
            _write_json(report_path, aggregate)
            _print_summary(aggregate, report_path)
            return 0
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    parser.print_help()
    return 2
