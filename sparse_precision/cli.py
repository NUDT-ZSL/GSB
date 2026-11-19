"""Offline CLI: deterministic sample generation, batch run and demonstrations."""

from __future__ import annotations

import argparse
import copy
import json
import platform
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np
import scipy

from .api import execute_case
from .codes import (
    E_RESOURCE_LIMIT,
    E_SHAPE_INVALID,
    E_TOLERANCE_EXCEEDED,
    OK,
)
from .models import OperatorConfig
from .sample_data import load_sample_files, sample_definitions, write_samples


def _environment() -> dict[str, Any]:
    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scipy": scipy.__version__,
        "network_required": False,
        "external_service_required": False,
    }


def _load_config(path: str | None) -> OperatorConfig:
    if path is None:
        return OperatorConfig()
    values = json.loads(Path(path).read_text(encoding="utf-8"))
    return OperatorConfig.from_mapping(values)


def _write_json(path: str | Path, payload: dict[str, Any]) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _case_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": report.get("case_id"),
        "kind": report.get("kind"),
        "ok": report.get("ok"),
        "code": report.get("code"),
        "phase": report.get("phase"),
        "field": report.get("field"),
        "max_abs_error": (
            report.get("diagnostics", {}).get("max_abs_error")
            if isinstance(report.get("diagnostics"), dict)
            else None
        ),
        "error_amplification_ratio": (
            report.get("diagnostics", {}).get("error_amplification_ratio")
            if isinstance(report.get("diagnostics"), dict)
            else None
        ),
        "dominant_error_source": (
            report.get("diagnostics", {})
            .get("stage_attribution", {})
            .get("dominant_source")
            if isinstance(report.get("diagnostics"), dict)
            else None
        ),
        "degraded": bool(
            report.get("degradation", {}).get("degraded", False)
        ),
        "degradation_category": report.get("degradation", {}).get("category"),
    }


def _run_metadata(
    metadata: dict[str, Any],
    config: OperatorConfig,
    *,
    include_result: bool,
) -> dict[str, Any]:
    return execute_case(
        metadata,
        config=config,
        include_result=include_result,
    )


def command_generate(args: argparse.Namespace) -> int:
    manifest = write_samples(args.out, base_seed=args.seed)
    print(
        json.dumps(
            {"out": str(Path(args.out)), **manifest},
            ensure_ascii=True,
        )
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    try:
        config = _load_config(args.config)
    except Exception as error:
        payload = {
            "ok": False,
            "code": "E_CONFIG_INVALID",
            "message": str(error),
        }
        _write_json(args.out, payload)
        print(json.dumps(payload, ensure_ascii=True))
        return 2
    metadata_cases = load_sample_files(args.samples)
    reports = [
        _run_metadata(metadata, config, include_result=args.include_result)
        for metadata in metadata_cases
    ]
    codes = Counter(report.get("code") for report in reports)
    report = {
        "schema_version": "1.0",
        "environment": _environment(),
        "config": config.to_dict(),
        "sample_directory": str(Path(args.samples)),
        "cases": reports,
        "summary": {
            "case_count": len(reports),
            "ok_count": sum(1 for item in reports if item.get("code") == OK),
            "degraded_count": sum(
                1
                for item in reports
                if item.get("degradation", {}).get("degraded", False)
            ),
            "codes": dict(sorted(codes.items())),
        },
    }
    _write_json(args.out, report)
    print(
        json.dumps(
            {
                "out": str(Path(args.out)),
                "case_count": len(reports),
                "codes": dict(sorted(codes.items())),
            },
            ensure_ascii=True,
        )
    )
    return 0 if all(code == OK for code in codes) else 1


def _downgrade_demo(
    metadata: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    values = {
        "max_blocks": 8,
        "max_aggregation_bytes": 1,
        "max_memory_bytes": 32 * 1024 * 1024,
    }
    config = OperatorConfig.from_mapping(values)
    report = execute_case(metadata, config=config, include_result=True)
    return values, report


def _resource_error_demo(metadata: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    values = {
        "max_blocks": 1,
        "max_memory_bytes": 1,
        "max_aggregation_bytes": 1,
    }
    config = OperatorConfig.from_mapping(values)
    return values, execute_case(metadata, config=config)


def _tolerance_probe(metadata: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    values = {
        "atol": 0.0,
        "rtol": 0.0,
        "compensated_sum": False,
        "forced_row_block": 8,
        "forced_accumulation_order": "reverse",
        "enable_self_check": False,
    }
    config = OperatorConfig.from_mapping(values)
    return values, execute_case(metadata, config=config)


def command_demo(args: argparse.Namespace) -> int:
    sample_directory = Path(args.samples)
    manifest = write_samples(sample_directory, base_seed=args.seed)
    metadata_cases = load_sample_files(sample_directory)
    by_id = {
        metadata["case_id"]: metadata
        for metadata in metadata_cases
    }
    config = OperatorConfig()
    normal_reports = [
        execute_case(metadata, config=config, include_result=True)
        for metadata in metadata_cases
    ]

    invalid = copy.deepcopy(by_id["spmm_dense_7"])
    invalid["case_id"] = "demo_invalid_shape"
    invalid["shape_mnk"] = [0, 96, 7]
    invalid["checksums"] = {}
    invalid_report = execute_case(invalid, config=config)

    downgrade_inputs, downgrade_report = _downgrade_demo(
        by_id["spspmm_aggregated_32"]
    )
    resource_inputs, resource_report = _resource_error_demo(
        by_id["spmm_dense_7"]
    )
    tolerance_inputs, tolerance_report = _tolerance_probe(
        by_id["spmm_vector_1"]
    )

    expected = {
        "invalid_shape_returns_E_SHAPE_INVALID": invalid_report.get("code")
        == E_SHAPE_INVALID,
        "over_budget_plan_is_marked_degraded": bool(
            downgrade_report.get("degradation", {}).get("degraded", False)
        ),
        "impossible_budget_returns_E_RESOURCE_LIMIT": resource_report.get("code")
        == E_RESOURCE_LIMIT,
        "tight_zero_tolerance_returns_E_TOLERANCE_EXCEEDED": (
            tolerance_report.get("code") == E_TOLERANCE_EXCEEDED
        ),
    }
    payload = {
        "schema_version": "1.0",
        "environment": _environment(),
        "sample_directory": str(sample_directory),
        "manifest": manifest,
        "default_config": config.to_dict(),
        "normal_cases": normal_reports,
        "normal_summary": [
            _case_summary(report) for report in normal_reports
        ],
        "demonstrations": {
            "invalid_input": {
                "injected_case_id": invalid["case_id"],
                "injection": "shape_mnk[0] changed from 72 to 0",
                "report": invalid_report,
            },
            "resource_downgrade": {
                "case_id": "spspmm_aggregated_32",
                "config_override": downgrade_inputs,
                "report": downgrade_report,
            },
            "impossible_budget": {
                "case_id": "spmm_dense_7",
                "config_override": resource_inputs,
                "report": resource_report,
            },
            "tolerance_probe": {
                "case_id": "spmm_vector_1",
                "config_override": tolerance_inputs,
                "report": tolerance_report,
            },
        },
        "expectations": expected,
        "all_expectations_met": all(expected.values()),
    }
    _write_json(args.out, payload)
    print(
        json.dumps(
            {
                "out": str(Path(args.out)),
                "sample_directory": str(sample_directory),
                "normal_cases": len(normal_reports),
                "expectations": expected,
            },
            ensure_ascii=True,
        )
    )
    return 0 if all(expected.values()) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sparse-precision",
        description="Deterministic blocked SpMM/SpSpMM precision component",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="write deterministic metadata JSON samples"
    )
    generate.add_argument("--out", default="artifacts/samples")
    generate.add_argument("--seed", type=int, default=20261119)
    generate.set_defaults(handler=command_generate)

    run = subparsers.add_parser(
        "run", help="execute every case under a sample directory"
    )
    run.add_argument("--samples", default="artifacts/samples")
    run.add_argument("--out", default="artifacts/report.json")
    run.add_argument("--config", default=None)
    run.add_argument("--include-result", action="store_true")
    run.set_defaults(handler=command_run)

    demo = subparsers.add_parser(
        "demo", help="generate samples and run success/failure demonstrations"
    )
    demo.add_argument("--out", default="artifacts/demo_report.json")
    demo.add_argument("--samples", default="artifacts/samples")
    demo.add_argument("--seed", type=int, default=20261119)
    demo.set_defaults(handler=command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())

