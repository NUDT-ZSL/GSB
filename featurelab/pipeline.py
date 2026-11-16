"""Deterministic selection preparation and cross-validation fold evaluation."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Tuple

import numpy as np

from .cache import CacheBundle
from .config import ExperimentConfig, PlanConfig
from .data import Dataset
from .encoding import (
    FeatureArtifact,
    ResolvedEncoding,
    combined_matrix,
    encode_column,
    resolve_plan_encodings,
)
from .errors import E_EVALUATION_FAILED, FeatureLabError
from .selector import SelectorState, select_features
from .utils import fsum, fingerprint, matrix_fingerprint


@dataclass(frozen=True)
class Preparation:
    plan_id: str
    resolved: Tuple[ResolvedEncoding, ...]
    feature_names: Tuple[str, ...]
    selection: SelectorState
    full_matrix_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "resolved": [item.to_dict() for item in self.resolved],
            "feature_names": list(self.feature_names),
            "selection": self.selection.to_dict(),
            "full_matrix_fingerprint": self.full_matrix_fingerprint,
        }


@dataclass(frozen=True)
class FoldOutcome:
    fold: int
    stable_key: str
    metrics: Mapping[str, float]
    n_train: int
    n_valid: int
    predictions_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fold": self.fold,
            "stable_key": self.stable_key,
            "metrics": dict(self.metrics),
            "n_train": self.n_train,
            "n_valid": self.n_valid,
            "predictions_fingerprint": self.predictions_fingerprint,
        }


def fold_assignments(n_rows: int, folds: int, seed: int) -> np.ndarray:
    permutation = np.random.default_rng(seed).permutation(n_rows)
    assignments = np.empty(n_rows, dtype=np.int64)
    assignments[permutation] = np.arange(n_rows, dtype=np.int64) % folds
    return assignments


def _encoding_params(resolved: ResolvedEncoding, plan: PlanConfig) -> dict[str, Any]:
    params: dict[str, Any] = {
        "requested_method": resolved.requested_method,
        "resolved_method": resolved.actual_method,
        "feature_names": list(resolved.feature_names),
        "categories": list(resolved.categories),
        "has_missing": resolved.has_missing,
        "fallback_used": resolved.fallback_used,
    }
    if resolved.actual_method == "one_hot":
        params["one_hot"] = plan.one_hot.to_dict()
        params["max_cardinality"] = plan.max_cardinality
        params["cardinality_policy"] = plan.cardinality_policy
        params["fallback_method"] = plan.fallback_method
    elif resolved.actual_method == "frequency":
        params["frequency"] = plan.frequency.to_dict()
    elif resolved.actual_method == "target":
        params["target_encoding"] = plan.target_encoding.to_dict()
    return params


def feature_cache_key(
    snapshot_fingerprint: str,
    resolved: ResolvedEncoding,
    plan: PlanConfig,
    context: str,
) -> str:
    return fingerprint(
        {
            "column_snapshot": snapshot_fingerprint,
            "encoding": _encoding_params(resolved, plan),
            "selector": plan.selector.to_dict(),
            "context": context,
        }
    )


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -40.0, 40.0)))


def _macro_f1(actual: np.ndarray, predicted: np.ndarray) -> float:
    scores: list[float] = []
    for label in (0, 1):
        true_positive = int(np.sum((actual == label) & (predicted == label)))
        false_positive = int(np.sum((actual != label) & (predicted == label)))
        false_negative = int(np.sum((actual == label) & (predicted != label)))
        denominator = 2 * true_positive + false_positive + false_negative
        scores.append(0.0 if denominator == 0 else 2.0 * true_positive / denominator)
    return fsum(scores) / 2.0


def _fit_predict(
    train_x: np.ndarray,
    train_y: np.ndarray,
    valid_x: np.ndarray,
    *,
    ridge_alpha: float = 0.05,
) -> tuple[np.ndarray, np.ndarray]:
    if train_x.ndim != 2 or train_x.shape[1] == 0:
        raise FeatureLabError(
            E_EVALUATION_FAILED,
            "no selected features remain for model fitting",
        )
    means = np.mean(train_x, axis=0)
    scales = np.std(train_x, axis=0)
    scales = np.where(scales > 0.0, scales, 1.0)
    train_standardized = (train_x - means) / scales
    valid_standardized = (valid_x - means) / scales
    target_rate = float(np.mean(train_y))
    train_design = np.column_stack(
        [np.ones(train_x.shape[0], dtype=np.float64), train_standardized]
    )
    valid_design = np.column_stack(
        [np.ones(valid_x.shape[0], dtype=np.float64), valid_standardized]
    )
    intercept = math.log(max(target_rate, 1e-9) / max(1.0 - target_rate, 1e-9))
    coefficients = np.zeros(train_x.shape[1] + 1, dtype=np.float64)
    coefficients[0] = intercept
    regularization = np.full(coefficients.shape, ridge_alpha, dtype=np.float64)
    regularization[0] = 0.0
    for _ in range(50):
        linear = np.clip(train_design @ coefficients, -40.0, 40.0)
        fitted = _sigmoid(linear)
        gradient = train_design.T @ (fitted - train_y.astype(np.float64))
        gradient += regularization * coefficients
        weights = fitted * (1.0 - fitted)
        hessian = train_design.T @ (train_design * weights[:, None])
        hessian += np.diag(regularization + 1e-12)
        step, _, _, _ = np.linalg.lstsq(hessian, gradient, rcond=None)
        coefficients = coefficients - step
        if float(np.max(np.abs(step))) < 1e-10:
            break
    probabilities = _sigmoid(np.clip(valid_design @ coefficients, -40.0, 40.0))
    predictions = (probabilities >= 0.5).astype(np.int64)
    return predictions, probabilities


def _metrics(actual: np.ndarray, predicted: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    accuracy = float(np.mean(actual == predicted)) if actual.size else 0.0
    clipped = np.clip(probabilities, 1e-12, 1.0 - 1e-12)
    log_loss = -float(
        np.mean(actual * np.log(clipped) + (1 - actual) * np.log(1.0 - clipped))
    )
    return {
        "accuracy": accuracy,
        "macro_f1": _macro_f1(actual, predicted),
        "log_loss": log_loss,
    }


class EvaluationPipeline:
    def __init__(
        self,
        dataset: Dataset,
        experiment: ExperimentConfig,
        caches: CacheBundle,
    ) -> None:
        self.dataset = dataset
        self.experiment = experiment
        self.caches = caches
        self._splits = fold_assignments(
            dataset.n_rows, experiment.cv_folds, experiment.cv_seed
        )
        self._y = np.asarray(dataset.target_values, dtype=np.int64)
        self._row_indices = np.arange(dataset.n_rows, dtype=np.int64)

    def _feature_artifact(
        self,
        plan: PlanConfig,
        resolved: ResolvedEncoding,
        context: str,
        fit_indices: np.ndarray,
        output_indices: np.ndarray,
    ) -> FeatureArtifact:
        snapshot = self.dataset.column(resolved.column)
        key = feature_cache_key(
            snapshot.content_fingerprint, resolved, plan, context
        )
        namespace = f"feature:{resolved.column}:{context}"
        return self.caches.features.get_or_compute(
            namespace,
            key,
            lambda: encode_column(
                snapshot,
                resolved,
                plan,
                fit_indices,
                output_indices,
                self._y,
            ),
        )

    def prepare(self, plan: PlanConfig) -> Preparation:
        if self.experiment.task_delay_ms:
            time.sleep(self.experiment.task_delay_ms / 1000.0)
        resolved = resolve_plan_encodings(self.dataset.columns, plan)
        context = f"full:{self.dataset.fingerprint}:{self.experiment.cv_seed}"
        artifacts = [
            self._feature_artifact(
                plan,
                item,
                context,
                self._row_indices,
                self._row_indices,
            )
            for item in resolved
        ]
        feature_names, matrix = combined_matrix(artifacts)
        if not feature_names:
            raise FeatureLabError(E_EVALUATION_FAILED, "no encoded features were produced")
        matrix_fp = matrix_fingerprint(matrix)
        target_fp = matrix_fingerprint(self._y.reshape(-1, 1))
        selector_context = f"selector:full:{self.dataset.fingerprint}"
        selector_key = fingerprint(
            {
                "feature_names": list(feature_names),
                "matrix": matrix_fp,
                "target": target_fp,
                "params": plan.selector.to_dict(),
            }
        )
        def validate_selection(candidate: SelectorState) -> bool:
            return (
                candidate.input_features == feature_names
                and candidate.input_matrix_fingerprint == matrix_fp
                and candidate.target_fingerprint == target_fp
                and candidate.params_fingerprint == fingerprint(plan.selector.to_dict())
            )

        selection = self.caches.selectors.get_or_compute_validated(
            selector_context,
            selector_key,
            lambda: select_features(feature_names, matrix, self._y, plan.selector),
            validate_selection,
        )
        return Preparation(
            plan_id=plan.plan_id,
            resolved=resolved,
            feature_names=feature_names,
            selection=selection,
            full_matrix_fingerprint=matrix_fp,
        )

    def fold_task(self, plan: PlanConfig, preparation: Preparation, fold: int) -> FoldOutcome:
        if self.experiment.task_delay_ms:
            time.sleep(self.experiment.task_delay_ms / 1000.0)
        valid_indices = self._row_indices[self._splits == fold]
        train_indices = self._row_indices[self._splits != fold]
        if not valid_indices.size or not train_indices.size:
            raise FeatureLabError(
                E_EVALUATION_FAILED,
                "cross-validation produced an empty train or validation partition",
                details={"fold": fold},
            )
        output_indices = np.concatenate([train_indices, valid_indices])
        context = (
            f"cv:{self.experiment.cv_seed}:{self.experiment.cv_folds}:{fold}:"
            f"{fingerprint([int(value) for value in train_indices])}"
        )
        artifacts = [
            self._feature_artifact(
                plan, item, context, train_indices, output_indices
            )
            for item in preparation.resolved
        ]
        names, matrix = combined_matrix(artifacts)
        name_to_index = {name: index for index, name in enumerate(names)}
        missing = [name for name in preparation.selection.selected_features if name not in name_to_index]
        if missing:
            raise FeatureLabError(
                E_EVALUATION_FAILED,
                "selected feature schema changed between full data and CV fold",
                details={"missing": missing, "fold": fold},
            )
        selected_indexes = [
            name_to_index[name] for name in preparation.selection.selected_features
        ]
        split_point = int(train_indices.size)
        train_x = matrix[:split_point, selected_indexes]
        valid_x = matrix[split_point:, selected_indexes]
        train_y = self._y[train_indices]
        valid_y = self._y[valid_indices]
        predictions, probabilities = _fit_predict(train_x, train_y, valid_x)
        metrics = _metrics(valid_y, predictions, probabilities)
        stable_key = f"{plan.plan_id}:fold:{fold:04d}"
        return FoldOutcome(
            fold=fold,
            stable_key=stable_key,
            metrics=metrics,
            n_train=int(train_indices.size),
            n_valid=int(valid_indices.size),
            predictions_fingerprint=fingerprint(predictions.tolist()),
        )


def aggregate_folds(plan_id: str, outcomes: Sequence[FoldOutcome]) -> dict[str, Any]:
    ordered = sorted(outcomes, key=lambda item: item.stable_key)
    if not ordered:
        return {
            "plan_id": plan_id,
            "metrics": None,
            "fold_metrics": [],
        }
    metrics = {
        "accuracy": fsum(item.metrics["accuracy"] for item in ordered) / len(ordered),
        "macro_f1": fsum(item.metrics["macro_f1"] for item in ordered) / len(ordered),
        "log_loss": fsum(item.metrics["log_loss"] for item in ordered) / len(ordered),
        "folds": len(ordered),
    }
    return {
        "plan_id": plan_id,
        "metrics": metrics,
        "fold_metrics": [item.to_dict() for item in ordered],
    }


def rank_key(metrics: Mapping[str, Any], plan_id: str) -> tuple[float, float, str]:
    return (-float(metrics["accuracy"]), float(metrics["log_loss"]), plan_id)
