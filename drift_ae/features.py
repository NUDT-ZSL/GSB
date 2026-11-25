"""Reconstruction-error features and mask-aware anomaly scoring."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from .codec import to_native
from .io_utils import array_fingerprint, payload_checksum
from .preprocessing import PreprocessedWindow


FEATURE_NAMES = (
    "mean_residual",
    "max_residual",
    "smooth_residual",
    "boundary_instability",
)


@dataclass(frozen=True)
class ScoringConfig:
    mean_weight: float = 0.35
    max_weight: float = 0.25
    smooth_weight: float = 0.30
    boundary_weight: float = 0.10
    jump_scale: float = 3.0
    epsilon: float = 1e-6


@dataclass
class ErrorFeatures:
    window_id: str
    mean_residual: float
    max_residual: float
    smooth_residual: float
    mask_consistency: float
    boundary_instability: float
    score: float
    residual: np.ndarray
    weighted_residual: np.ndarray
    smoothed_residual: np.ndarray
    feature_vector: np.ndarray
    boundary_traces: list[dict] = field(default_factory=list)
    checksum: str = ""

    def scalar_values(self) -> dict[str, float]:
        return {
            "mean_residual": self.mean_residual,
            "max_residual": self.max_residual,
            "smooth_residual": self.smooth_residual,
            "mask_consistency": self.mask_consistency,
            "boundary_instability": self.boundary_instability,
            "score": self.score,
        }

    def to_record(self) -> dict:
        record = self.scalar_values()
        record["window_id"] = self.window_id
        record["feature_names"] = list(FEATURE_NAMES)
        record["feature_vector"] = self.feature_vector.tolist()
        record["boundary_traces"] = to_native(self.boundary_traces)
        record["checksum"] = self.checksum
        return record


def score_from_values(values: dict[str, float], config: ScoringConfig | None = None) -> float:
    cfg = config or ScoringConfig()
    return float(
        cfg.mean_weight * float(values["mean_residual"])
        + cfg.max_weight * float(values["max_residual"])
        + cfg.smooth_weight * float(values["smooth_residual"])
        + cfg.boundary_weight * float(values["boundary_instability"])
    )


def extract_features(
    preprocessed: PreprocessedWindow,
    reconstruction: np.ndarray | Sequence[float],
    smooth_kernel: int = 5,
    scoring_config: ScoringConfig | None = None,
) -> ErrorFeatures:
    cfg = scoring_config or ScoringConfig()
    recon = np.asarray(reconstruction, dtype=np.float64).reshape(-1)
    if recon.size != preprocessed.dim:
        raise ValueError("reconstruction dimension mismatch")

    residual = np.abs(preprocessed.completed - recon)
    weights = preprocessed.contribution_weights
    weighted_residual = residual * weights

    weight_sum = float(weights.sum())
    mean_residual = float(weighted_residual.sum() / max(weight_sum, cfg.epsilon))
    max_residual = float(weighted_residual.max(initial=0.0))

    kernel = np.ones(max(1, int(smooth_kernel)), dtype=np.float64)
    smooth_numerator = np.convolve(weighted_residual, kernel, mode="same")
    smooth_denominator = np.convolve(weights, kernel, mode="same")
    smoothed_residual = smooth_numerator / np.maximum(smooth_denominator, cfg.epsilon)
    smooth_residual = float(np.max(smoothed_residual))

    boundary_traces = _adjacent_mask_consistency(preprocessed, residual, mean_residual, cfg)
    if boundary_traces:
        boundary_instability = float(np.mean([item["instability"] for item in boundary_traces]))
    else:
        boundary_instability = 0.0
    mask_consistency = float(1.0 - boundary_instability)

    feature_vector = np.asarray(
        [mean_residual, max_residual, smooth_residual, boundary_instability],
        dtype=np.float64,
    )
    score = score_from_values(
        {
            "mean_residual": mean_residual,
            "max_residual": max_residual,
            "smooth_residual": smooth_residual,
            "boundary_instability": boundary_instability,
        },
        cfg,
    )
    checksum = payload_checksum(
        {
            "window_id": preprocessed.window_id,
            "scalars": [
                mean_residual,
                max_residual,
                smooth_residual,
                boundary_instability,
                score,
            ],
            "residual": array_fingerprint(residual),
            "smoothed": array_fingerprint(smoothed_residual),
        }
    )

    return ErrorFeatures(
        window_id=preprocessed.window_id,
        mean_residual=mean_residual,
        max_residual=max_residual,
        smooth_residual=smooth_residual,
        mask_consistency=mask_consistency,
        boundary_instability=boundary_instability,
        score=float(score),
        residual=residual,
        weighted_residual=weighted_residual,
        smoothed_residual=smoothed_residual,
        feature_vector=feature_vector,
        boundary_traces=boundary_traces,
        checksum=checksum,
    )


def _adjacent_mask_consistency(
    preprocessed: PreprocessedWindow,
    residual: np.ndarray,
    mean_residual: float,
    config: ScoringConfig,
) -> list[dict]:
    mask = preprocessed.mask
    boundaries = np.flatnonzero(mask[:-1] != mask[1:])
    traces = []
    scale = max(float(mean_residual), float(residual.mean()), config.epsilon)
    shape = preprocessed.input_shape
    for left_dimension in boundaries:
        left = int(left_dimension)
        right = left + 1
        jump = float(abs(residual[left] - residual[right]))
        instability = min(1.0, jump / max(config.jump_scale * scale, config.epsilon))
        if len(shape) == 2:
            left_time, left_sensor = np.unravel_index(left, shape)
            right_time, right_sensor = np.unravel_index(right, shape)
        else:
            left_time, left_sensor = left, 0
            right_time, right_sensor = right, 0
        traces.append(
            {
                "window_id": preprocessed.window_id,
                "dimension": right,
                "left_dimension": left,
                "right_dimension": right,
                "left_time_index": int(left_time),
                "left_sensor_index": int(left_sensor),
                "right_time_index": int(right_time),
                "right_sensor_index": int(right_sensor),
                "mask_left": int(mask[left]),
                "mask_right": int(mask[right]),
                "residual_left": float(residual[left]),
                "residual_right": float(residual[right]),
                "residual_jump": jump,
                "instability": float(instability),
                "consistency_contribution": float(1.0 - instability),
            }
        )
    return traces
