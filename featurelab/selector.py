"""Stateless variance, correlation, and ridge-weight feature selection."""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Mapping, Sequence, Tuple

import numpy as np

from .config import SelectorParams
from .errors import E_EVALUATION_FAILED, FeatureLabError
from .utils import fingerprint, matrix_fingerprint


@dataclass(frozen=True)
class RemovedFeature:
    feature: str
    round: int
    reason: str
    weight: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "round": self.round,
            "reason": self.reason,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class EliminationRound:
    round: int
    reason: str
    removed: Tuple[Mapping[str, Any], ...]
    kept_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round,
            "reason": self.reason,
            "removed": [dict(item) for item in self.removed],
            "kept_count": self.kept_count,
        }


@dataclass(frozen=True)
class SelectorState:
    input_features: Tuple[str, ...]
    selected_features: Tuple[str, ...]
    removed_features: Tuple[RemovedFeature, ...]
    elimination_rounds: Tuple[EliminationRound, ...]
    input_matrix_fingerprint: str
    target_fingerprint: str
    params_fingerprint: str
    state_fingerprint: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_features": list(self.input_features),
            "selected_features": list(self.selected_features),
            "removed_features": [item.to_dict() for item in self.removed_features],
            "elimination_rounds": [item.to_dict() for item in self.elimination_rounds],
            "input_matrix_fingerprint": self.input_matrix_fingerprint,
            "target_fingerprint": self.target_fingerprint,
            "params_fingerprint": self.params_fingerprint,
            "state_fingerprint": self.state_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SelectorState":
        removed = tuple(
            RemovedFeature(
                feature=str(item["feature"]),
                round=int(item["round"]),
                reason=str(item["reason"]),
                weight=None if item.get("weight") is None else float(item["weight"]),
            )
            for item in raw.get("removed_features", [])
        )
        rounds = tuple(
            EliminationRound(
                round=int(item["round"]),
                reason=str(item["reason"]),
                removed=tuple(dict(entry) for entry in item.get("removed", [])),
                kept_count=int(item["kept_count"]),
            )
            for item in raw.get("elimination_rounds", [])
        )
        return cls(
            input_features=tuple(str(name) for name in raw["input_features"]),
            selected_features=tuple(str(name) for name in raw["selected_features"]),
            removed_features=removed,
            elimination_rounds=rounds,
            input_matrix_fingerprint=str(raw["input_matrix_fingerprint"]),
            target_fingerprint=str(raw["target_fingerprint"]),
            params_fingerprint=str(raw["params_fingerprint"]),
            state_fingerprint=str(raw["state_fingerprint"]),
        )


def _variance(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    centered = values - float(np.mean(values))
    return float(np.mean(centered * centered))


def _correlation(left: np.ndarray, right: np.ndarray) -> float:
    left_centered = left - float(np.mean(left))
    right_centered = right - float(np.mean(right))
    denominator = float(
        np.sqrt(np.sum(left_centered * left_centered) * np.sum(right_centered * right_centered))
    )
    if denominator == 0.0:
        return 0.0
    return float(np.sum(left_centered * right_centered) / denominator)


def _ridge_weights(matrix: np.ndarray, target: np.ndarray, alpha: float) -> np.ndarray:
    if matrix.shape[1] == 0:
        return np.zeros(0, dtype=np.float64)
    means = np.mean(matrix, axis=0)
    scales = np.std(matrix, axis=0)
    scales = np.where(scales > 0.0, scales, 1.0)
    standardized = (matrix - means) / scales
    target_centered = target - float(np.mean(target))
    augment_x = np.vstack(
        [standardized, np.sqrt(alpha) * np.eye(standardized.shape[1], dtype=np.float64)]
    )
    augment_y = np.concatenate(
        [target_centered, np.zeros(standardized.shape[1], dtype=np.float64)]
    )
    coefficients, _, _, _ = np.linalg.lstsq(augment_x, augment_y, rcond=None)
    return np.asarray(coefficients, dtype=np.float64)


def select_features(
    feature_names: Sequence[str],
    matrix: np.ndarray,
    target: np.ndarray,
    params: SelectorParams,
) -> SelectorState:
    names = tuple(str(name) for name in feature_names)
    if len(set(names)) != len(names):
        raise FeatureLabError(
            E_EVALUATION_FAILED,
            "selector input feature names are not unique",
            details={"features": list(names)},
        )
    values = np.asarray(matrix, dtype=np.float64)
    y = np.asarray(target, dtype=np.float64)
    if values.ndim != 2 or values.shape[0] != y.shape[0] or values.shape[1] != len(names):
        raise FeatureLabError(
            E_EVALUATION_FAILED,
            "selector input matrix dimensions are inconsistent",
            details={"matrix_shape": list(values.shape), "features": len(names)},
        )
    if values.size and not np.all(np.isfinite(values)):
        raise FeatureLabError(E_EVALUATION_FAILED, "selector input contains non-finite values")

    column_by_name = {name: values[:, index] for index, name in enumerate(names)}
    remaining = set(names)
    removed: list[RemovedFeature] = []
    rounds: list[EliminationRound] = []
    filtered_round: list[dict[str, Any]] = []

    for name in sorted(names):
        variance = _variance(column_by_name[name])
        if variance <= params.variance_threshold:
            remaining.discard(name)
            removed.append(RemovedFeature(name, 0, "variance", None))
            filtered_round.append({"feature": name, "reason": "variance", "variance": variance})

    candidates: list[tuple[float, str, str]] = []
    active_after_variance = sorted(remaining)
    for left, right in itertools.combinations(active_after_variance, 2):
        correlation = _correlation(column_by_name[left], column_by_name[right])
        if abs(correlation) >= params.correlation_threshold:
            candidates.append((abs(correlation), left, right))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    correlation_removed: list[str] = []
    for absolute_correlation, left, right in candidates:
        if left in remaining and right in remaining:
            victim = max(left, right)
            remaining.remove(victim)
            removed.append(RemovedFeature(victim, 0, "correlation", None))
            correlation_removed.append(victim)
            filtered_round.append(
                {
                    "feature": victim,
                    "reason": "correlation",
                    "peer": left if victim == right else right,
                    "absolute_correlation": absolute_correlation,
                }
            )
    if filtered_round:
        rounds.append(
            EliminationRound(
                round=0,
                reason="filters",
                removed=tuple(filtered_round),
                kept_count=len(remaining),
            )
        )

    if not remaining:
        raise FeatureLabError(
            E_EVALUATION_FAILED,
            "all features were removed by configured filters",
            details={"features": list(names)},
        )

    weight_params = params.weight_elimination
    weight_round = 0
    if weight_params.enabled:
        while (
            len(remaining) > weight_params.min_features
            and weight_round < weight_params.max_eliminations
            and len(remaining) > 1
        ):
            active_names = sorted(remaining)
            active_matrix = np.column_stack([column_by_name[name] for name in active_names])
            weights = _ridge_weights(active_matrix, y, weight_params.ridge_alpha)
            weight_round += 1
            victim_index = min(
                range(len(active_names)),
                key=lambda index: (abs(float(weights[index])), active_names[index]),
            )
            victim = active_names[victim_index]
            victim_weight = float(weights[victim_index])
            remaining.remove(victim)
            removed.append(RemovedFeature(victim, weight_round, "weight", victim_weight))
            rounds.append(
                EliminationRound(
                    round=weight_round,
                    reason="weight",
                    removed=(
                        {
                            "feature": victim,
                            "weight": victim_weight,
                            "absolute_weight": abs(victim_weight),
                        },
                    ),
                    kept_count=len(remaining),
                )
            )

    selected = tuple(sorted(remaining))
    params_fp = fingerprint(params.to_dict())
    matrix_fp = matrix_fingerprint(values)
    target_fp = matrix_fingerprint(y.reshape(-1, 1))
    state_data = {
        "input_features": list(names),
        "selected_features": list(selected),
        "removed": [item.to_dict() for item in removed],
        "rounds": [item.to_dict() for item in rounds],
        "input_matrix_fingerprint": matrix_fp,
        "target_fingerprint": target_fp,
        "params_fingerprint": params_fp,
    }
    return SelectorState(
        input_features=names,
        selected_features=selected,
        removed_features=tuple(removed),
        elimination_rounds=tuple(rounds),
        input_matrix_fingerprint=matrix_fp,
        target_fingerprint=target_fp,
        params_fingerprint=params_fp,
        state_fingerprint=fingerprint(state_data),
    )


def rebuild_selector_state(
    feature_names: Sequence[str],
    matrix: np.ndarray,
    target: np.ndarray,
    params: SelectorParams,
) -> SelectorState:
    """Rebuild selection solely from the current inputs and parameters."""

    return select_features(feature_names, matrix, target, params)
