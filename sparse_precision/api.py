"""Unified public entry points for SpMM, SpSpMM and diagnostics."""

from __future__ import annotations

from typing import Any

import numpy as np

from .codes import (
    E_CONFIG_INVALID,
    E_CSR_INVALID,
    E_INCONSISTENT,
    E_SHAPE_INVALID,
    E_TOLERANCE_EXCEEDED,
    OK,
)
from .consistency import run_consistency_checks
from .diagnostics import diagnose_error_spectrum
from .errors import SparseOperatorError
from .fingerprint import bytes_fingerprint
from .generator import (
    build_operands,
    densify_reference,
    reference_checksum,
)
from .kernel import ExecutionOutcome, execute_batches
from .models import CaseSpec, Operands, OperatorConfig
from .strategy import decide_strategy_for_spec
from .validation import (
    normalize_operands,
    parse_case_spec,
    validate_operands,
    validate_reference,
)


def _stage_metrics(outcome: ExecutionOutcome) -> dict[str, Any]:
    return {
        "sparse_index_summation": {
            "value": outcome.local_max_abs_error,
            "position": outcome.local_max_position,
            "method": outcome.stage_methods["sparse_index_summation"],
        },
        "cross_block_reduction": {
            "value": outcome.cross_reduction_max_abs_error,
            "position": outcome.cross_reduction_max_position,
            "method": outcome.stage_methods["cross_block_reduction"],
        },
        "row_block": {
            "value": 0.0,
            "position": None,
            "method": outcome.stage_methods["row_block"],
        },
    }


def _result_summary(result: np.ndarray) -> dict[str, Any]:
    finite_result = np.asarray(result, dtype=np.float64)
    return {
        "shape": [int(item) for item in finite_result.shape],
        "dtype": "float64",
        "checksum": bytes_fingerprint(finite_result),
        "min": float(finite_result.min()) if finite_result.size else 0.0,
        "max": float(finite_result.max()) if finite_result.size else 0.0,
        "max_abs": float(np.max(np.abs(finite_result)))
        if finite_result.size
        else 0.0,
        "nonzero_count": int(np.count_nonzero(finite_result)),
    }


def _base_report(
    spec: CaseSpec,
    config: OperatorConfig,
    input_summary: dict[str, Any],
    terrain: dict[str, Any],
) -> dict[str, Any]:
    return {
        "case_id": spec.case_id,
        "kind": spec.kind,
        "shape_mnk": [spec.m, spec.k, spec.n],
        "batch_size": spec.batch_size,
        "dtype": spec.dtype,
        "seed": spec.seed,
        "config": config.to_dict(),
        "input_summary": {
            "batch_size": spec.batch_size,
            "nnz_a": int(input_summary["a_nnz"]),
            "nnz_b": int(spec.nnz_b),
            "b_storage": spec.b_storage,
            "b_nnz_or_entries": int(input_summary["b_nnz_or_entries"]),
            "block_candidates": list(spec.block_candidates),
        },
        "truth_source": {
            "mode": "dense float64 reference",
            "reconstruction": "same seed and JSON metadata",
            "blocked": False,
            "compensated": False,
            "reference_checksum_if_available": spec.checksums.get("reference"),
        },
        "terrain": terrain,
    }


def _execute_spec(
    spec: CaseSpec,
    operands: Operands,
    config: OperatorConfig,
    input_summary: dict[str, Any],
    *,
    reference: np.ndarray | None,
    include_result: bool,
) -> dict[str, Any]:
    plan = decide_strategy_for_spec(spec, operands, config)
    truth = densify_reference(spec, operands)
    reference_source = "reconstructed"
    if reference is not None:
        supplied_truth = validate_reference(spec, reference)
        if not np.array_equal(truth, supplied_truth):
            maximum_difference = float(np.max(np.abs(truth - supplied_truth)))
            raise SparseOperatorError(
                E_CSR_INVALID,
                "caller reference is not bitwise identical to the reconstructed "
                "dense float64 reference",
                case_id=spec.case_id,
                phase="reconstruction",
                field="reference",
                details={"max_abs_difference": maximum_difference},
            )
        reference_source = "reconstructed_verified_caller_snapshot"
    expected_reference_checksum = spec.checksums.get("reference")
    actual_reference_checksum = reference_checksum(truth)
    if (
        expected_reference_checksum is not None
        and str(expected_reference_checksum) != actual_reference_checksum
    ):
        raise SparseOperatorError(
            E_CSR_INVALID,
            "dense reference does not match the metadata checksum",
            case_id=spec.case_id,
            phase="reconstruction",
            field="checksums.reference",
            details={
                "expected": expected_reference_checksum,
                "actual": actual_reference_checksum,
            },
        )

    try:
        outcome = execute_batches(
            spec, operands, plan, config, reference=truth
        )
    except SparseOperatorError as error:
        error.details.setdefault("strategy", plan)
        raise
    diagnostics = diagnose_error_spectrum(
        truth,
        outcome.result,
        config,
        case_id=spec.case_id,
        stage_metrics=_stage_metrics(outcome),
    )
    diagnostics["stage_attribution"]["envelope"] = {
        "value": outcome.row_block_envelope_max_abs_error,
        "position": outcome.row_block_envelope_position,
        "method": "block output versus full reference rows (context, not double-counted)",
    }
    diagnostics["reduction_recompute_max_abs_diff"] = (
        outcome.reduction_recompute_max_abs_error
    )

    consistency = run_consistency_checks(
        spec, operands, outcome, plan, config
    )
    terrain = {"reference_source": reference_source}
    report: dict[str, Any] = {
        **_base_report(spec, config, input_summary, terrain),
        "strategy": plan,
        "execution": {
            "batch_size": spec.batch_size,
            "nnz_a_processed": outcome.nnz_processed,
            "nnz_a_declared": spec.nnz_a,
            "nnz_b_processed": outcome.nnz_b_processed,
            "nnz_b_declared": spec.nnz_b,
            "coverage_ok": outcome.coverage_ok,
            "actual_partial_bytes": outcome.actual_partial_bytes,
            "aggregation_entries": outcome.aggregation_entries,
            "partial_checksum": _partial_checksum(outcome),
            "stage_methods": outcome.stage_methods,
        },
        "result_summary": _result_summary(outcome.result),
        "diagnostics": diagnostics,
        "consistency": consistency,
        "degradation": plan["degradation"],
    }
    if include_result:
        report["result"] = outcome.result.tolist()

    code = OK
    phase = "complete"
    field = None
    message = "all checks passed"
    details: dict[str, Any] | None = None
    if not diagnostics["passed"]:
        code = E_TOLERANCE_EXCEEDED
        phase = "diagnostics"
        field = "result"
        message = "one or more output elements exceed the configured tolerance"
        details = {
            "violation_count": diagnostics["violation_count"],
            "worst_violations": diagnostics["worst_violations"],
        }
    elif not outcome.coverage_ok:
        code = E_INCONSISTENT
        phase = "execution"
        field = "nnz_coverage"
        message = "CSR nonzeros were not fully covered by the selected block grid"
        details = {
            "nnz_a_processed": outcome.nnz_processed,
            "nnz_a_declared": spec.nnz_a,
            "nnz_b_processed": outcome.nnz_b_processed,
            "nnz_b_declared": spec.nnz_b,
            "category": "csr_coverage",
        }
    elif consistency["enabled"] and not consistency["passed"]:
        code = E_INCONSISTENT
        phase = "consistency"
        field = "self_check"
        message = "cross-mode consistency checks failed"
        details = {
            "failed_categories": consistency["failed_categories"],
            "checks": consistency["checks"],
        }

    report.update(
        {
            "ok": code == OK,
            "code": code,
            "business_code": code,
            "phase": phase,
            "field": field,
            "message": message,
        }
    )
    if details:
        report["details"] = details
    return report


def _partial_checksum(outcome: ExecutionOutcome) -> str:
    import hashlib

    digest = hashlib.sha256()
    for batch in outcome.partials:
        for row_block in batch:
            for partial in row_block:
                array = np.ascontiguousarray(partial, dtype="<f8")
                digest.update(np.asarray(array.shape, dtype="<i8").tobytes())
                digest.update(array.tobytes())
    return digest.hexdigest()


def execute_case(
    metadata: dict[str, Any],
    a: Any | None = None,
    b: Any | None = None,
    config: OperatorConfig | dict[str, Any] | None = None,
    *,
    reference: np.ndarray | None = None,
    include_result: bool = False,
) -> dict[str, Any]:
    case_id = metadata.get("case_id") if isinstance(metadata, dict) else None
    try:
        if config is None:
            config = OperatorConfig()
        elif isinstance(config, dict):
            config = OperatorConfig.from_mapping(config)
        elif not isinstance(config, OperatorConfig):
            raise SparseOperatorError(
                E_CONFIG_INVALID,
                "config must be an OperatorConfig or JSON object",
                case_id=case_id,
                phase="configuration",
                field="config",
            )
        config.validate()
        spec = parse_case_spec(metadata)
        if a is None and b is None:
            operands = build_operands(spec)
        elif a is None or b is None:
            raise SparseOperatorError(
                E_SHAPE_INVALID,
                "both operands must be supplied together",
                case_id=spec.case_id,
                phase="validation",
                field="operands",
            )
        else:
            operands = normalize_operands(spec, a, b)
        input_summary = validate_operands(spec, operands)
        return _execute_spec(
            spec,
            operands,
            config,
            input_summary,
            reference=reference,
            include_result=include_result,
        )
    except SparseOperatorError as error:
        payload = error.to_payload()
        if payload.get("case_id") is None and case_id is not None:
            payload["case_id"] = case_id
        payload["config"] = (
            config.to_dict()
            if isinstance(config, OperatorConfig)
            else config
        )
        return payload


def spmm(
    metadata: dict[str, Any],
    a: Any | None = None,
    dense_b: Any | None = None,
    config: OperatorConfig | dict[str, Any] | None = None,
    *,
    reference: np.ndarray | None = None,
    include_result: bool = False,
) -> dict[str, Any]:
    return execute_case(
        metadata,
        a,
        dense_b,
        config,
        reference=reference,
        include_result=include_result,
    )


def spspmm(
    metadata: dict[str, Any],
    a: Any | None = None,
    b: Any | None = None,
    config: OperatorConfig | dict[str, Any] | None = None,
    *,
    reference: np.ndarray | None = None,
    include_result: bool = False,
) -> dict[str, Any]:
    return execute_case(
        metadata,
        a,
        b,
        config,
        reference=reference,
        include_result=include_result,
    )

