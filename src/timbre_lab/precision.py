"""Shared operator semantics for float64, float32 and fixed-point execution."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import PrecisionConfig
from .deterministic import sha256_json
from .embedding import (
    AlignmentResult,
    CandidateUnit,
    align_units,
    assignment_identity,
)


STAGE_NAMES = (
    "feature_scale",
    "embedding_norm",
    "log_transform",
    "similarity_accum",
)


@dataclass(frozen=True)
class StageTrace:
    name: str
    raw_frame_values: np.ndarray
    raw_candidate_values: np.ndarray | None
    frame_values: np.ndarray
    candidate_values: np.ndarray | None
    quantization_residual_max: float

    def metadata(self) -> dict[str, Any]:
        return {
            "stage": self.name,
            "frame_shape": [int(value) for value in self.frame_values.shape],
            "candidate_shape": (
                [int(value) for value in self.candidate_values.shape]
                if self.candidate_values is not None
                else None
            ),
            "quantization_residual_max": self.quantization_residual_max,
        }


@dataclass(frozen=True)
class PrecisionExecution:
    tier: str
    frame_indices: tuple[int, ...]
    assignment_digest: str
    frame_set_digest: str
    assignment_labels: tuple[tuple[int, int, int], ...]
    assignment_scores: tuple[float, ...]
    alignment: AlignmentResult
    stages: dict[str, StageTrace]
    per_frame_similarity: np.ndarray
    cumulative_similarity: np.ndarray
    config: PrecisionConfig
    canonical_assignment_source: str = "float64"

    @property
    def final_similarities(self) -> np.ndarray:
        return self.cumulative_similarity

    def metadata(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "precision_config": self.config.to_dict(),
            "frame_count": len(self.frame_indices),
            "frame_set_digest": self.frame_set_digest,
            "assignment_digest": self.assignment_digest,
            "assignment_count": len(self.assignment_labels),
            "assignment_source": self.canonical_assignment_source,
            "frame_set_preserved": True,
            "stages": {name: self.stages[name].metadata() for name in STAGE_NAMES},
        }


class PrecisionContext:
    """Representation boundary plus shared arithmetic helpers."""

    def __init__(self, config: PrecisionConfig) -> None:
        config.validate()
        self.config = config
        if config.tier == "float64":
            self.dtype = np.dtype(np.float64)
        elif config.tier == "float32":
            self.dtype = np.dtype(np.float32)
        else:
            self.dtype = np.dtype(np.float64)

    def input_array(self, values: np.ndarray) -> np.ndarray:
        if self.config.tier == "fixed":
            return np.asarray(values, dtype=np.float64)
        return np.asarray(values, dtype=self.dtype)

    def scalar(self, value: float) -> Any:
        if self.config.tier == "float32":
            return np.float32(value)
        return float(value)

    def finish(self, values: np.ndarray) -> tuple[np.ndarray, float]:
        array = np.asarray(values)
        if self.config.tier == "float64":
            output = np.asarray(array, dtype=np.float64)
            return output, 0.0
        if self.config.tier == "float32":
            output = np.asarray(array, dtype=np.float32).astype(np.float64)
            float32_raw = np.asarray(array, dtype=np.float32)
            residual = float(np.max(np.abs(float32_raw.astype(np.float64) - output))) if output.size else 0.0
            return output, residual
        quantized, integers = quantize_to_grid(
            np.asarray(array, dtype=np.float64),
            self.config.fixed_scale,
            self.config.rounding_mode,
        )
        residual = float(np.max(np.abs(quantized - np.asarray(array, dtype=np.float64)))) if output_size(quantized) else 0.0
        del integers
        return quantized, residual


def output_size(values: np.ndarray) -> int:
    return int(np.asarray(values).size)


def quantize_to_grid(
    values: np.ndarray,
    scale: int,
    rounding_mode: str,
) -> tuple[np.ndarray, np.ndarray]:
    """Quantize real values to an integer grid and return dequantized values."""

    if isinstance(scale, bool) or not isinstance(scale, int) or scale <= 0:
        raise ValueError("scale must be a positive integer")
    array = np.asarray(values, dtype=np.float64)
    if not np.all(np.isfinite(array)):
        raise ValueError("cannot quantize non-finite values")
    scaled = array * float(scale)
    if rounding_mode == "half_even":
        rounded = np.rint(scaled)
    elif rounding_mode == "half_away":
        rounded = np.sign(scaled) * np.floor(np.abs(scaled) + 0.5)
    elif rounding_mode == "toward_zero":
        rounded = np.trunc(scaled)
    elif rounding_mode == "floor":
        rounded = np.floor(scaled)
    elif rounding_mode == "ceil":
        rounded = np.ceil(scaled)
    else:
        raise ValueError(f"unknown rounding mode: {rounding_mode}")
    integer_limit = float(np.iinfo(np.int64).max)
    if np.max(np.abs(rounded)) > integer_limit:
        raise OverflowError("fixed-point value exceeds int64 range")
    integers = rounded.astype(np.int64)
    return integers.astype(np.float64) / float(scale), integers


def format_axis(values: np.ndarray, dtype: np.dtype) -> np.ndarray:
    return np.asarray(values, dtype=dtype)


def normalize_rows(values: np.ndarray, epsilon: float, dtype: np.dtype) -> np.ndarray:
    array = np.asarray(values, dtype=dtype)
    epsilon_value = np.asarray(epsilon, dtype=dtype)
    squared = np.sum(array * array, axis=1)
    norms = np.sqrt(np.maximum(squared, np.asarray(0.0, dtype=dtype)))
    reciprocal = np.where(norms > epsilon_value, 1.0 / (norms + epsilon_value), 0.0)
    return np.asarray(array * reciprocal[:, None], dtype=dtype)


def signed_log1p(values: np.ndarray, dtype: np.dtype) -> np.ndarray:
    array = np.asarray(values, dtype=dtype)
    magnitude = np.log1p(np.abs(array))
    return np.asarray(np.sign(array) * magnitude, dtype=dtype)


def cosine_selected(
    frame_vectors: np.ndarray,
    candidate_vectors: np.ndarray,
    selected_indices: np.ndarray,
    epsilon: float,
    dtype: np.dtype,
) -> np.ndarray:
    frames = np.asarray(frame_vectors, dtype=dtype)
    selected = np.asarray(candidate_vectors, dtype=dtype)[selected_indices]
    numerator = np.sum(frames * selected, axis=1)
    frame_norm = np.sqrt(np.sum(frames * frames, axis=1))
    selected_norm = np.sqrt(np.sum(selected * selected, axis=1))
    denominator = frame_norm * selected_norm
    epsilon_value = np.asarray(epsilon, dtype=dtype)
    return np.asarray(
        np.where(denominator > epsilon_value, numerator / (denominator + epsilon_value), 0.0),
        dtype=dtype,
    )


def _run_stages_one_to_three(
    frame_base: np.ndarray,
    candidate_base: np.ndarray,
    context: PrecisionContext,
) -> tuple[dict[str, StageTrace], np.ndarray, np.ndarray]:
    dtype = context.dtype
    input_frames = context.input_array(frame_base)
    input_candidates = context.input_array(candidate_base)
    scale = context.scalar(context.config.feature_scale)

    raw_scale_frames = input_frames * scale
    raw_scale_candidates = input_candidates * scale
    scale_frames, residual_scale_frame = context.finish(raw_scale_frames)
    scale_candidates, residual_scale_candidate = context.finish(raw_scale_candidates)
    scale_trace = StageTrace(
        name="feature_scale",
        raw_frame_values=np.asarray(raw_scale_frames),
        raw_candidate_values=np.asarray(raw_scale_candidates),
        frame_values=scale_frames,
        candidate_values=scale_candidates,
        quantization_residual_max=max(residual_scale_frame, residual_scale_candidate),
    )

    raw_norm_frames = normalize_rows(scale_frames, context.config.epsilon, dtype)
    raw_norm_candidates = normalize_rows(scale_candidates, context.config.epsilon, dtype)
    norm_frames, residual_norm_frame = context.finish(raw_norm_frames)
    norm_candidates, residual_norm_candidate = context.finish(raw_norm_candidates)
    norm_trace = StageTrace(
        name="embedding_norm",
        raw_frame_values=np.asarray(raw_norm_frames),
        raw_candidate_values=np.asarray(raw_norm_candidates),
        frame_values=norm_frames,
        candidate_values=norm_candidates,
        quantization_residual_max=max(residual_norm_frame, residual_norm_candidate),
    )

    raw_log_frames = signed_log1p(norm_frames, dtype)
    raw_log_candidates = signed_log1p(norm_candidates, dtype)
    log_frames, residual_log_frame = context.finish(raw_log_frames)
    log_candidates, residual_log_candidate = context.finish(raw_log_candidates)
    log_trace = StageTrace(
        name="log_transform",
        raw_frame_values=np.asarray(raw_log_frames),
        raw_candidate_values=np.asarray(raw_log_candidates),
        frame_values=log_frames,
        candidate_values=log_candidates,
        quantization_residual_max=max(residual_log_frame, residual_log_candidate),
    )
    return {
        "feature_scale": scale_trace,
        "embedding_norm": norm_trace,
        "log_transform": log_trace,
    }, log_frames, log_candidates


def _complete_similarity_stage(
    stages: dict[str, StageTrace],
    alignment: AlignmentResult,
    context: PrecisionContext,
) -> tuple[dict[str, StageTrace], np.ndarray, np.ndarray]:
    log_trace = stages["log_transform"]
    candidate_lookup = {
        (candidate.speaker_id, candidate.phoneme_id): index
        for index, candidate in enumerate(alignment.candidates)
    }
    selected_indices = np.asarray(
        [
            candidate_lookup[(assignment.speaker_id, assignment.phoneme_id)]
            for assignment in alignment.assignments
        ],
        dtype=np.int64,
    )
    scores = cosine_selected(
        log_trace.frame_values,
        log_trace.candidate_values,
        selected_indices,
        context.config.epsilon,
        context.dtype,
    )
    cumulative = np.cumsum(scores)
    raw_similarity = np.asarray(cumulative[:, None])
    similarity_values, residual = context.finish(raw_similarity)
    similarity_trace = StageTrace(
        name="similarity_accum",
        raw_frame_values=np.asarray(raw_similarity),
        raw_candidate_values=None,
        frame_values=similarity_values,
        candidate_values=None,
        quantization_residual_max=residual,
    )
    stages = dict(stages)
    stages["similarity_accum"] = similarity_trace
    return stages, scores.astype(np.float64), cumulative.astype(np.float64)


def _execute_with_alignment(
    tier: str,
    frame_base: np.ndarray,
    candidate_base: np.ndarray,
    alignment: AlignmentResult,
    config: PrecisionConfig,
) -> PrecisionExecution:
    effective = PrecisionConfig(
        tier=tier,
        fixed_scale=config.fixed_scale,
        rounding_mode=config.rounding_mode,
        feature_scale=config.feature_scale,
        epsilon=config.epsilon,
    )
    context = PrecisionContext(effective)
    stages, log_frames, log_candidates = _run_stages_one_to_three(
        frame_base,
        candidate_base,
        context,
    )
    stages, scores, cumulative = _complete_similarity_stage(stages, alignment, context)
    frame_indices = tuple(assignment.frame_index for assignment in alignment.assignments)
    labels = tuple(assignment_identity(alignment))
    return PrecisionExecution(
        tier=tier,
        frame_indices=frame_indices,
        assignment_digest=alignment.assignment_digest,
        frame_set_digest=sha256_json(list(frame_indices)),
        assignment_labels=labels,
        assignment_scores=tuple(float(assignment.score) for assignment in alignment.assignments),
        alignment=alignment,
        stages=stages,
        per_frame_similarity=np.asarray(scores, dtype=np.float64),
        cumulative_similarity=np.asarray(cumulative, dtype=np.float64),
        config=effective,
    )


def execute_precision(
    frame_base: np.ndarray,
    candidate_base: np.ndarray,
    candidates: tuple[CandidateUnit, ...],
    config: PrecisionConfig,
) -> PrecisionExecution:
    """Execute one tier while preserving a canonical float64 assignment set."""

    config.validate()
    frames = np.asarray(frame_base, dtype=np.float64)
    candidate_vectors = np.asarray(candidate_base, dtype=np.float64)
    if frames.ndim != 2 or candidate_vectors.ndim != 2:
        raise ValueError("base vectors must be two-dimensional")
    canonical_config = PrecisionConfig(
        tier="float64",
        fixed_scale=config.fixed_scale,
        rounding_mode=config.rounding_mode,
        feature_scale=config.feature_scale,
        epsilon=config.epsilon,
    )
    canonical_context = PrecisionContext(canonical_config)
    canonical_stages, canonical_log_frames, canonical_log_candidates = _run_stages_one_to_three(
        frames,
        candidate_vectors,
        canonical_context,
    )
    alignment = align_units(
        canonical_log_frames,
        canonical_log_candidates,
        candidates,
        stage_name="log_transform",
    )
    if config.tier == "float64":
        stages, scores, cumulative = _complete_similarity_stage(
            canonical_stages,
            alignment,
            canonical_context,
        )
        frame_indices = tuple(assignment.frame_index for assignment in alignment.assignments)
        labels = tuple(assignment_identity(alignment))
        return PrecisionExecution(
            tier="float64",
            frame_indices=frame_indices,
            assignment_digest=alignment.assignment_digest,
            frame_set_digest=sha256_json(list(frame_indices)),
            assignment_labels=labels,
            assignment_scores=tuple(
                float(value.score) for value in alignment.assignments
            ),
            alignment=alignment,
            stages=stages,
            per_frame_similarity=np.asarray(scores, dtype=np.float64),
            cumulative_similarity=np.asarray(cumulative, dtype=np.float64),
            config=PrecisionConfig(
                tier="float64",
                fixed_scale=config.fixed_scale,
                rounding_mode=config.rounding_mode,
                feature_scale=config.feature_scale,
                epsilon=config.epsilon,
            ),
        )
    return _execute_with_alignment(
        config.tier,
        frames,
        candidate_vectors,
        alignment,
        config,
    )


def verify_fixed_recomputation(execution: PrecisionExecution) -> dict[str, Any]:
    """Verify each fixed stage by reapplying the declared quantization rule."""

    if execution.tier != "fixed":
        return {"ok": True, "applicable": False, "stages": {}}
    scale = execution.config.fixed_scale
    mode = execution.config.rounding_mode
    checks: dict[str, Any] = {}
    ok = True
    for name in STAGE_NAMES:
        trace = execution.stages[name]
        expected_frame, _ = quantize_to_grid(trace.raw_frame_values, scale, mode)
        frame_equal = bool(np.array_equal(expected_frame, trace.frame_values))
        candidate_equal = True
        if trace.raw_candidate_values is not None and trace.candidate_values is not None:
            expected_candidate, _ = quantize_to_grid(trace.raw_candidate_values, scale, mode)
            candidate_equal = bool(
                np.array_equal(expected_candidate, trace.candidate_values)
            )
        stage_ok = frame_equal and candidate_equal
        ok = ok and stage_ok
        checks[name] = {
            "ok": stage_ok,
            "frame_equal": frame_equal,
            "candidate_equal": candidate_equal,
            "fixed_scale": scale,
            "rounding_mode": mode,
        }
    return {
        "ok": ok,
        "applicable": True,
        "fixed_scale": scale,
        "rounding_mode": mode,
        "stages": checks,
    }


def precision_summary(execution: PrecisionExecution) -> dict[str, Any]:
    return {
        **execution.metadata(),
        "mean_similarity": float(np.mean(execution.per_frame_similarity))
        if execution.per_frame_similarity.size
        else 0.0,
        "final_cumulative_similarity": float(execution.cumulative_similarity[-1])
        if execution.cumulative_similarity.size
        else 0.0,
        "fixed_recompute": verify_fixed_recomputation(execution),
    }
