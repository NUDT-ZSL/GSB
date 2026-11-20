"""Public embedding pipeline and end-to-end job execution."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import numpy as np

from .config import NumericsConfig
from .errors import (
    E_CONFIG_INVALID,
    E_INDEX_INVALID,
    E_INDEX_OUT_OF_RANGE,
    E_REDUCTION_MODE_UNKNOWN,
    E_SHAPE_MISMATCH,
    business_error,
)
from .generator import generate_job_arrays
from .indexing import IndexValidation, validate_indices
from .models import JobSpec
from .numeric import (
    AccumulationResult,
    accumulate_weighted_terms,
    apply_numeric_policy,
    protect_subnormal,
    round_to_step,
)
from .quantize import build_quantized_table
from .util import array_digest, sha256_json


REDUCTION_MODES = ("sum", "mean", "first", "concat")


@dataclass(frozen=True)
class ReductionResult:
    gathered: np.ndarray
    indices: np.ndarray
    pad_mask: np.ndarray
    reduction_weights: np.ndarray
    position_weights: np.ndarray
    effective_weights: np.ndarray
    reduced: np.ndarray
    output: np.ndarray
    accumulation: AccumulationResult
    reduction_mode: str
    pad_policy: str
    token_positions: list[int]
    weight_details: list[dict[str, Any]]
    token_weight_details: list[dict[str, Any]]
    reduction_digest: str
    output_digest: str

    @property
    def output_dim(self) -> int:
        return int(self.output.shape[1])


def _normalize_index_array(
    indices: np.ndarray,
    *,
    job_id: str,
) -> np.ndarray:
    array = np.asarray(indices)
    if array.size == 0:
        raise business_error(
            E_INDEX_INVALID,
            "gather",
            job_id,
            "gather index array is empty",
        )
    if array.dtype.kind not in ("i", "u"):
        raise business_error(
            E_INDEX_INVALID,
            "gather",
            job_id,
            "gather index array must contain integers",
            dtype=str(array.dtype),
        )
    if array.ndim == 1:
        array = array.reshape(array.shape[0], 1)
    if array.ndim != 2:
        raise business_error(
            E_INDEX_INVALID,
            "gather",
            job_id,
            "gather indices must be 1-D or 2-D",
            shape=list(array.shape),
        )
    return np.ascontiguousarray(array, dtype=np.int64)


def _check_gather_bounds(
    indices: np.ndarray,
    bound: int,
    *,
    gather_axis: int,
    job_id: str,
) -> None:
    bad = (indices < 0) | (indices >= bound)
    flat = np.flatnonzero(bad.reshape(-1))
    if flat.size == 0:
        return
    first = int(flat[0])
    multi = np.unravel_index(first, indices.shape)
    position = [int(multi[0]), int(multi[1])]
    raise business_error(
        E_INDEX_OUT_OF_RANGE,
        "gather",
        job_id,
        (
            f"index {int(indices[multi])} at position {position} out of "
            f"gather-axis bound [0, {bound})"
        ),
        first_position=first,
        position=position,
        value=int(indices[multi]),
        gather_axis=gather_axis,
        lower=0,
        upper=bound,
    )


def gather_embedding(
    table: np.ndarray,
    indices: np.ndarray,
    gather_axis: int = 0,
    *,
    job_id: str = "gather",
) -> np.ndarray:
    """Gather token vectors with an explicit physical table axis.

    ``gather_axis=0`` expects a logical ``(vocab_size, embed_dim)`` table.
    ``gather_axis=1`` expects a transposed physical table
    ``(embed_dim, vocab_size)``. Both return ``(tokens, hits, embed_dim)``.
    """

    array = np.asarray(table)
    if array.ndim != 2:
        raise business_error(
            E_SHAPE_MISMATCH,
            "gather",
            job_id,
            f"gather table must be 2-D, got shape {array.shape}",
            actual_shape=list(array.shape),
        )
    if gather_axis not in (0, 1):
        raise business_error(
            E_CONFIG_INVALID,
            "gather",
            job_id,
            f"gather_axis must be 0 or 1, got {gather_axis}",
            gather_axis=gather_axis,
        )
    index_array = _normalize_index_array(indices, job_id=job_id)
    bound = int(array.shape[gather_axis])
    _check_gather_bounds(
        index_array,
        bound,
        gather_axis=gather_axis,
        job_id=job_id,
    )
    if gather_axis == 0:
        return np.ascontiguousarray(np.take(array, index_array, axis=0))
    gathered = np.take(array, index_array, axis=1)
    gathered = np.moveaxis(gathered, 0, -1)
    return np.ascontiguousarray(gathered)


def _validate_weight_shape(
    position_weights: np.ndarray,
    shape: tuple[int, int],
    *,
    job_id: str,
) -> np.ndarray:
    weights = np.asarray(position_weights, dtype=np.float64)
    if weights.shape != shape:
        raise business_error(
            E_SHAPE_MISMATCH,
            "weighted_accumulation",
            job_id,
            (
                f"position weights must have shape {shape}, "
                f"got {weights.shape}"
            ),
            expected_shape=list(shape),
            actual_shape=list(weights.shape),
        )
    return np.ascontiguousarray(weights)


def _coefficients(
    indices: np.ndarray,
    pad_mask: np.ndarray,
    reduction_mode: str,
    pad_policy: str,
    *,
    job_id: str,
) -> tuple[np.ndarray, np.ndarray]:
    if reduction_mode not in REDUCTION_MODES:
        raise business_error(
            E_REDUCTION_MODE_UNKNOWN,
            "reduce_and_weight",
            job_id,
            f"unknown reduction_mode: {reduction_mode!r}",
            known_modes=list(REDUCTION_MODES),
        )
    tokens, hits = indices.shape
    if pad_policy == "exclude":
        nonpad = ~pad_mask
        denominator = np.maximum(nonpad.sum(axis=1), 1).astype(np.float64)
    elif pad_policy == "zero_weight":
        nonpad = np.ones((tokens, hits), dtype=bool)
        denominator = np.full((tokens,), float(hits), dtype=np.float64)
    else:
        raise business_error(
            E_CONFIG_INVALID,
            "reduce_and_weight",
            job_id,
            f"unknown pad_policy: {pad_policy!r}",
            pad_policy=pad_policy,
        )
    alpha = np.zeros((tokens, hits), dtype=np.float64)
    if reduction_mode == "sum":
        alpha[nonpad] = 1.0
    elif reduction_mode == "mean":
        alpha[nonpad] = 1.0 / denominator[:, None].repeat(hits, axis=1)[nonpad]
    elif reduction_mode == "first":
        for token in range(tokens):
            active = np.flatnonzero(~pad_mask[token])
            if active.size:
                alpha[token, int(active[0])] = 1.0
    else:
        alpha[nonpad] = 1.0
    alpha[pad_mask] = 0.0
    return np.ascontiguousarray(alpha), nonpad


def _flatten_concat(blocks: np.ndarray) -> np.ndarray:
    tokens, hits, embed_dim = blocks.shape
    return np.ascontiguousarray(blocks.reshape(tokens, hits * embed_dim))


def reduce_and_weight(
    gathered: np.ndarray,
    indices: np.ndarray,
    pad_mask: np.ndarray,
    position_weights: np.ndarray,
    *,
    reduction_mode: str,
    pad_policy: str,
    config: NumericsConfig,
    job_id: str = "reduce",
) -> ReductionResult:
    """Reduce repeated hits and apply token-position weights.

    The exported token details satisfy, for every output element::

        output[token, slice] = sum(
            combined_weight * embedding[index, :]
        )

    Accumulation ordering is token-major and hit-major.
    """

    values = np.asarray(gathered, dtype=np.float64)
    index_array = _normalize_index_array(indices, job_id=job_id)
    if values.ndim != 3:
        raise business_error(
            E_SHAPE_MISMATCH,
            "reduce_and_weight",
            job_id,
            f"gathered values must be 3-D, got {values.shape}",
            actual_shape=list(values.shape),
        )
    if values.shape[:2] != index_array.shape:
        raise business_error(
            E_SHAPE_MISMATCH,
            "reduce_and_weight",
            job_id,
            "gathered token/hit shape does not match indices",
            gathered_shape=list(values.shape),
            indices_shape=list(index_array.shape),
        )
    mask = np.asarray(pad_mask, dtype=bool)
    if mask.shape != index_array.shape:
        raise business_error(
            E_SHAPE_MISMATCH,
            "reduce_and_weight",
            job_id,
            "padding mask shape does not match indices",
            mask_shape=list(mask.shape),
            indices_shape=list(index_array.shape),
        )
    tokens, hits, embed_dim = values.shape
    weights = _validate_weight_shape(
        position_weights,
        (tokens, embed_dim),
        job_id=job_id,
    )
    alpha, nonpad = _coefficients(
        index_array,
        mask,
        reduction_mode,
        pad_policy,
        job_id=job_id,
    )
    effective_weights = alpha[:, :, None] * weights[:, None, :]
    effective_weights = np.ascontiguousarray(effective_weights)

    if reduction_mode == "concat":
        reduced_blocks = values * alpha[:, :, None]
        reduced = _flatten_concat(reduced_blocks)
        weighted_blocks = values * effective_weights
        output_blocks = np.asarray(weighted_blocks, dtype=np.float64)
        if config.accum_rounding_enabled and config.accum_rounding_step > 0.0:
            output_blocks = round_to_step(
                output_blocks,
                config.accum_rounding_step,
                config.rounding_mode,
            )
        output_blocks = protect_subnormal(
            output_blocks,
            config.subnormal_policy,
            config.subnormal_threshold,
        )
        output = _flatten_concat(output_blocks)
        accumulation = AccumulationResult(
            values=output,
            additions=1,
            rounding_applied=bool(
                config.accum_rounding_enabled
                and config.accum_rounding_step > 0.0
            ),
        )
    else:
        reduced = np.sum(values * alpha[:, :, None], axis=1, dtype=np.float64)
        reduced = protect_subnormal(
            reduced,
            config.subnormal_policy,
            config.subnormal_threshold,
        )
        accumulation = accumulate_weighted_terms(
            values,
            effective_weights,
            config,
        )
        output = accumulation.values
    output_dim = output.shape[1]

    aggregate: dict[int, dict[str, Any]] = {}
    token_details: list[dict[str, Any]] = []
    for token in range(tokens):
        candidates: list[dict[str, Any]] = []
        active_hits = 0
        for hit in range(hits):
            index = int(index_array[token, hit])
            is_padding = bool(mask[token, hit])
            reduction_weight = float(alpha[token, hit])
            if reduction_weight != 0.0:
                active_hits += 1
            if reduction_mode == "concat":
                output_slice = [hit * embed_dim, (hit + 1) * embed_dim]
            else:
                output_slice = [0, output_dim]
            combined = np.asarray(
                effective_weights[token, hit, :], dtype=np.float64
            )
            position_vector = np.asarray(weights[token, :], dtype=np.float64)
            candidate = {
                "hit": hit,
                "index": index,
                "is_padding": is_padding,
                "reduction_weight": reduction_weight,
                "position_weight": position_vector.tolist(),
                "combined_weight": combined.tolist(),
                "output_slice": output_slice,
            }
            candidates.append(candidate)
            group = aggregate.setdefault(
                index,
                {
                    "index": index,
                    "hits": 0,
                    "positions": [],
                    "reduction_weights": [],
                    "position_weight_means": [],
                    "combined_weight_means": [],
                    "combined_weight_vectors": [],
                    "output_slices": [],
                },
            )
            group["hits"] += 1
            group["positions"].append([token, hit])
            group["reduction_weights"].append(reduction_weight)
            group["position_weight_means"].append(float(position_vector.mean()))
            group["combined_weight_means"].append(float(combined.mean()))
            group["combined_weight_vectors"].append(combined.tolist())
            group["output_slices"].append(output_slice)
        token_details.append(
            {
                "token": token,
                "active_hits": active_hits,
                "candidates": candidates,
            }
        )
    weight_details = [aggregate[index] for index in sorted(aggregate)]
    reduction_digest = sha256_json(
        {
            "job_id": job_id,
            "mode": reduction_mode,
            "pad_policy": pad_policy,
            "indices": index_array.tolist(),
            "pad_mask": mask.tolist(),
            "alpha": alpha.tolist(),
            "position_weights": weights.tolist(),
        }
    )
    return ReductionResult(
        gathered=np.ascontiguousarray(values),
        indices=index_array,
        pad_mask=np.ascontiguousarray(mask),
        reduction_weights=alpha,
        position_weights=weights,
        effective_weights=effective_weights,
        reduced=np.ascontiguousarray(reduced),
        output=np.ascontiguousarray(output),
        accumulation=accumulation,
        reduction_mode=reduction_mode,
        pad_policy=pad_policy,
        token_positions=list(range(tokens)),
        weight_details=weight_details,
        token_weight_details=token_details,
        reduction_digest=reduction_digest,
        output_digest=array_digest(output),
    )


def _reference_config(config: NumericsConfig) -> NumericsConfig:
    return replace(
        config,
        dequant_rounding_step=0.0,
        accum_rounding_enabled=False,
        accum_rounding_step=0.0,
        subnormal_policy="preserve",
    )


def _output_interval(
    actual: np.ndarray,
    reference: np.ndarray,
    config: NumericsConfig,
) -> dict[str, Any]:
    tolerance = (
        config.tolerance_abs
        + config.tolerance_rel * np.maximum(1.0, np.abs(reference))
    )
    lower = np.minimum(actual, reference) - tolerance
    upper = np.maximum(actual, reference) + tolerance
    return {
        "lower": lower.tolist(),
        "upper": upper.tolist(),
        "center": ((lower + upper) / 2.0).tolist(),
        "half_width": ((upper - lower) / 2.0).tolist(),
        "precision": "float64",
        "interval_rule": "snapshot_reference_envelope_plus_explicit_tolerance",
        "contains_actual": bool(np.all((actual >= lower) & (actual <= upper))),
        "contains_reference": bool(
            np.all((reference >= lower) & (reference <= upper))
        ),
    }


def _degradation_reasons(
    spec: JobSpec,
    config: NumericsConfig,
    within_tolerance: bool,
) -> list[str]:
    reasons: list[str] = []
    if spec.table_dtype != "float64":
        reasons.append(f"precision_tier:{spec.table_dtype}")
    if config.dequant_rounding_step > 0.0:
        reasons.append("dequant_rounding_grid")
    if config.accum_rounding_enabled and config.accum_rounding_step > 0.0:
        reasons.append("accumulation_rounding_grid")
    if config.subnormal_policy != "preserve":
        reasons.append(f"subnormal_policy:{config.subnormal_policy}")
    if not within_tolerance:
        reasons.append("fixed_tolerance_exceeded")
    return reasons


def run_job(
    spec: JobSpec | dict[str, Any],
    config: NumericsConfig | None = None,
    *,
    truth_table_override: np.ndarray | None = None,
    index_override: np.ndarray | None = None,
    position_weights_override: np.ndarray | None = None,
) -> dict[str, Any]:
    """Execute one manifest job and return a deterministic report record."""

    from .diagnostics import build_error_spectrum, self_check_job

    if not isinstance(spec, JobSpec):
        spec = JobSpec.from_mapping(spec)
    active_config = config or NumericsConfig()
    if spec.pad_policy is not None:
        active_config = replace(active_config, pad_policy=spec.pad_policy)
    arrays = generate_job_arrays(spec)
    truth_table = (
        np.asarray(truth_table_override)
        if truth_table_override is not None
        else arrays.truth_table
    )
    indices = (
        np.asarray(index_override)
        if index_override is not None
        else arrays.indices
    )
    validation = validate_indices(
        indices,
        vocab_size=spec.vocab_size,
        pad_index=spec.effective_pad_index,
        job_id=spec.job_id,
    )
    position_weights = (
        np.asarray(position_weights_override)
        if position_weights_override is not None
        else arrays.position_weights
    )
    position_weights = _validate_weight_shape(
        position_weights,
        (validation.indices.shape[0], spec.embed_dim),
        job_id=spec.job_id,
    )
    quantized = build_quantized_table(
        truth_table,
        spec=spec,
        config=active_config,
    )
    physical_actual = (
        quantized.dequantized
        if spec.gather_axis == 0
        else np.ascontiguousarray(quantized.dequantized.T)
    )
    physical_reference = (
        validate_table(
            truth_table,
            spec,
        )
        if spec.gather_axis == 0
        else np.ascontiguousarray(validate_table(truth_table, spec).T)
    )
    gathered_actual = gather_embedding(
        physical_actual,
        validation.indices,
        spec.gather_axis,
        job_id=spec.job_id,
    )
    gathered_reference = gather_embedding(
        physical_reference,
        validation.indices,
        spec.gather_axis,
        job_id=spec.job_id,
    )
    reduction_actual = reduce_and_weight(
        gathered_actual,
        validation.indices,
        validation.pad_mask,
        position_weights,
        reduction_mode=spec.reduction_mode,
        pad_policy=active_config.pad_policy,
        config=active_config,
        job_id=spec.job_id,
    )
    reduction_reference = reduce_and_weight(
        gathered_reference,
        validation.indices,
        validation.pad_mask,
        position_weights,
        reduction_mode=spec.reduction_mode,
        pad_policy=active_config.pad_policy,
        config=_reference_config(active_config),
        job_id=spec.job_id,
    )
    output_without_accum_rounding = reduce_and_weight(
        gathered_actual,
        validation.indices,
        validation.pad_mask,
        position_weights,
        reduction_mode=spec.reduction_mode,
        pad_policy=active_config.pad_policy,
        config=replace(
            active_config,
            accum_rounding_enabled=False,
            accum_rounding_step=0.0,
        ),
        job_id=spec.job_id,
    )
    spectrum = build_error_spectrum(
        job_id=spec.job_id,
        table_dtype=spec.table_dtype,
        dequantized_table=quantized.dequantized,
        reference_table=validate_table(truth_table, spec),
        gathered_actual=gathered_actual,
        gathered_reference=gathered_reference,
        reduced_actual=reduction_actual.reduced,
        reduced_reference=reduction_reference.reduced,
        output_actual=reduction_actual.output,
        output_reference=reduction_reference.output,
        output_without_accum_rounding=output_without_accum_rounding.output,
        reduction_weights_actual=reduction_actual.reduction_weights,
        reduction_weights_reference=reduction_reference.reduction_weights,
        config=active_config,
        quant_scale=quantized.quant_scale,
    )
    self_check = self_check_job(
        job_id=spec.job_id,
        validation=validation,
        actual=reduction_actual,
        embedding_table=quantized.dequantized,
        config=active_config,
        spectrum=spectrum,
    )
    reasons = _degradation_reasons(
        spec,
        active_config,
        bool(spectrum["within_tolerance"]),
    )
    return {
        "job_id": spec.job_id,
        "status": "ok",
        "precision_tier": spec.table_dtype,
        "gather_axis": spec.gather_axis,
        "reduction_mode": spec.reduction_mode,
        "pad_policy": active_config.pad_policy,
        "num_tokens": int(validation.indices.shape[0]),
        "hits_per_token": int(validation.indices.shape[1]),
        "token_positions": reduction_actual.token_positions,
        "index_validation": validation.to_dict(),
        "table": quantized.to_dict(),
        "generated_arrays": arrays.to_dict(),
        "position_weights": position_weights.tolist(),
        "weight_details": reduction_actual.weight_details,
        "token_weight_details": reduction_actual.token_weight_details,
        "output_values": reduction_actual.output.tolist(),
        "output_interval": _output_interval(
            reduction_actual.output,
            reduction_reference.output,
            active_config,
        ),
        "accumulation": {
            "additions_per_output": reduction_actual.accumulation.additions,
            "rounding_applied": reduction_actual.accumulation.rounding_applied,
            "cooperative_stage_order": "token_major_then_hit_major",
        },
        "spectrum": spectrum,
        "self_check": self_check,
        "business_codes": [],
        "degraded": bool(reasons),
        "degradation_reasons": reasons,
        "reduction_digest": reduction_actual.reduction_digest,
        "output_digest": reduction_actual.output_digest,
    }


def validate_table(table: np.ndarray, spec: JobSpec) -> np.ndarray:
    """Validate a logical table while preserving float64 reference values."""

    array = np.asarray(table)
    if array.shape != (spec.vocab_size, spec.embed_dim):
        raise business_error(
            E_SHAPE_MISMATCH,
            "table_build",
            spec.job_id,
            (
                f"vocab_size/embed_dim mismatch: expected "
                f"{(spec.vocab_size, spec.embed_dim)}, table has {array.shape}"
            ),
            expected_vocab_size=spec.vocab_size,
            expected_embed_dim=spec.embed_dim,
            actual_shape=list(array.shape),
        )
    return np.ascontiguousarray(array, dtype=np.float64)
