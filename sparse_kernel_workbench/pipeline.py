"""End-to-end report construction and atomic report output."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .audit import (
    cross_version_audit,
    ledger_hash,
    matrix_hash,
    recommend_strategy,
    result_hash,
    vector_hash,
)
from .batching import batching_report, build_batching_plan
from .distribution import nonzero_distribution, select_layout
from .errors import WorkbenchError, validation_error
from .hashing import sha256_file, stable_hash
from .kernel import execute_kernel
from .model import NormalizedMatrix, decimal_text
from .normalize import normalize_fragment
from .reference import compare_with_reference, dense_reference
from .spec import (
    OperationSpec,
    VersionSpec,
    apply_operation_overrides,
    load_operation_spec,
    load_version_spec,
)


REPORT_SCHEMA_VERSION = 1


def _display_path(path: Path, base: Path | None) -> str:
    if base is not None:
        try:
            return str(path.resolve().relative_to(base.resolve()))
        except ValueError:
            pass
    return str(path.resolve())


def _operation_report(
    operation: OperationSpec,
    base: Path | None,
    overrides: dict[str, Any],
) -> dict[str, Any]:
    return {
        "operation_id": operation.raw.get("operation_id"),
        "spec_path": _display_path(operation.path, base),
        "spec_sha256": sha256_file(operation.path),
        "direction": operation.direction,
        "batch_nonzero_budget": operation.batch_budget,
        "dense_vector_chunk_size": operation.dense_chunk_size,
        "hotspot_multiplier": decimal_text(operation.hotspot_multiplier),
        "hotspot_average_basis": operation.hotspot_average_basis,
        "tolerance": operation.tolerance,
        "strategies": list(operation.strategies),
        "cli_overrides": overrides,
    }


def _normalization_report(
    matrix: NormalizedMatrix,
    base: Path | None,
) -> dict[str, Any]:
    return {
        "version": matrix.version,
        "format": matrix.format,
        "source_path": _display_path(matrix.source_path, base),
        "spec_path": _display_path(matrix.spec_path, base),
        "source_sha256": sha256_file(matrix.source_path),
        "spec_sha256": sha256_file(matrix.spec_path),
        "dense_vector_sha256": sha256_file(matrix.vector.source_path),
        "raw_rows": matrix.raw_rows,
        "raw_columns": matrix.raw_columns,
        "raw_nonzero_count": matrix.raw_nonzero_count,
        "normalized_rows": matrix.rows,
        "normalized_columns": matrix.columns,
        "canonical_nonzero_count": matrix.nonzero_count,
        "duplicate_entries_merged": matrix.duplicate_count,
        "highest_declared_row_index": matrix.highest_row_index,
        "highest_declared_column_index": matrix.highest_column_index,
        "dimension_rule": matrix.dimension_rule,
        "index_base": matrix.index_base,
        "index_offset_to_zero": -matrix.index_base,
        "weight_field": matrix.weight_field,
        "weight_unit": matrix.weight_unit,
        "weight_conversion_factor": decimal_text(matrix.weight_factor),
        "canonical_weight_unit": "count",
        "empty_rows": [
            index for index, count in enumerate(matrix.row_counts()) if count == 0
        ],
        "empty_columns": [
            index for index, count in enumerate(matrix.column_counts()) if count == 0
        ],
        "field_corrections": matrix.corrections,
    }


def _vector_report(matrix: NormalizedMatrix, base: Path | None) -> dict[str, Any]:
    return {
        "source_path": _display_path(matrix.vector.source_path, base),
        "sha256": sha256_file(matrix.vector.source_path),
        "index_base": matrix.vector.index_base,
        "index_offset_to_zero": -matrix.vector.index_base,
        "length": len(matrix.vector.values),
        "ids_field": matrix.vector.ids_field,
        "values_field": matrix.vector.values_field,
        "values": [float(value) for value in matrix.vector.values],
        "field_corrections": matrix.vector.corrections,
    }


def _run_version(
    matrix: NormalizedMatrix,
    operation: OperationSpec,
    base: Path | None,
) -> dict[str, Any]:
    distribution = nonzero_distribution(matrix)
    selection = select_layout(operation, distribution)
    reference = dense_reference(matrix)
    strategies: dict[str, dict[str, Any]] = {}
    strategy_runs: dict[str, dict[str, Any]] = {}

    for layout in operation.strategies:
        plan = build_batching_plan(matrix, operation, layout)
        kernel_run = execute_kernel(matrix, operation, plan)
        comparison = compare_with_reference(
            matrix,
            operation,
            kernel_run["result_vector"],
            reference,
        )
        strategies[layout] = {
            "layout": layout,
            "batching": batching_report(plan),
            "kernel": kernel_run,
            "ledger": kernel_run["ledger"],
            "comparison": comparison,
            "result_hash": result_hash(kernel_run["result_vector"]),
            "ledger_hash": ledger_hash(kernel_run["ledger"]),
        }
        strategy_runs[layout] = {
            "result_vector": kernel_run["result_vector"],
            "ledger": kernel_run["ledger"],
            "comparison": comparison,
        }

    recommendation, conflict = recommend_strategy(
        operation,
        selection["selected"],
        strategy_runs,
    )
    report: dict[str, Any] = {
        "version": matrix.version,
        "format": matrix.format,
        "normalization": _normalization_report(matrix, base),
        "dense_vector": _vector_report(matrix, base),
        "distribution": distribution,
        "layout_selection": selection,
        "reference": reference,
        "strategies": strategies,
        "recommendation": recommendation,
        "conflict": conflict,
        "status": "ok" if recommendation is not None else "conflict",
        "fingerprints": {
            "matrix": matrix_hash(matrix),
            "dense_vector": vector_hash(matrix),
        },
    }
    return report


def _resolve_spec_paths(paths: Iterable[str | Path] | None, base: Path | None) -> list[Path]:
    root = base or Path.cwd()
    if paths:
        resolved = []
        for path_value in paths:
            path = Path(path_value)
            if not path.is_absolute():
                path = root / path
            resolved.append(path.resolve())
    else:
        resolved = sorted((root / "data" / "versions").glob("*/version.json"))
    if not resolved:
        raise WorkbenchError(
            "SKW-IO-008",
            "No version specification files were found",
            stage="input",
            context={"field": "version_specs", "element": "input"},
        )
    for path in resolved:
        if not path.exists():
            raise WorkbenchError(
                "SKW-IO-001",
                f"Version specification file does not exist: {path}",
                stage="input",
                context={
                    "file": str(path),
                    "field": "version_spec",
                    "element": "input",
                },
            )
    return resolved


def build_report(
    *,
    operation_spec: str | Path,
    version_specs: Iterable[str | Path] | None = None,
    base_dir: str | Path | None = None,
    direction: str | None = None,
    batch_budget: int | None = None,
    dense_chunk_size: int | None = None,
    hotspot_multiplier: Any = None,
    tolerance: float | None = None,
) -> dict[str, Any]:
    base = Path(base_dir).resolve() if base_dir is not None else Path.cwd().resolve()
    operation = load_operation_spec(Path(operation_spec))
    override_decimal = None
    if hotspot_multiplier is not None:
        try:
            from decimal import Decimal

            override_decimal = Decimal(str(hotspot_multiplier))
        except Exception as exc:  # pragma: no cover - defensive conversion
            raise validation_error(
                "SKW-OPSPEC-007",
                "hotspot multiplier override must be a decimal number",
                stage="operation",
                field="hotspot_multiplier",
                element="cli_override",
                raw_value=hotspot_multiplier,
            ) from exc
    operation = apply_operation_overrides(
        operation,
        direction=direction,
        batch_budget=batch_budget,
        dense_chunk_size=dense_chunk_size,
        hotspot_multiplier=override_decimal,
        tolerance=tolerance,
    )
    overrides = {
        key: value
        for key, value in {
            "direction": direction,
            "batch_nonzero_budget": batch_budget,
            "dense_vector_chunk_size": dense_chunk_size,
            "hotspot_multiplier": (
                decimal_text(override_decimal) if override_decimal is not None else None
            ),
            "tolerance": tolerance,
        }.items()
        if value is not None
    }

    spec_paths = _resolve_spec_paths(version_specs, base)
    version_spec_objects: list[VersionSpec] = []
    seen_versions: set[str] = set()
    for path in spec_paths:
        spec = load_version_spec(path)
        if spec.version in seen_versions:
            raise validation_error(
                "SKW-SPEC-012",
                f"Duplicate version identifier: {spec.version}",
                stage="spec",
                version=spec.version,
                file=str(path),
                field="version",
                element="spec",
                raw_value=spec.version,
            )
        seen_versions.add(spec.version)
        version_spec_objects.append(spec)
    version_spec_objects.sort(key=lambda spec: spec.version)

    matrices = [normalize_fragment(spec) for spec in version_spec_objects]
    version_reports = [
        _run_version(matrix, operation, base)
        for matrix in matrices
    ]
    cross_version = cross_version_audit(matrices, version_reports, operation)

    version_conflict = any(report["status"] != "ok" for report in version_reports)
    cross_version_conflict = not cross_version["consistent"]
    status = "ok"
    if version_conflict or cross_version_conflict:
        status = "conflict"

    top_recommendation = None
    if status == "ok" and version_reports:
        top_recommendation = version_reports[0]["recommendation"]

    operation_report = _operation_report(operation, base, overrides)
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "status": status,
        "operation": operation_report,
        "run_fingerprint": stable_hash(
            {
                "operation": operation_report["spec_sha256"],
                "direction": operation.direction,
                "batch_nonzero_budget": operation.batch_budget,
                "dense_vector_chunk_size": operation.dense_chunk_size,
                "hotspot_multiplier": decimal_text(operation.hotspot_multiplier),
                "hotspot_average_basis": operation.hotspot_average_basis,
                "tolerance": operation.tolerance,
                "versions": [
                    {
                        "version": matrix.version,
                        "spec_sha256": sha256_file(matrix.spec_path),
                        "matrix_sha256": sha256_file(matrix.source_path),
                        "vector_sha256": sha256_file(matrix.vector.source_path),
                    }
                    for matrix in matrices
                ],
            }
        ),
        "versions": version_reports,
        "cross_version": cross_version,
        "recommendation": top_recommendation,
        "recommendation_refused": top_recommendation is None,
    }
    return report


def atomic_write_json(path: str | Path, value: dict[str, Any]) -> Path:
    destination = Path(path).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except Exception as exc:
        temporary_path.unlink(missing_ok=True)
        if isinstance(exc, WorkbenchError):
            raise
        raise WorkbenchError(
            "SKW-IO-009",
            f"Failed to write report atomically: {destination}",
            stage="output",
            context={
                "file": str(destination),
                "field": "output",
                "element": "report",
                "reason": str(exc),
            },
        ) from exc
    return destination


def run_workbench(
    *,
    operation_spec: str | Path,
    version_specs: Iterable[str | Path] | None = None,
    output: str | Path,
    base_dir: str | Path | None = None,
    direction: str | None = None,
    batch_budget: int | None = None,
    dense_chunk_size: int | None = None,
    hotspot_multiplier: Any = None,
    tolerance: float | None = None,
) -> dict[str, Any]:
    report = build_report(
        operation_spec=operation_spec,
        version_specs=version_specs,
        base_dir=base_dir,
        direction=direction,
        batch_budget=batch_budget,
        dense_chunk_size=dense_chunk_size,
        hotspot_multiplier=hotspot_multiplier,
        tolerance=tolerance,
    )
    atomic_write_json(output, report)
    return report
