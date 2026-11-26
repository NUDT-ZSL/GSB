"""Minimal drift closures and closure-local incremental retraining."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .canonical import sha256_value, stable_float
from .config import PipelineConfig
from .snapshots import PredictorModel
from .windows import WindowEvidence


@dataclass(frozen=True)
class DriftClosure:
    closure_id: str
    covered_windows: tuple[str, ...]
    event_windows: tuple[str, ...]
    window_ordinals: tuple[int, ...]
    sample_indices: tuple[int, ...]
    recompute_start_index: int
    recompute_end_index: int
    recompute_start_ms: int
    recompute_end_ms: int
    max_drift_score: float
    business_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "closure_id": self.closure_id,
            "covered_windows": list(self.covered_windows),
            "event_windows": list(self.event_windows),
            "window_ordinals": list(self.window_ordinals),
            "sample_indices": list(self.sample_indices),
            "recompute_range": {
                "start_index": self.recompute_start_index,
                "end_index": self.recompute_end_index,
                "start_ms": self.recompute_start_ms,
                "end_ms": self.recompute_end_ms,
            },
            "max_drift_score": self.max_drift_score,
            "business_codes": list(self.business_codes),
        }


@dataclass(frozen=True)
class ClosureRetrain:
    closure: DriftClosure
    model_before: PredictorModel
    model_after: PredictorModel
    old_predictions: dict[str, tuple[float, ...]]
    new_predictions: dict[str, tuple[float, ...]]
    retrain_samples: tuple[tuple[float, ...], ...]
    retrain_targets: tuple[float, ...]
    drift_scores: dict[str, float]
    result_digest: str
    business_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "closure": self.closure.to_dict(),
            "model_before": self.model_before.to_dict(),
            "model_after": self.model_after.to_dict(),
            "old_predictions": {
                key: list(value) for key, value in sorted(self.old_predictions.items())
            },
            "new_predictions": {
                key: list(value) for key, value in sorted(self.new_predictions.items())
            },
            "retrain_samples": [list(row) for row in self.retrain_samples],
            "retrain_targets": list(self.retrain_targets),
            "drift_scores": dict(sorted(self.drift_scores.items())),
            "result_digest": self.result_digest,
            "business_codes": list(self.business_codes),
        }


@dataclass(frozen=True)
class RetrainOutcome:
    closures: tuple[DriftClosure, ...]
    retrains: tuple[ClosureRetrain, ...]
    window_predictions: dict[str, tuple[float, ...]]
    reused_windows: tuple[str, ...]
    recomputed_windows: tuple[str, ...]
    business_codes: tuple[str, ...]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "closures": [closure.to_dict() for closure in self.closures],
            "retrains": [retrain.to_dict() for retrain in self.retrains],
            "window_predictions": {
                key: list(value)
                for key, value in sorted(self.window_predictions.items())
            },
            "reused_windows": list(self.reused_windows),
            "recomputed_windows": list(self.recomputed_windows),
            "business_codes": list(self.business_codes),
            "digest": self.digest,
        }


def _drift_sort_key(window: WindowEvidence) -> tuple[float, int, str]:
    return (-window.statistics.drift_score, window.start_ms, window.window_id)


def build_closures(
    windows: Iterable[WindowEvidence],
    *,
    closure_radius: int,
) -> tuple[DriftClosure, ...]:
    window_list = sorted(windows, key=lambda item: item.ordinal)
    if not window_list:
        return ()
    drift_windows = sorted(
        (item for item in window_list if item.decision.drift),
        key=_drift_sort_key,
    )
    if not drift_windows:
        return ()

    intervals: list[dict[str, Any]] = []
    for event in drift_windows:
        start_ordinal = max(0, event.ordinal - closure_radius)
        end_ordinal = min(
            len(window_list) - 1, event.ordinal + closure_radius
        )
        intervals.append(
            {
                "start": start_ordinal,
                "end": end_ordinal,
                "events": [event.window_id],
                "scores": [event.statistics.drift_score],
                "first_start_ms": event.start_ms,
            }
        )
    intervals.sort(
        key=lambda item: (
            item["start"],
            item["first_start_ms"],
            item["end"],
        )
    )
    merged: list[dict[str, Any]] = []
    for interval in intervals:
        if merged and interval["start"] <= merged[-1]["end"] + 1:
            merged[-1]["end"] = max(merged[-1]["end"], interval["end"])
            merged[-1]["events"].extend(interval["events"])
            merged[-1]["scores"].extend(interval["scores"])
        else:
            merged.append(
                {
                    "start": interval["start"],
                    "end": interval["end"],
                    "events": list(interval["events"]),
                    "scores": list(interval["scores"]),
                    "first_start_ms": interval["first_start_ms"],
                }
            )

    closures: list[DriftClosure] = []
    for index, interval in enumerate(merged, 1):
        covered = window_list[interval["start"] : interval["end"] + 1]
        sample_indices = sorted(
            {
                index
                for window in covered
                for index in window.sample_indices
            }
        )
        event_ids = tuple(sorted(set(interval["events"]), key=lambda item: item))
        closures.append(
            DriftClosure(
                closure_id=f"closure-{index:04d}",
                covered_windows=tuple(window.window_id for window in covered),
                event_windows=event_ids,
                window_ordinals=tuple(window.ordinal for window in covered),
                sample_indices=tuple(sample_indices),
                recompute_start_index=min(window.start_index for window in covered),
                recompute_end_index=max(window.end_index for window in covered),
                recompute_start_ms=min(window.start_ms for window in covered),
                recompute_end_ms=max(window.end_ms for window in covered),
                max_drift_score=max(interval["scores"]),
                business_codes=(
                    "RETRAIN_CLOSURE_CREATED",
                    "CLOSURE_SAMPLES_RESTRICTED",
                ),
            )
        )
    return tuple(closures)


def incremental_ridge_update(
    model: PredictorModel,
    features: np.ndarray,
    targets: np.ndarray,
    config: PipelineConfig,
    *,
    trained_until_ms: int,
) -> PredictorModel:
    if features.size == 0:
        return model
    design = np.column_stack(
        [features, np.ones(features.shape[0], dtype=np.float64)]
    )
    old_theta = np.asarray(
        list(model.coefficients) + [model.intercept], dtype=np.float64
    )
    l2 = config.retrain.l2
    gram = design.T @ design
    regularizer = np.eye(design.shape[1], dtype=np.float64) * l2
    theta = np.linalg.solve(gram + regularizer, design.T @ targets + l2 * old_theta)
    theta = np.asarray(
        [stable_float(item) for item in theta], dtype=np.float64
    )
    return model.with_update(
        coefficients=theta[:-1],
        intercept=float(theta[-1]),
        retrain_generation=model.retrain_generation + 1,
        trained_until_ms=trained_until_ms,
        origin="incremental_ridge_closure",
    )


def retrain_closures(
    windows: Iterable[WindowEvidence],
    predictor: PredictorModel,
    config: PipelineConfig,
) -> RetrainOutcome:
    window_list = sorted(windows, key=lambda item: item.ordinal)
    by_id = {window.window_id: window for window in window_list}
    closures = build_closures(
        window_list, closure_radius=config.retrain.closure_radius
    )
    window_predictions = {
        window.window_id: window.predictions for window in window_list
    }
    if not closures:
        digest = sha256_value(
            {
                "closures": [],
                "window_predictions": {
                    key: list(value)
                    for key, value in sorted(window_predictions.items())
                },
            }
        )
        return RetrainOutcome(
            closures=(),
            retrains=(),
            window_predictions=window_predictions,
            reused_windows=tuple(window.window_id for window in window_list),
            recomputed_windows=(),
            business_codes=("NO_DRIFT_CLOSURE", "PREDICTIONS_REUSED"),
            digest=digest,
        )

    working_model = predictor
    retrains: list[ClosureRetrain] = []
    recomputed: set[str] = set()
    for closure in closures:
        covered = [by_id[window_id] for window_id in closure.covered_windows]
        sample_indices = list(closure.sample_indices)
        sample_to_feature: dict[int, tuple[float, ...]] = {}
        sample_to_target: dict[int, float] = {}
        for window in covered:
            for offset, sample_index in enumerate(window.sample_indices):
                sample_to_feature[sample_index] = window.features[offset]
                sample_to_target[sample_index] = window.targets[offset]
        features = np.asarray(
            [sample_to_feature[index] for index in sample_indices],
            dtype=np.float64,
        )
        targets = np.asarray(
            [sample_to_target[index] for index in sample_indices],
            dtype=np.float64,
        )
        model_before = working_model
        model_after = incremental_ridge_update(
            model_before,
            features,
            targets,
            config,
            trained_until_ms=closure.recompute_end_ms,
        )
        old_predictions: dict[str, tuple[float, ...]] = {}
        new_predictions: dict[str, tuple[float, ...]] = {}
        drift_scores: dict[str, float] = {}
        for window in covered:
            old_predictions[window.window_id] = window.predictions
            predicted = model_after.predict(np.asarray(window.features))
            new_predictions[window.window_id] = tuple(
                stable_float(item) for item in predicted
            )
            window_predictions[window.window_id] = new_predictions[window.window_id]
            drift_scores[window.window_id] = window.statistics.drift_score
            recomputed.add(window.window_id)
        payload = closure_result_payload(
            closure=closure,
            model_before=model_before,
            model_after=model_after,
            old_predictions=old_predictions,
            new_predictions=new_predictions,
            retrain_samples=features,
            retrain_targets=targets,
            drift_scores=drift_scores,
        )
        result_digest = sha256_value(payload)
        retrains.append(
            ClosureRetrain(
                closure=closure,
                model_before=model_before,
                model_after=model_after,
                old_predictions=old_predictions,
                new_predictions=new_predictions,
                retrain_samples=tuple(tuple(row) for row in features),
                retrain_targets=tuple(stable_float(item) for item in targets),
                drift_scores=drift_scores,
                result_digest=result_digest,
                business_codes=(
                    "RETRAIN_CLOSURE_CREATED",
                    "CLOSURE_SAMPLES_RESTRICTED",
                    "PREDICTION_REPLACED",
                ),
            )
        )
        working_model = model_after

    reused = tuple(
        window.window_id
        for window in window_list
        if window.window_id not in recomputed
    )
    payload = {
        "closures": [closure.to_dict() for closure in closures],
        "window_predictions": {
            key: list(value) for key, value in sorted(window_predictions.items())
        },
        "recomputed": sorted(recomputed),
        "reused": list(reused),
    }
    return RetrainOutcome(
        closures=closures,
        retrains=tuple(retrains),
        window_predictions=window_predictions,
        reused_windows=reused,
        recomputed_windows=tuple(sorted(recomputed)),
        business_codes=(
            "RETRAIN_CLOSURES_COMMITTED",
            "NON_CLOSURE_PREDICTIONS_REUSED",
        ),
        digest=sha256_value(payload),
    )


def verify_non_closure_reuse(
    windows: Iterable[WindowEvidence], outcome: RetrainOutcome
) -> bool:
    by_id = {window.window_id: window for window in windows}
    return all(
        outcome.window_predictions[window_id] == by_id[window_id].predictions
        for window_id in outcome.reused_windows
    )


def closure_result_payload(
    *,
    closure: DriftClosure,
    model_before: PredictorModel,
    model_after: PredictorModel,
    old_predictions: dict[str, tuple[float, ...]],
    new_predictions: dict[str, tuple[float, ...]],
    retrain_samples: np.ndarray | Iterable[Iterable[float]],
    retrain_targets: np.ndarray | Iterable[float],
    drift_scores: dict[str, float],
) -> dict[str, Any]:
    return {
        "closure": closure.to_dict(),
        "model_before": model_before.to_dict(),
        "model_after": model_after.to_dict(),
        "old_predictions": {
            key: list(value) for key, value in sorted(old_predictions.items())
        },
        "new_predictions": {
            key: list(value) for key, value in sorted(new_predictions.items())
        },
        "retrain_samples": [
            [stable_float(item) for item in row] for row in retrain_samples
        ],
        "retrain_targets": [
            stable_float(item) for item in retrain_targets
        ],
        "drift_scores": dict(sorted(drift_scores.items())),
    }


def closure_result_digest(
    *,
    closure: DriftClosure,
    model_before: PredictorModel,
    model_after: PredictorModel,
    old_predictions: dict[str, tuple[float, ...]],
    new_predictions: dict[str, tuple[float, ...]],
    retrain_samples: np.ndarray | Iterable[Iterable[float]],
    retrain_targets: np.ndarray | Iterable[float],
    drift_scores: dict[str, float],
) -> str:
    return sha256_value(
        closure_result_payload(
            closure=closure,
            model_before=model_before,
            model_after=model_after,
            old_predictions=old_predictions,
            new_predictions=new_predictions,
            retrain_samples=retrain_samples,
            retrain_targets=retrain_targets,
            drift_scores=drift_scores,
        )
    )

