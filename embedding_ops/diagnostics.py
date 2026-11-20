"""Stage-wise error spectra, self-checks, and structured error injection."""

from __future__ import annotations

import traceback
from dataclasses import replace
from typing import Any, Callable

import numpy as np

from .config import NumericsConfig
from .errors import (
    BusinessError,
    E_DUP_JOB,
    E_INDEX_OUT_OF_RANGE,
    business_error,
)
from .indexing import IndexValidation
from .models import JobSpec
from .numeric import protect_subnormal, round_to_step
from .quantize import TIERS


def _position_list(index: int, shape: tuple[int, ...]) -> list[int]:
    return [int(part) for part in np.unravel_index(int(index), shape)]


def _tier_resolution(
    table_dtype: str,
    actual: np.ndarray,
    quant_scale: float,
    config: NumericsConfig,
    stage: str,
) -> tuple[float, str]:
    if (
        stage == "accumulate"
        and config.accum_rounding_enabled
        and config.accum_rounding_step > 0.0
    ):
        return float(config.accum_rounding_step), "accumulation_rounding_step"
    tier = TIERS.get(table_dtype)
    maximum = float(np.max(np.abs(actual))) if actual.size else 0.0
    if tier is not None and tier.kind == "quantized":
        return float(quant_scale), "quantization_step"
    if table_dtype == "float32":
        return (
            float(np.finfo(np.float32).eps * max(1.0, maximum)),
            "float32_precision_at_scale",
        )
    return (
        float(np.finfo(np.float64).eps * max(1.0, maximum)),
        "float64_precision_at_scale",
    )


def _stage_stats(
    *,
    stage: str,
    actual: np.ndarray,
    reference: np.ndarray,
    table_dtype: str,
    quant_scale: float,
    config: NumericsConfig,
) -> dict[str, Any]:
    actual_values = np.asarray(actual, dtype=np.float64)
    reference_values = np.asarray(reference, dtype=np.float64)
    if actual_values.shape != reference_values.shape:
        raise ValueError(
            f"stage {stage}: shape mismatch {actual_values.shape} "
            f"vs {reference_values.shape}"
        )
    difference = actual_values - reference_values
    absolute = np.abs(difference)
    resolution, resolution_method = _tier_resolution(
        table_dtype,
        actual_values,
        quant_scale,
        config,
        stage,
    )
    flat_index = int(np.argmax(absolute)) if absolute.size else 0
    max_abs = float(absolute.reshape(-1)[flat_index]) if absolute.size else 0.0
    denominator = np.maximum(np.abs(reference_values), resolution)
    relative = np.divide(
        absolute,
        denominator,
        out=np.zeros_like(absolute),
        where=denominator > 0.0,
    )
    relative_index = (
        int(np.argmax(relative.reshape(-1))) if relative.size else 0
    )
    max_rel = (
        float(relative.reshape(-1)[relative_index])
        if relative.size
        else 0.0
    )
    interval = (
        [float(difference.min()), float(difference.max())]
        if difference.size
        else [0.0, 0.0]
    )
    return {
        "stage": stage,
        "shape": list(actual_values.shape),
        "max_abs_deviation": max_abs,
        "max_rel_deviation": max_rel,
        "max_rel_deviation_position": _position_list(
            relative_index, actual_values.shape
        ),
        "error_interval": interval,
        "error_interval_ordered": bool(interval[0] <= interval[1]),
        "max_deviation_position": _position_list(
            flat_index, actual_values.shape
        ),
        "max_deviation_actual": (
            float(actual_values.reshape(-1)[flat_index])
            if actual_values.size
            else 0.0
        ),
        "max_deviation_reference": (
            float(reference_values.reshape(-1)[flat_index])
            if reference_values.size
            else 0.0
        ),
        "observed_resolution": resolution,
        "resolution_method": resolution_method,
        "relative_denominator_policy": (
            "max(abs(reference), observed_resolution)"
        ),
    }


def _ratio(current: float, previous: float, *, first: bool) -> float | str | None:
    if previous > 0.0:
        return float(current / previous)
    if current > 0.0:
        return "inf"
    return None if first else 1.0


def build_error_spectrum(
    *,
    job_id: str,
    table_dtype: str,
    dequantized_table: np.ndarray,
    reference_table: np.ndarray,
    gathered_actual: np.ndarray,
    gathered_reference: np.ndarray,
    reduced_actual: np.ndarray,
    reduced_reference: np.ndarray,
    output_actual: np.ndarray,
    output_reference: np.ndarray,
    output_without_accum_rounding: np.ndarray | None = None,
    reduction_weights_actual: np.ndarray | None = None,
    reduction_weights_reference: np.ndarray | None = None,
    config: NumericsConfig,
    quant_scale: float,
) -> dict[str, Any]:
    """Compare every pipeline stage against the float64 reference path."""

    stages = [
        _stage_stats(
            stage="dequantize",
            actual=dequantized_table,
            reference=reference_table,
            table_dtype=table_dtype,
            quant_scale=quant_scale,
            config=config,
        ),
        _stage_stats(
            stage="gather",
            actual=gathered_actual,
            reference=gathered_reference,
            table_dtype=table_dtype,
            quant_scale=quant_scale,
            config=config,
        ),
        _stage_stats(
            stage="reduce",
            actual=reduced_actual,
            reference=reduced_reference,
            table_dtype=table_dtype,
            quant_scale=quant_scale,
            config=config,
        ),
        _stage_stats(
            stage="accumulate",
            actual=output_actual,
            reference=output_reference,
            table_dtype=table_dtype,
            quant_scale=quant_scale,
            config=config,
        ),
    ]
    previous_max = 0.0
    dequant_peak = stages[0]["max_abs_deviation"]
    for position, stage in enumerate(stages):
        current_max = float(stage["max_abs_deviation"])
        stage["error_increment"] = float(current_max - previous_max)
        stage["amplification_ratio"] = _ratio(
            current_max,
            previous_max,
            first=position == 0,
        )
        stage["cumulative_amplification_ratio"] = _ratio(
            current_max,
            float(dequant_peak),
            first=position == 0,
        )
        previous_max = current_max

    by_stage = {stage["stage"]: stage for stage in stages}
    base_output = (
        np.asarray(output_without_accum_rounding, dtype=np.float64)
        if output_without_accum_rounding is not None
        else np.asarray(output_actual, dtype=np.float64)
    )
    actual_output = np.asarray(output_actual, dtype=np.float64)
    reference_output = np.asarray(output_reference, dtype=np.float64)
    dequant_contribution = float(
        np.max(np.abs(base_output - reference_output))
    )
    accumulation_contribution = float(
        np.max(np.abs(actual_output - base_output))
    )
    if (
        reduction_weights_actual is None
        or reduction_weights_reference is None
    ):
        reduction_weight_delta = 0.0
    else:
        reduction_weight_delta = float(
            np.max(
                np.abs(
                    np.asarray(reduction_weights_actual, dtype=np.float64)
                    - np.asarray(
                        reduction_weights_reference, dtype=np.float64
                    )
                )
            )
        )
    reduction_contribution = 0.0
    contributions = [
        ("dequantization", "dequantize", dequant_contribution),
        ("reduction_weights", "reduce", reduction_contribution),
        ("weighted_accumulation", "accumulate", accumulation_contribution),
    ]
    dominant_source = "none"
    dominant_stage = "none"
    dominant_contribution = 0.0
    for source, stage, value in contributions:
        if value > dominant_contribution:
            dominant_source = source
            dominant_stage = stage
            dominant_contribution = value

    final_abs = np.abs(
        np.asarray(output_actual, dtype=np.float64)
        - np.asarray(output_reference, dtype=np.float64)
    )
    tolerance = (
        config.tolerance_abs
        + config.tolerance_rel
        * np.maximum(1.0, np.abs(np.asarray(output_reference, dtype=np.float64)))
    )
    within_tolerance = bool(np.all(final_abs <= tolerance))
    return {
        "job_id": job_id,
        "table_dtype": table_dtype,
        "stage_order": ["dequantize", "gather", "reduce", "accumulate"],
        "stages": stages,
        "source_contributions": {
            "dequantization": dequant_contribution,
            "reduction_weights": reduction_contribution,
            "weighted_accumulation": accumulation_contribution,
        },
        "dominant_source": dominant_source,
        "dominant_stage": dominant_stage,
        "dominant_contribution": dominant_contribution,
        "dominant_rule": (
            "largest isolated output contribution; dequantization is the "
            "actual-weight no-accumulation-rounding path minus the float64 "
            "reference, accumulation is the final path minus that base path; "
            "reduction weight coefficients are identical to the reference "
            "path, so their isolated contribution is zero unless their "
            "recorded delta becomes nonzero; ties follow source order"
        ),
        "attribution_basis": {
            "dequantization": (
                "max(abs(output_with_actual_weights_without_accum_rounding "
                "- float64_reference_output))"
            ),
            "reduction_weights": (
                "maximum absolute coefficient delta; current reference and "
                "actual reduction masks/coefficients are identical"
            ),
            "weighted_accumulation": (
                "max(abs(final_output - output_with_actual_weights_without_"
                "accum_rounding))"
            ),
        },
        "reduction_weight_delta_max_abs": reduction_weight_delta,
        "dequant_only_final_max_abs_deviation": dequant_contribution,
        "accumulation_rounding_delta_max_abs": accumulation_contribution,
        "dominance_trace": [
            {
                "source": source,
                "stage": stage,
                "contribution": value,
                "stage_max_abs_deviation": by_stage[stage]["max_abs_deviation"],
                "isolated_measure": (
                    "output_path_delta"
                    if source != "reduction_weights"
                    else "reduction_coefficient_delta"
                ),
            }
            for source, stage, value in contributions
        ],
        "final_max_abs_deviation": float(final_abs.max())
        if final_abs.size
        else 0.0,
        "within_tolerance": within_tolerance,
        "tolerance": config.to_dict()["tolerance"],
    }


def _recompute_output(
    table: np.ndarray,
    reduction: Any,
    config: NumericsConfig,
) -> np.ndarray:
    embedding = np.asarray(table, dtype=np.float64)
    tokens = len(reduction.token_weight_details)
    output_dim = int(reduction.output.shape[1])
    recomputed = np.zeros((tokens, output_dim), dtype=np.float64)
    for token_detail in reduction.token_weight_details:
        token = int(token_detail["token"])
        candidates = token_detail["candidates"]
        if reduction.reduction_mode == "concat":
            for candidate in candidates:
                index = int(candidate["index"])
                combined = np.asarray(
                    candidate["combined_weight"], dtype=np.float64
                )
                start, end = [int(value) for value in candidate["output_slice"]]
                block = embedding[index, :] * combined
                if (
                    config.accum_rounding_enabled
                    and config.accum_rounding_step > 0.0
                ):
                    block = round_to_step(
                        block,
                        config.accum_rounding_step,
                        config.rounding_mode,
                    )
                recomputed[token, start:end] = protect_subnormal(
                    block,
                    config.subnormal_policy,
                    config.subnormal_threshold,
                )
        else:
            partial = np.zeros(output_dim, dtype=np.float64)
            for candidate in candidates:
                index = int(candidate["index"])
                combined = np.asarray(
                    candidate["combined_weight"], dtype=np.float64
                )
                partial = partial + embedding[index, :] * combined
                if (
                    config.accum_rounding_enabled
                    and config.accum_rounding_step > 0.0
                ):
                    partial = round_to_step(
                        partial,
                        config.accum_rounding_step,
                        config.rounding_mode,
                    )
                partial = protect_subnormal(
                    partial,
                    config.subnormal_policy,
                    config.subnormal_threshold,
                )
            recomputed[token, :] = partial
    return recomputed


def self_check_job(
    *,
    job_id: str,
    validation: IndexValidation,
    actual: Any,
    embedding_table: np.ndarray,
    config: NumericsConfig,
    spectrum: dict[str, Any],
) -> dict[str, Any]:
    """Cross-module consistency checks required by the integration contract."""

    checks: list[dict[str, Any]] = []
    validation_positions = list(validation.token_positions)
    reduction_positions = list(actual.token_positions)
    output_positions = list(range(int(actual.output.shape[0])))
    positions_consistent = (
        validation_positions == reduction_positions == output_positions
        and int(actual.gathered.shape[0]) == len(output_positions)
    )
    checks.append(
        {
            "name": "token_position_sets_consistent",
            "passed": bool(positions_consistent),
            "detail": {
                "index_validation": validation_positions,
                "gather": list(range(int(actual.gathered.shape[0]))),
                "reduce_and_weight": reduction_positions,
                "accumulate": output_positions,
            },
        }
    )
    interval_rows = {
        stage["stage"]: stage["error_interval"] for stage in spectrum["stages"]
    }
    intervals_ordered = all(
        float(interval[0]) <= float(interval[1])
        for interval in interval_rows.values()
    )
    checks.append(
        {
            "name": "error_intervals_ordered",
            "passed": bool(intervals_ordered),
            "detail": interval_rows,
        }
    )
    recomputed = _recompute_output(embedding_table, actual, config)
    recompute_diff = float(
        np.max(np.abs(recomputed - actual.output))
        if actual.output.size
        else 0.0
    )
    rewrite_tolerance = float(
        config.tolerance_abs
        + config.tolerance_rel
        * max(1.0, float(np.max(np.abs(actual.output))))
    )
    recompute_consistent = bool(recompute_diff <= rewrite_tolerance)
    checks.append(
        {
            "name": "output_recomputable_from_table_and_weights",
            "passed": recompute_consistent,
            "detail": {
                "max_abs_recompute_diff": recompute_diff,
                "comparison_tolerance": rewrite_tolerance,
                "tolerance_policy": config.tolerance_policy,
                "source": "token_weight_details + dequantized_table",
            },
        }
    )
    return {
        "job_id": job_id,
        "passed": all(check["passed"] for check in checks),
        "checks": checks,
    }


def _capture(
    *,
    injection: dict[str, Any],
    action: Callable[[], Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        action()
    except BusinessError as exc:
        record = {
            "injection": injection,
            "job_id": exc.job_id,
            "result": "error_raised",
            "business_codes": [exc.code],
            **exc.to_dict(),
        }
        return record, None
    except Exception as exc:  # noqa: BLE001 - report, never silently swallow
        unattributed = {
            "injection": injection,
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        return None, unattributed
    return (
        {
            "injection": injection,
            "job_id": injection.get("job_id", "unknown"),
            "result": "error_not_raised",
            "business_codes": [],
            "code": None,
            "stage": None,
            "message": "injection did not raise a business error",
            "detail": {},
        },
        None,
    )


def _duplicate_job_action(spec: Any) -> Callable[[], None]:
    def action() -> None:
        records = [spec.to_dict(), spec.to_dict()]
        seen: set[str] = set()
        for position, record in enumerate(records):
            job_id = record["job_id"]
            if job_id in seen:
                raise business_error(
                    E_DUP_JOB,
                    "manifest",
                    job_id,
                    f"duplicate job_id in manifest: {job_id!r}",
                    first_occurrence=0,
                    duplicate_position=position,
                )
            seen.add(job_id)

    return action


def run_error_injection_suite(
    base_spec: JobSpec | dict[str, Any],
    config: NumericsConfig | None = None,
) -> dict[str, Any]:
    """Trigger every required business error and capture its attribution."""

    from .generator import generate_job_arrays
    from .pipeline import run_job

    if not isinstance(base_spec, JobSpec):
        base_spec = JobSpec.from_mapping(base_spec)
    active_config = config or NumericsConfig()
    arrays = generate_job_arrays(base_spec)
    records: list[dict[str, Any]] = []
    unattributed: list[dict[str, Any]] = []

    def run_case(
        injection: dict[str, Any],
        spec: Any,
        **overrides: Any,
    ) -> None:
        attributed, unexpected = _capture(
            injection=injection,
            action=lambda: run_job(spec, active_config, **overrides),
        )
        if attributed is not None:
            records.append(attributed)
        if unexpected is not None:
            unattributed.append(unexpected)

    if base_spec.vocab_size > 1:
        bad_table = arrays.truth_table[:-1, :]
    else:
        bad_table = arrays.truth_table[:, :-1]
    shape_spec = replace(base_spec, job_id="inj-shape")
    run_case(
        {
            "type": "shape_mismatch",
            "job_id": shape_spec.job_id,
            "expected_vocab_size": base_spec.vocab_size,
            "table_rows": int(bad_table.shape[0]),
        },
        shape_spec,
        truth_table_override=bad_table,
    )

    empty_spec = replace(base_spec, job_id="inj-empty")
    run_case(
        {"type": "empty_index", "job_id": empty_spec.job_id},
        empty_spec,
        index_override=np.empty((0, 1), dtype=np.int64),
    )

    negative_spec = replace(base_spec, job_id="inj-negscale", scale=-2.0)
    run_case(
        {
            "type": "negative_scale",
            "job_id": negative_spec.job_id,
            "scale": negative_spec.scale,
        },
        negative_spec,
    )

    dtype_spec = replace(base_spec, job_id="inj-dtype", table_dtype="qint4")
    run_case(
        {
            "type": "unknown_dtype",
            "job_id": dtype_spec.job_id,
            "table_dtype": dtype_spec.table_dtype,
        },
        dtype_spec,
    )

    oob_spec = replace(base_spec, job_id="inj-oob")
    oob_indices = np.array(
        [[base_spec.vocab_size], [0]],
        dtype=np.int64,
    )
    run_case(
        {
            "type": "index_out_of_range",
            "job_id": oob_spec.job_id,
            "position": [0, 0],
            "value": base_spec.vocab_size,
        },
        oob_spec,
        index_override=oob_indices,
    )

    quant_spec = replace(
        base_spec,
        job_id="inj-quant",
        table_dtype="qint8",
        quant_scale=0.0,
    )
    run_case(
        {
            "type": "quant_invalid",
            "job_id": quant_spec.job_id,
            "quant_scale": quant_spec.quant_scale,
        },
        quant_spec,
    )

    pad_spec = replace(
        base_spec,
        job_id="inj-pad",
        pad_index=base_spec.vocab_size,
    )
    run_case(
        {
            "type": "pad_index_invalid",
            "job_id": pad_spec.job_id,
            "pad_index": pad_spec.pad_index,
        },
        pad_spec,
    )

    duplicate_spec = replace(base_spec, job_id="inj-dup")
    attributed, unexpected = _capture(
        injection={
            "type": "duplicate_job_id",
            "job_id": duplicate_spec.job_id,
        },
        action=_duplicate_job_action(duplicate_spec),
    )
    if attributed is not None:
        records.append(attributed)
    if unexpected is not None:
        unattributed.append(unexpected)

    return {
        "records": records,
        "unattributed_exceptions": unattributed,
    }
