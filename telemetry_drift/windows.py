"""Residual backtrack windows and self-contained drift evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .alignment import AlignmentResult
from .canonical import sha256_value, stable_float
from .config import PipelineConfig, StatisticsConfig
from .snapshots import PredictorModel
from .statistics import (
    WindowStatistics,
    compute_window_statistics,
    recompute_drift_score,
)
from .thresholds import JointDecision, decide_joint


@dataclass(frozen=True)
class WindowSpec:
    window_id: str
    ordinal: int
    sample_indices: tuple[int, ...]
    start_index: int
    end_index: int
    start_ms: int
    end_ms: int
    backtrack_start_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "ordinal": self.ordinal,
            "sample_indices": list(self.sample_indices),
            "start_index": self.start_index,
            "end_index": self.end_index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "backtrack_start_ms": self.backtrack_start_ms,
        }


@dataclass(frozen=True)
class WindowEvidence:
    window_id: str
    ordinal: int
    start_index: int
    end_index: int
    start_ms: int
    end_ms: int
    backtrack_start_ms: int
    sample_indices: tuple[int, ...]
    raw_residuals: tuple[float, ...]
    feature_deltas: tuple[tuple[float, ...], ...]
    features: tuple[tuple[float, ...], ...]
    targets: tuple[float, ...]
    predictions: tuple[float, ...]
    coefficients: tuple[float, ...]
    statistics: WindowStatistics
    decision: JointDecision
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "ordinal": self.ordinal,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "backtrack_start_ms": self.backtrack_start_ms,
            "sample_indices": list(self.sample_indices),
            "raw_residuals": list(self.raw_residuals),
            "window_params": {
                "feature_deltas": [list(row) for row in self.feature_deltas],
                "coefficients": list(self.coefficients),
            },
            "features": [list(row) for row in self.features],
            "targets": list(self.targets),
            "predictions": list(self.predictions),
            "statistics": self.statistics.to_dict(),
            "decision": self.decision.to_dict(),
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WindowEvidence":
        params = value["window_params"]
        stats_payload = value["statistics"]
        stats = WindowStatistics(
            mean=float(stats_payload["mean"]),
            variance=float(stats_payload["variance"]),
            standard_deviation=float(stats_payload["standard_deviation"]),
            skewness=float(stats_payload["skewness"]),
            trend=float(stats_payload["trend"]),
            upstream_contribution=float(stats_payload["upstream_contribution"]),
            input_amplitude=float(stats_payload["input_amplitude"]),
            drift_score=float(stats_payload["drift_score"]),
            effective_sample_count=float(stats_payload["effective_sample_count"]),
        )
        return cls(
            window_id=str(value["window_id"]),
            ordinal=int(value["ordinal"]),
            start_index=int(value["start_index"]),
            end_index=int(value["end_index"]),
            start_ms=int(value["start_ms"]),
            end_ms=int(value["end_ms"]),
            backtrack_start_ms=int(value["backtrack_start_ms"]),
            sample_indices=tuple(int(item) for item in value["sample_indices"]),
            raw_residuals=tuple(float(item) for item in value["raw_residuals"]),
            feature_deltas=tuple(
                tuple(float(item) for item in row)
                for row in params["feature_deltas"]
            ),
            features=tuple(
                tuple(float(item) for item in row)
                for row in value["features"]
            ),
            targets=tuple(float(item) for item in value["targets"]),
            predictions=tuple(float(item) for item in value["predictions"]),
            coefficients=tuple(float(item) for item in params["coefficients"]),
            statistics=stats,
            decision=JointDecision.from_dict(value["decision"]),
            digest=str(value["digest"]),
        )


def window_evidence_from_cache(
    value: dict[str, Any], config: PipelineConfig
) -> WindowEvidence:
    params = value["window_params"]
    stats_config = config.statistics
    stats = WindowStatistics(
        mean=float(value["statistics"]["mean"]),
        variance=float(value["statistics"]["variance"]),
        standard_deviation=float(value["statistics"]["standard_deviation"]),
        skewness=float(value["statistics"]["skewness"]),
        trend=float(value["statistics"]["trend"]),
        upstream_contribution=float(value["statistics"]["upstream_contribution"]),
        input_amplitude=float(value["statistics"]["input_amplitude"]),
        drift_score=float(value["statistics"]["drift_score"]),
        effective_sample_count=float(
            value["statistics"]["effective_sample_count"]
        ),
    )
    decision = decide_joint(
        stats.input_amplitude,
        stats.drift_score,
        input_threshold=config.predictor.input_amplitude_threshold,
        predictor_threshold=config.predictor.drift_threshold,
        config=config.thresholds,
    )
    return WindowEvidence(
        window_id=str(value["window_id"]),
        ordinal=int(value["ordinal"]),
        start_index=int(value["start_index"]),
        end_index=int(value["end_index"]),
        start_ms=int(value["start_ms"]),
        end_ms=int(value["end_ms"]),
        backtrack_start_ms=int(value["backtrack_start_ms"]),
        sample_indices=tuple(int(item) for item in value["sample_indices"]),
        raw_residuals=tuple(float(item) for item in value["raw_residuals"]),
        feature_deltas=tuple(
            tuple(float(item) for item in row)
            for row in params["feature_deltas"]
        ),
        features=tuple(
            tuple(float(item) for item in row) for row in value["features"]
        ),
        targets=tuple(float(item) for item in value["targets"]),
        predictions=tuple(float(item) for item in value["predictions"]),
        coefficients=tuple(float(item) for item in params["coefficients"]),
        statistics=stats,
        decision=decision,
        digest=str(value["digest"]),
    )


def _feature_deltas(features: np.ndarray) -> np.ndarray:
    deltas = np.zeros_like(features, dtype=np.float64)
    if features.shape[0] > 1:
        deltas[1:] = features[1:] - features[:-1]
    return deltas


def build_window_specs(
    aligned: AlignmentResult,
    config: PipelineConfig,
) -> list[WindowSpec]:
    output_flags = aligned.fill_flags[config.predictor.output_variable]
    output_spec = config.variable_map()[config.predictor.output_variable]
    output_period_ms = max(1, int(round(1000.0 / output_spec.sample_rate_hz)))
    origin_ms = aligned.times_ms[0]
    evaluated_indices = [
        index
        for index, (tick, flag) in enumerate(zip(aligned.times_ms, output_flags))
        if (tick - origin_ms) % output_period_ms == 0 and flag != "sample_hold"
    ]
    size = config.residual_window.size
    step = config.residual_window.step
    if len(evaluated_indices) < size:
        return []
    specs: list[WindowSpec] = []
    for ordinal, start_position in enumerate(
        range(0, len(evaluated_indices) - size + 1, step)
    ):
        positions = evaluated_indices[start_position : start_position + size]
        specs.append(
            WindowSpec(
                window_id=f"w{ordinal:04d}",
                ordinal=ordinal,
                sample_indices=tuple(positions),
                start_index=positions[0],
                end_index=positions[-1],
                start_ms=aligned.times_ms[positions[0]],
                end_ms=aligned.times_ms[positions[-1]],
                backtrack_start_ms=aligned.times_ms[
                    max(
                        0,
                        positions[0] - config.residual_window.max_backtrack,
                    )
                ],
            )
        )
    return specs


def build_window_evidence_for_specs(
    aligned: AlignmentResult,
    predictor: PredictorModel,
    config: PipelineConfig,
    specs: Iterable[WindowSpec],
) -> list[WindowEvidence]:
    input_ids = list(config.predictor.input_variables)
    features = aligned.matrix(input_ids)
    targets = aligned.variable(config.predictor.output_variable)
    predictions = np.asarray(predictor.predict(features), dtype=np.float64)
    residuals = targets - predictions
    windows: list[WindowEvidence] = []
    for spec in specs:
        positions = list(spec.sample_indices)
        window_sample_indices = positions
        window_features = features[positions]
        window_targets = targets[positions]
        window_predictions = predictions[positions]
        window_residuals = residuals[positions]
        deltas = _feature_deltas(window_features)
        stats = compute_window_statistics(
            [stable_float(item) for item in window_residuals],
            feature_deltas=[
                [stable_float(item) for item in row] for row in deltas
            ],
            coefficients=predictor.coefficients,
            config=config.statistics,
        )
        decision = decide_joint(
            stats.input_amplitude,
            stats.drift_score,
            input_threshold=config.predictor.input_amplitude_threshold,
            predictor_threshold=config.predictor.drift_threshold,
            config=config.thresholds,
        )
        payload = {
            "window_id": spec.window_id,
            "ordinal": spec.ordinal,
            "start_index": spec.start_index,
            "end_index": spec.end_index,
            "start_ms": spec.start_ms,
            "end_ms": spec.end_ms,
            "backtrack_start_ms": spec.backtrack_start_ms,
            "sample_indices": window_sample_indices,
            "raw_residuals": [stable_float(item) for item in window_residuals],
            "window_params": {
                "feature_deltas": [
                    [stable_float(item) for item in row] for row in deltas
                ],
                "coefficients": list(predictor.coefficients),
            },
            "features": [
                [stable_float(item) for item in row] for row in window_features
            ],
            "targets": [stable_float(item) for item in window_targets],
            "predictions": [stable_float(item) for item in window_predictions],
            "statistics": stats.to_dict(),
            "decision": decision.to_dict(),
        }
        digest = sha256_value(payload)
        windows.append(
            WindowEvidence(
                window_id=payload["window_id"],
                ordinal=spec.ordinal,
                start_index=spec.start_index,
                end_index=spec.end_index,
                start_ms=payload["start_ms"],
                end_ms=payload["end_ms"],
                backtrack_start_ms=payload["backtrack_start_ms"],
                sample_indices=tuple(window_sample_indices),
                raw_residuals=tuple(payload["raw_residuals"]),
                feature_deltas=tuple(
                    tuple(row)
                    for row in payload["window_params"]["feature_deltas"]
                ),
                features=tuple(tuple(row) for row in payload["features"]),
                targets=tuple(payload["targets"]),
                predictions=tuple(payload["predictions"]),
                coefficients=tuple(predictor.coefficients),
                statistics=stats,
                decision=decision,
                digest=digest,
            )
        )
    return windows


def build_window_evidence(
    aligned: AlignmentResult,
    predictor: PredictorModel,
    config: PipelineConfig,
) -> list[WindowEvidence]:
    return build_window_evidence_for_specs(
        aligned,
        predictor,
        config,
        build_window_specs(aligned, config),
    )


def recompute_window_score(
    evidence: WindowEvidence, config: StatisticsConfig
) -> float:
    return recompute_drift_score(
        evidence.raw_residuals,
        feature_deltas=evidence.feature_deltas,
        coefficients=evidence.coefficients,
        config=config,
    )


def windows_digest(windows: Iterable[WindowEvidence]) -> str:
    return sha256_value([window.to_dict() for window in windows])

