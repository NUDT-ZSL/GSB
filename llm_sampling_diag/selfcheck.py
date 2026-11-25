from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import canonical_json, first_difference, sha256_json
from .errors import DiagnosticError, ErrorCode
from .formats import SuiteSources, read_suite
from .model import OperatorConfig, UnifiedTask
from .pipeline import run_task
from .validation import assert_unique_case_ids, validate_task


def _index_tasks(tasks: list[UnifiedTask], format_name: str) -> dict[str, UnifiedTask]:
    result = {task.case_id: task for task in tasks}
    if len(result) != len(tasks):
        seen: dict[str, UnifiedTask] = {}
        for task in tasks:
            if task.case_id in seen:
                raise DiagnosticError(
                    ErrorCode.E_DUP_CASE,
                    f"duplicate case_id {task.case_id!r} in {format_name}",
                    field="case_id",
                    case_id=task.case_id,
                    details={
                        "first_source": seen[task.case_id].source_ref,
                        "second_source": task.source_ref,
                    },
                )
            seen[task.case_id] = task
    return result


def run_self_check(
    suite_root: str | Path | None = None,
    *,
    sources: SuiteSources | None = None,
    config: OperatorConfig | None = None,
) -> dict[str, Any]:
    config = config or OperatorConfig(mask_priority="mask_first")
    suite = read_suite(suite_root, sources=sources)
    by_format: dict[str, dict[str, UnifiedTask]] = {}
    for format_name in ("v1", "v2", "v3"):
        tasks = suite.per_format[format_name].tasks
        assert_unique_case_ids(tasks)
        for task in tasks:
            validate_task(task)
        by_format[format_name] = _index_tasks(tasks, format_name)
    case_ids = sorted(set(by_format["v1"]) | set(by_format["v2"]) | set(by_format["v3"]))
    cases: list[dict[str, Any]] = []
    for case_id in case_ids:
        format_tasks: dict[str, UnifiedTask] = {}
        for format_name in ("v1", "v2", "v3"):
            task = by_format[format_name].get(case_id)
            if task is None:
                raise DiagnosticError(
                    ErrorCode.E_INCONSISTENT,
                    f"case {case_id!r} is missing from {format_name}",
                    field="case_id",
                    case_id=case_id,
                    details={"missing_format": format_name, "available_formats": sorted(format_tasks)},
                )
            format_tasks[format_name] = task
        projections = {
            format_name: task.semantic_projection()
            for format_name, task in format_tasks.items()
        }
        normalized_deviation = first_difference(projections["v1"], projections["v2"])
        if normalized_deviation is None:
            normalized_deviation = first_difference(projections["v1"], projections["v3"])
        if normalized_deviation is not None:
            raise DiagnosticError(
                ErrorCode.E_INCONSISTENT,
                "normalized task differs across formats",
                field=normalized_deviation.get("path", "$"),
                case_id=case_id,
                details={
                    "first_deviation": normalized_deviation,
                    "normalized_projections": projections,
                },
            )
        reports: dict[str, dict[str, Any]] = {}
        for format_name, task in format_tasks.items():
            reports[format_name] = run_task(task, config)
        reference_format = "v1"
        reference = reports[reference_format]
        for format_name in ("v2", "v3"):
            candidate = reports[format_name]
            deviation = first_difference(
                {
                    "candidate_order": reference["candidate_order"],
                    "sample_sequence": reference["sample_sequence"],
                    "error_spectrum": reference["error_spectrum"],
                },
                {
                    "candidate_order": candidate["candidate_order"],
                    "sample_sequence": candidate["sample_sequence"],
                    "error_spectrum": candidate["error_spectrum"],
                },
            )
            if deviation is not None:
                raise DiagnosticError(
                    ErrorCode.E_INCONSISTENT,
                    f"cross-format result differs between {reference_format} and {format_name}",
                    field=deviation.get("path", "$"),
                    case_id=case_id,
                    details={
                        "reference_format": reference_format,
                        "candidate_format": format_name,
                        "first_deviation": deviation,
                        "reference_sample_sequence": reference["sample_sequence"],
                        "candidate_sample_sequence": candidate["sample_sequence"],
                        "reference_spectrum_checksum": sha256_json(reference["error_spectrum"]),
                        "candidate_spectrum_checksum": sha256_json(candidate["error_spectrum"]),
                    },
                )
        error_spectrum = reports["v1"]["error_spectrum"]
        cases.append(
            {
                "case_id": case_id,
                "formats": sorted(format_tasks),
                "sample_sequences": {
                    format_name: reports[format_name]["sample_sequence"]
                    for format_name in sorted(reports)
                },
                "candidate_orders": {
                    format_name: reports[format_name]["candidate_order"]
                    for format_name in sorted(reports)
                },
                "cache_keys": {
                    format_name: reports[format_name]["cache_key"]
                    for format_name in sorted(reports)
                },
                "error_spectrum_checksum": sha256_json(error_spectrum),
                "max_abs_error": error_spectrum["max_abs_error"],
                "within_tolerance": error_spectrum["within_tolerance"],
                "consistent": True,
                "reports": reports,
            }
        )
    summary = {
        "case_count": len(cases),
        "format_count": 3,
        "tasks_checked": len(cases) * 3,
        "all_consistent": True,
    }
    report = {
        "schema": "sampling-selfcheck-v1",
        "config": config.to_dict(),
        "summary": summary,
        "cases": cases,
        "migration_records": suite.records,
    }
    report["report_checksum"] = sha256_json(report)
    return report


def assert_self_check(report: dict[str, Any]) -> None:
    if not report.get("summary", {}).get("all_consistent"):
        raise DiagnosticError(ErrorCode.E_INCONSISTENT, "self-check report is not consistent")
    for case in report.get("cases", []):
        sequences = case.get("sample_sequences", {})
        if sequences and len({canonical_json(value) for value in sequences.values()}) != 1:
            raise DiagnosticError(ErrorCode.E_INCONSISTENT, "sample sequences differ", case_id=case.get("case_id"))

