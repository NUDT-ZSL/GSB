"""Four-stage deterministic diagnostics pipeline."""

from __future__ import annotations

import hashlib
import traceback
from typing import Any, Mapping

import numpy as np

from .comparison import compare_outputs, effective_tolerance
from .errors import (
    DiagnosticError,
    E_INTERNAL_UNATTRIBUTED,
    E_NUMERIC_MISMATCH,
    E_NUMERIC_NONFINITE,
    E_PARAM_INVALID,
    E_RESOURCE_LIMIT,
)
from .execution import execute_chunked
from .partition import (
    Layout,
    estimate_resources,
    make_blocks,
    select_layout,
)
from .reference import reference_output
from .sampling import array_sha256, generate_arrays, input_metadata
from .schema import (
    STAGE_COMPARE,
    STAGE_NUMERIC,
    STAGE_PARTITION,
    STAGE_VALIDATION,
    NormalizedJob,
    Tolerance,
    normalize_job,
    validate_tolerance,
)
from .serialization import canonical_json, to_jsonable
from .spectrum import adjudicate_deviations, build_deviation_spectrum


STAGES = (STAGE_VALIDATION, STAGE_PARTITION, STAGE_NUMERIC, STAGE_COMPARE)


def _sha256_array(array: np.ndarray) -> str:
    return array_sha256(array)


def _job_id_of(task: Any) -> str:
    if isinstance(task, Mapping):
        value = task.get("job_id")
        if isinstance(value, str) and value:
            return value
    return "<unknown>"


def _task_with_validation_injection(task: Mapping[str, Any]) -> dict[str, Any]:
    effective = dict(task)
    injection = task.get("injection")
    if not isinstance(injection, Mapping):
        return effective
    trigger = injection.get(STAGE_VALIDATION)
    if trigger == "padding_overflow":
        effective["padding"] = [999, 999, 999, 999]
    return effective


def _explicit_trigger(job: dict[str, Any] | None, stage: str) -> str | None:
    if not isinstance(job, Mapping):
        return None
    injection = job.get("injection")
    if not isinstance(injection, Mapping):
        return None
    trigger = injection.get(stage)
    return str(trigger) if trigger is not None else None


def _infer_trigger(error: DiagnosticError, explicit: str | None) -> str:
    if explicit:
        return explicit
    if error.stage == STAGE_VALIDATION:
        if error.field in {"weight", "kernel_shape"} and "finite" in error.message:
            return "nonfinite_weight"
        if error.field == "input" and "finite" in error.message:
            return "nonfinite_input"
        if error.field == "padding":
            return "padding_overflow"
        if "dtype" in error.field:
            return "unsupported_dtype"
        if "kernel_shape" in error.field or "shape" in error.field:
            return "shape_invalid"
        return "invalid_parameter"
    if error.stage == STAGE_PARTITION:
        if "block size" in error.message:
            return "zero_block_size"
        return "block_resource_limit"
    if error.stage == STAGE_NUMERIC:
        return "nonfinite_output"
    if error.stage == STAGE_COMPARE:
        return "nonpositive_tolerance"
    return "stage_failure"


def _make_injection_record(
    error: DiagnosticError,
    *,
    trigger: str,
    explicit: bool,
) -> dict[str, Any]:
    return {
        "job_id": error.job_id,
        "stage": error.stage,
        "code": error.code,
        "field": error.field,
        "message": error.message,
        "trigger": trigger,
        "mode": "explicit_parameter" if explicit else "natural_invalid_input",
        "details": to_jsonable(error.details),
    }


def _make_business_code(
    error: DiagnosticError,
    *,
    recovered: bool,
    degradation_category: str | None = None,
) -> dict[str, Any]:
    record = to_jsonable(error.as_record())
    record["recovered"] = recovered
    if degradation_category:
        record["degradation_category"] = degradation_category
    return record


def _make_stage_record(
    stage: str,
    status: str,
    *,
    error: DiagnosticError | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {"stage": stage, "status": status}
    if error is not None:
        record.update(
            {
                "code": error.code,
                "field": error.field,
                "message": error.message,
            }
        )
    if note:
        record["note"] = note
    return record


def _single_block_layout(
    job: NormalizedJob, *, dtype: str, reason: str
) -> Layout:
    if job.family == "conv":
        batch = job.output_shape[0]
        out_h, out_w = job.output_shape[2], job.output_shape[3]
    else:
        batch = job.output_shape[0]
        out_h, out_w = job.output_shape[1], job.output_shape[2]
    block_size = (out_h, out_w)
    estimate = estimate_resources(job, block_size=block_size, dtype=dtype)
    blocks = make_blocks(batch, out_h, out_w, block_size)
    return Layout(
        block_size=block_size,
        blocks=tuple(blocks),
        requested_block_size=block_size,
        dtype=dtype,
        estimate=estimate,
        selection_basis=reason,
        resource_events=(),
    )


def _effective_config(job: NormalizedJob, layout: Layout | None) -> dict[str, Any]:
    config = {
        "job_id": job.job_id,
        "op_kind": job.op_kind,
        "family": job.family,
        "interp_mode": job.interp_mode,
        "input_shape_nhwc": list(job.input_shape),
        "kernel_shape_oihw": list(job.kernel_shape) if job.kernel_shape else None,
        "output_shape": list(job.output_shape),
        "stride": list(job.stride),
        "padding": list(job.padding),
        "padding_spec": job.padding_spec,
        "scale": list(job.scale) if job.scale else None,
        "requested_dtype": job.dtype,
        "seed": job.seed,
        "weight_seed": job.weight_seed,
        "block_policy": job.block_policy,
        "tolerance": job.tolerance.as_dict(),
        "resource_limits": job.resource_limits.as_dict(),
        "injection": job.injection,
    }
    if layout is not None:
        config["effective_dtype"] = layout.dtype
    return config


def _fingerprint(
    job: NormalizedJob,
    *,
    input_array: np.ndarray | None,
    weight_array: np.ndarray | None,
    layout: Layout | None,
) -> str:
    payload = {
        "effective_config": _effective_config(job, layout),
        "input_sha256": _sha256_array(input_array) if input_array is not None else None,
        "weight_sha256": _sha256_array(weight_array)
        if weight_array is not None
        else None,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _base_report(task: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "job_id": _job_id_of(task),
        "status": "error",
        "business_codes": [],
        "stage_records": [],
        "injection_records": [],
        "degradation_chain": [],
        "unattributed_exceptions": [],
        "diagnostic_warnings": [],
        "effective_config": None,
        "inputs": None,
        "policy_record": None,
        "resource": None,
        "reference": None,
        "execution": None,
        "comparison": None,
        "deviation_spectrum": None,
        "adjudication": None,
        "reproducibility": None,
    }


def _fatal_result(
    report: dict[str, Any],
    error: DiagnosticError,
    *,
    trigger: str,
    explicit: bool,
    category: str,
    stages_completed: int,
) -> dict[str, Any]:
    report["stage_records"].append(
        _make_stage_record(error.stage, "error", error=error)
    )
    for stage in STAGES[stages_completed + 1 :]:
        report["stage_records"].append(
            _make_stage_record(
                stage,
                "skipped",
                note=f"upstream {error.code} at {error.stage} halted the pipeline",
            )
        )
    report["injection_records"].append(
        _make_injection_record(error, trigger=trigger, explicit=explicit)
    )
    report["business_codes"].append(
        _make_business_code(
            error, recovered=False, degradation_category=category
        )
    )
    report["degradation_chain"].append(
        {
            "job_id": error.job_id,
            "category": category,
            "stage": error.stage,
            "upstream_code": error.code,
            "upstream_field": error.field,
            "action": "pipeline halted; downstream stages marked skipped",
        }
    )
    report["status"] = "error"
    report["primary_code"] = error.code
    return report


def execute_task(
    task: Mapping[str, Any],
    *,
    input_array: np.ndarray | None = None,
    weight_array: np.ndarray | None = None,
) -> tuple[dict[str, Any], np.ndarray | None, np.ndarray | None, str]:
    """Run one task and return report, final output, reference, and source."""

    report = _base_report(task)
    normalized: NormalizedJob | None = None
    actual_output: np.ndarray | None = None
    reference_array: np.ndarray | None = None
    output_source = "none"
    generated_input: np.ndarray | None = None
    generated_weight: np.ndarray | None = None
    layout: Layout | None = None
    completed = 0

    explicit_validation = _explicit_trigger(dict(task), STAGE_VALIDATION)
    try:
        effective_task = _task_with_validation_injection(task)
        normalized = normalize_job(
            effective_task,
            input_array=input_array,
            weight_array=weight_array,
        )
        if input_array is None:
            generated_input, generated_weight = generate_arrays(normalized)
        else:
            generated_input = np.asarray(input_array, dtype=np.float64)
            generated_weight = (
                np.asarray(weight_array, dtype=np.float64)
                if weight_array is not None
                else None
            )
        if normalized.family == "conv" and generated_weight is None:
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                normalized.job_id,
                "weight",
                "convolution tasks require a generated or provided weight array",
            )

        if explicit_validation == "nonfinite_weight":
            if generated_weight is None:
                raise DiagnosticError(
                    E_PARAM_INVALID,
                    STAGE_VALIDATION,
                    normalized.job_id,
                    "weight",
                    "cannot inject nonfinite_weight into interpolation task",
                )
            generated_weight = np.array(generated_weight, copy=True)
            generated_weight.flat[0] = np.nan

        if not np.all(np.isfinite(generated_input)):
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                normalized.job_id,
                "input",
                "input contains non-finite values",
                {
                    "nonfinite_count": int(
                        np.count_nonzero(~np.isfinite(generated_input))
                    )
                },
            )
        if generated_weight is not None and not np.all(np.isfinite(generated_weight)):
            raise DiagnosticError(
                E_PARAM_INVALID,
                STAGE_VALIDATION,
                normalized.job_id,
                "weight",
                "kernel/weight contains non-finite values",
                {
                    "nonfinite_count": int(
                        np.count_nonzero(~np.isfinite(generated_weight))
                    )
                },
            )

        report["effective_config"] = _effective_config(normalized, None)
        report["inputs"] = input_metadata(generated_input, generated_weight)
        report["stage_records"].append(
            _make_stage_record(STAGE_VALIDATION, "ok")
        )
        completed = 1
    except DiagnosticError as error:
        report["effective_config"] = None
        if normalized is not None and generated_input is not None:
            report["effective_config"] = _effective_config(normalized, None)
            report["inputs"] = input_metadata(generated_input, generated_weight)
        trigger = _infer_trigger(error, explicit_validation)
        return (
            to_jsonable(
                _fatal_result(
                    report,
                    error,
                    trigger=trigger,
                    explicit=bool(explicit_validation),
                    category="REJECT_INVALID_INPUT",
                    stages_completed=0,
                )
            ),
            None,
            None,
            "none",
        )
    except Exception as exc:  # noqa: BLE001
        report["unattributed_exceptions"].append(
            {
                "stage": STAGE_VALIDATION,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        report["stage_records"].append(
            _make_stage_record(
                STAGE_VALIDATION,
                "unattributed_exception",
                note=f"{type(exc).__name__}: {exc}",
            )
        )
        report["status"] = "error"
        report["primary_code"] = E_INTERNAL_UNATTRIBUTED
        for stage in STAGES[1:]:
            report["stage_records"].append(
                _make_stage_record(
                    stage,
                    "skipped",
                    note="validation raised an unattributed exception",
                )
            )
        return to_jsonable(report), None, None, "none"

    assert normalized is not None
    assert generated_input is not None
    explicit_partition = _explicit_trigger(dict(task), STAGE_PARTITION)
    partition_policy = normalized.block_policy
    if explicit_partition == "zero_block_size":
        partition_policy = dict(partition_policy)
        partition_policy["block_size"] = [0, 0]
        partition_policy["candidates"] = [[0, 0]]

    try:
        layout = select_layout(normalized, block_policy_override=partition_policy)
        report["stage_records"].append(_make_stage_record(STAGE_PARTITION, "ok"))
    except DiagnosticError as error:
        trigger = _infer_trigger(error, explicit_partition)
        report["injection_records"].append(
            _make_injection_record(
                error, trigger=trigger, explicit=bool(explicit_partition)
            )
        )
        if error.code == E_PARAM_INVALID and (
            explicit_partition == "zero_block_size"
            or "block size" in error.message
        ):
            layout = _single_block_layout(
                normalized,
                dtype=normalized.dtype,
                reason="fallback to one full spatial block per batch image",
            )
            report["business_codes"].append(
                _make_business_code(
                    error,
                    recovered=True,
                    degradation_category="SINGLE_BLOCK_FALLBACK",
                )
            )
            report["degradation_chain"].append(
                {
                    "job_id": normalized.job_id,
                    "category": "SINGLE_BLOCK_FALLBACK",
                    "stage": STAGE_PARTITION,
                    "upstream_code": error.code,
                    "upstream_field": error.field,
                    "action": (
                        "retried with one full output block per batch image; "
                        "the original edge clipping and boundary policy remain active"
                    ),
                }
            )
            report["stage_records"].append(
                _make_stage_record(
                    STAGE_PARTITION,
                    "degraded",
                    error=error,
                    note="SINGLE_BLOCK_FALLBACK",
                )
            )
        else:
            return (
                to_jsonable(
                    _fatal_result(
                        report,
                        error,
                        trigger=trigger,
                        explicit=bool(explicit_partition),
                        category="REJECT_RESOURCE_LIMIT"
                        if error.code == E_RESOURCE_LIMIT
                        else "REJECT_PARTITION_FAILURE",
                        stages_completed=1,
                    )
                ),
                None,
                None,
                "none",
            )
    except Exception as exc:  # noqa: BLE001
        report["unattributed_exceptions"].append(
            {
                "stage": STAGE_PARTITION,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        layout = _single_block_layout(
            normalized,
            dtype=normalized.dtype,
            reason="single-block fallback after unattributed partition exception",
        )
        report["degradation_chain"].append(
            {
                "job_id": normalized.job_id,
                "category": "SINGLE_BLOCK_FALLBACK",
                "stage": STAGE_PARTITION,
                "upstream_code": E_INTERNAL_UNATTRIBUTED,
                "upstream_field": None,
                "action": "unattributed partition exception; retried as one block",
            }
        )
        report["stage_records"].append(
            _make_stage_record(
                STAGE_PARTITION,
                "degraded",
                note="single-block fallback after unattributed exception",
            )
        )

    assert layout is not None
    report["effective_config"] = _effective_config(normalized, layout)
    report["policy_record"] = {
        "shared_block_grid": True,
        "layout": layout.as_dict(),
        "boundary_rules": {
            "conv2d": "explicit zero padding outside the source image",
            "interpolation": "clamp source/neighbor indices inside the zero-padded image",
            "edge_blocks": "block ranges are clipped to the output shape",
        },
    }
    report["resource"] = layout.estimate
    report["fingerprint"] = _fingerprint(
        normalized,
        input_array=generated_input,
        weight_array=generated_weight,
        layout=layout,
    )
    for event in layout.resource_events:
        category = event["category"]
        report["business_codes"].append(
            {
                "job_id": normalized.job_id,
                "code": event["code"],
                "stage": event["stage"],
                "field": "resource_limits",
                "message": "resource limit required an adaptive layout/dtype choice",
                "details": to_jsonable(event),
                "recovered": True,
                "degradation_category": category,
            }
        )
        report["degradation_chain"].append(
            {
                "job_id": normalized.job_id,
                "category": category,
                "stage": event["stage"],
                "upstream_code": event["code"],
                "upstream_field": "resource_limits",
                "action": (
                    f"selected block={event.get('effective_block_size')} "
                    f"dtype={event.get('effective_dtype', layout.dtype)}"
                ),
            }
        )
    if layout.resource_events:
        report["stage_records"].append(
            _make_stage_record(
                STAGE_PARTITION,
                "degraded",
                note="; ".join(
                    str(event["category"]) for event in layout.resource_events
                ),
            )
        )

    try:
        reference_array = reference_output(
            normalized, generated_input, generated_weight
        )
    except Exception as exc:  # noqa: BLE001
        report["unattributed_exceptions"].append(
            {
                "stage": STAGE_NUMERIC,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        report["stage_records"].append(
            _make_stage_record(
                STAGE_NUMERIC,
                "unattributed_exception",
                note=f"reference generation failed: {type(exc).__name__}: {exc}",
            )
        )
        for stage in STAGES[3:]:
            report["stage_records"].append(
                _make_stage_record(
                    stage,
                    "skipped",
                    note="independent reference generation failed",
                )
            )
        report["status"] = "error"
        report["primary_code"] = E_INTERNAL_UNATTRIBUTED
        return to_jsonable(report), None, None, "none"

    explicit_numeric = _explicit_trigger(dict(task), STAGE_NUMERIC)
    attempted_sha: str | None = None
    attempted_nonfinite = 0
    attempted_locations: list[dict[str, Any]] = []
    execution_stats = None
    numeric_error: DiagnosticError | None = None
    try:
        execution_stats = execute_chunked(
            normalized, generated_input, generated_weight, layout
        )
        actual_output = execution_stats.output
        if explicit_numeric == "nonfinite_output":
            actual_output = np.array(actual_output, copy=True)
            actual_output.flat[min(3, actual_output.size - 1)] = np.nan
        nonfinite_mask = ~np.isfinite(actual_output)
        attempted_nonfinite = int(np.count_nonzero(nonfinite_mask))
        if attempted_nonfinite:
            for flat_index in np.flatnonzero(nonfinite_mask.ravel())[:16]:
                index = np.unravel_index(int(flat_index), actual_output.shape)
                attempted_locations.append(
                    {
                        "flat_index": int(flat_index),
                        "index": [int(item) for item in index],
                    }
                )
            numeric_error = DiagnosticError(
                E_NUMERIC_NONFINITE,
                STAGE_NUMERIC,
                normalized.job_id,
                "output",
                "chunked numeric output contains non-finite values",
                {
                    "nonfinite_count": attempted_nonfinite,
                    "locations": attempted_locations,
                },
            )
    except Exception as exc:  # noqa: BLE001
        report["unattributed_exceptions"].append(
            {
                "stage": STAGE_NUMERIC,
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
        numeric_error = DiagnosticError(
            E_INTERNAL_UNATTRIBUTED,
            STAGE_NUMERIC,
            normalized.job_id,
            "execution",
            f"unattributed chunk execution exception: {type(exc).__name__}: {exc}",
        )

    if actual_output is not None and numeric_error is None:
        attempted_sha = _sha256_array(actual_output)
        actual_output = actual_output.astype(np.float64, copy=False)
        report["stage_records"].append(_make_stage_record(STAGE_NUMERIC, "ok"))
        output_source = "chunked"
    elif numeric_error is not None:
        if actual_output is not None:
            attempted_sha = _sha256_array(actual_output)
        actual_output = reference_array.copy()
        output_source = "reference_fallback"
        trigger = _infer_trigger(numeric_error, explicit_numeric)
        report["injection_records"].append(
            _make_injection_record(
                numeric_error, trigger=trigger, explicit=bool(explicit_numeric)
            )
        )
        report["business_codes"].append(
            _make_business_code(
                numeric_error,
                recovered=True,
                degradation_category="REFERENCE_FALLBACK",
            )
        )
        report["degradation_chain"].append(
            {
                "job_id": normalized.job_id,
                "category": "REFERENCE_FALLBACK",
                "stage": STAGE_NUMERIC,
                "upstream_code": numeric_error.code,
                "upstream_field": numeric_error.field,
                "action": (
                    "discarded the invalid chunk output and used the independent "
                    "float64 element-wise reference as the degraded result"
                ),
            }
        )
        report["stage_records"].append(
            _make_stage_record(
                STAGE_NUMERIC,
                "degraded",
                error=numeric_error,
                note="REFERENCE_FALLBACK",
            )
        )

    assert actual_output is not None
    assert reference_array is not None
    explicit_compare = _explicit_trigger(dict(task), STAGE_COMPARE)
    selected_tolerance = normalized.tolerance
    if explicit_compare == "nonpositive_tolerance":
        selected_tolerance = Tolerance(atol=0.0, rtol=-1.0)
    tolerance_source = "task"
    try:
        validate_tolerance(selected_tolerance, job_id=normalized.job_id)
        report["stage_records"].append(
            _make_stage_record(STAGE_COMPARE, "ok", note="tolerance validated")
        )
    except DiagnosticError as error:
        trigger = _infer_trigger(error, explicit_compare)
        selected_tolerance = Tolerance()
        tolerance_source = "default_after_invalid_task_tolerance"
        report["injection_records"].append(
            _make_injection_record(
                error, trigger=trigger, explicit=bool(explicit_compare)
            )
        )
        report["business_codes"].append(
            _make_business_code(
                error,
                recovered=True,
                degradation_category="DEFAULT_TOLERANCE",
            )
        )
        report["degradation_chain"].append(
            {
                "job_id": normalized.job_id,
                "category": "DEFAULT_TOLERANCE",
                "stage": STAGE_COMPARE,
                "upstream_code": error.code,
                "upstream_field": error.field,
                "action": "fell back to atol=1e-12, rtol=1e-9 before comparison",
            }
        )
        report["stage_records"].append(
            _make_stage_record(
                STAGE_COMPARE,
                "degraded",
                error=error,
                note="DEFAULT_TOLERANCE",
            )
        )

    effective_tol, tolerance_meta = effective_tolerance(
        selected_tolerance, dtype=layout.dtype
    )
    comparison = compare_outputs(
        normalized,
        actual_output,
        reference_array,
        effective_tol,
        max_records=normalized.max_violation_records,
    )
    comparison["tolerance_source"] = tolerance_source
    comparison["dtype_tolerance"] = tolerance_meta
    report["comparison"] = comparison
    if not comparison["within_tolerance"]:
        mismatch_error = DiagnosticError(
            E_NUMERIC_MISMATCH,
            STAGE_COMPARE,
            normalized.job_id,
            "output",
            "chunked output exceeded the configured reference tolerance",
            {
                "violation_count": comparison["violation_count"],
                "max_abs_error": comparison["max_abs_error"],
                "max_rel_error": comparison["max_rel_error"],
            },
        )
        report["business_codes"].append(
            _make_business_code(
                mismatch_error,
                recovered=False,
                degradation_category=None,
            )
        )
        report["stage_records"].append(
            _make_stage_record(
                STAGE_COMPARE, "error", error=mismatch_error
            )
        )
        report["status"] = "error"
        report["primary_code"] = E_NUMERIC_MISMATCH
    elif report["business_codes"]:
        report["status"] = "degraded"
        report["primary_code"] = report["business_codes"][0]["code"]
    else:
        report["status"] = "ok"
        report["primary_code"] = None

    spectrum = build_deviation_spectrum(
        normalized,
        actual=actual_output,
        reference=reference_array,
        input_array=generated_input,
        weight_array=generated_weight,
        layout=layout,
        comparison=comparison,
    )
    report["deviation_spectrum"] = spectrum
    report["adjudication"] = adjudicate_deviations(spectrum)
    if not report["adjudication"]["verified"]:
        report["diagnostic_warnings"].append(
            {
                "code": "W_SPECTRUM_DETAIL_INCOMPLETE",
                "message": (
                    "deviation detail arrays were truncated or did not reproduce "
                    "the declared summaries"
                ),
                "mismatches": report["adjudication"]["mismatches"],
            }
        )

    report["reference"] = {
        "dtype": "float64",
        "shape": list(reference_array.shape),
        "sha256": _sha256_array(reference_array),
        "implementation": "independent_elementwise_float64",
        "nonfinite_count": int(np.count_nonzero(~np.isfinite(reference_array))),
    }
    report["execution"] = {
        "output_source": output_source,
        "effective_dtype": layout.dtype,
        "requested_dtype": normalized.dtype,
        "output_shape": list(actual_output.shape),
        "output_dtype": str(actual_output.dtype),
        "sha256": _sha256_array(actual_output),
        "attempted_chunk_sha256": attempted_sha,
        "attempted_nonfinite_count": attempted_nonfinite,
        "attempted_nonfinite_locations": attempted_locations,
        "boundary_policy": execution_stats.boundary_policy
        if execution_stats is not None
        else None,
        "block_records": list(execution_stats.block_records)
        if execution_stats is not None
        else None,
    }
    report["reproducibility"] = {
        "task_fingerprint_sha256": report["fingerprint"],
        "reference_output_sha256": report["reference"]["sha256"],
        "final_output_sha256": report["execution"]["sha256"],
        "seed_derived_input_sha256": report["inputs"]["input_sha256"],
        "byte_reproducibility_scope": (
            "seeded arrays, float64 reference bytes, execution output bytes, "
            "and canonical report content excluding no nondeterministic fields"
        ),
    }
    return (
        to_jsonable(report),
        actual_output,
        reference_array,
        output_source,
    )
