"""Deterministic feature encoders with leakage-safe target encoding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

import numpy as np

from .config import PlanConfig
from .data import ColumnSnapshot
from .errors import E_CARDINALITY_EXCEEDED, FeatureLabError, config_error
from .utils import fingerprint, matrix_fingerprint


@dataclass(frozen=True)
class ResolvedEncoding:
    column: str
    kind: str
    requested_method: str
    actual_method: str
    feature_names: Tuple[str, ...]
    categories: Tuple[str, ...]
    has_missing: bool
    fallback_used: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "kind": self.kind,
            "requested_method": self.requested_method,
            "resolved_method": self.actual_method,
            "feature_names": list(self.feature_names),
            "categories": list(self.categories),
            "has_missing": self.has_missing,
            "fallback_used": self.fallback_used,
        }


@dataclass(frozen=True)
class FeatureArtifact:
    column: str
    actual_method: str
    feature_names: Tuple[str, ...]
    matrix: np.ndarray
    source_fingerprint: str
    params_fingerprint: str
    metadata: Mapping[str, Any]

    @property
    def matrix_fingerprint(self) -> str:
        return matrix_fingerprint(self.matrix)

    def immutable_matrix(self) -> np.ndarray:
        array = np.asarray(self.matrix, dtype=np.float64).copy()
        array.flags.writeable = False
        return array

    def to_cache_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "actual_method": self.actual_method,
            "feature_names": list(self.feature_names),
            "matrix": np.asarray(self.matrix, dtype=np.float64).tolist(),
            "source_fingerprint": self.source_fingerprint,
            "params_fingerprint": self.params_fingerprint,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_cache_dict(cls, raw: Mapping[str, Any]) -> "FeatureArtifact":
        try:
            matrix = np.asarray(raw["matrix"], dtype=np.float64)
            return cls(
                column=str(raw["column"]),
                actual_method=str(raw["actual_method"]),
                feature_names=tuple(str(name) for name in raw["feature_names"]),
                matrix=matrix,
                source_fingerprint=str(raw["source_fingerprint"]),
                params_fingerprint=str(raw["params_fingerprint"]),
                metadata=dict(raw.get("metadata", {})),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid cached feature artifact") from exc


def _missing_mask(snapshot: ColumnSnapshot) -> np.ndarray:
    return np.asarray([value is None for value in snapshot.values], dtype=bool)


def _known_categories(snapshot: ColumnSnapshot) -> Tuple[str, ...]:
    return tuple(sorted({str(value) for value in snapshot.values if value is not None}))


def resolve_encoding(
    snapshot: ColumnSnapshot,
    requested_method: str,
    plan: PlanConfig,
) -> ResolvedEncoding:
    if snapshot.kind == "numeric":
        if requested_method != "numeric":
            raise config_error(
                "numeric column must use numeric encoding",
                column=snapshot.name,
                method=requested_method,
            )
        return ResolvedEncoding(
            column=snapshot.name,
            kind="numeric",
            requested_method="numeric",
            actual_method="numeric",
            feature_names=(snapshot.name,),
            categories=(),
            has_missing=bool(np.any(_missing_mask(snapshot))),
            fallback_used=False,
        )

    if requested_method not in {"one_hot", "target", "frequency"}:
        raise config_error(
            "unsupported categorical encoding",
            column=snapshot.name,
            method=requested_method,
        )
    categories = _known_categories(snapshot)
    has_missing = bool(np.any(_missing_mask(snapshot)))
    actual_method = requested_method
    fallback_used = False
    if requested_method == "one_hot" and len(categories) > plan.max_cardinality:
        if plan.cardinality_policy == "error":
            raise FeatureLabError(
                E_CARDINALITY_EXCEEDED,
                "one-hot cardinality exceeds the configured maximum",
                details={
                    "column": snapshot.name,
                    "cardinality": len(categories),
                    "max_cardinality": plan.max_cardinality,
                },
            )
        actual_method = plan.fallback_method
        fallback_used = True

    if actual_method == "one_hot":
        categories_to_encode = list(categories)
        if has_missing:
            if plan.one_hot.missing_label in categories_to_encode:
                raise config_error(
                    "one_hot.missing_label collides with a real category",
                    column=snapshot.name,
                    label=plan.one_hot.missing_label,
                )
            categories_to_encode.append(plan.one_hot.missing_label)
        if plan.one_hot.drop_first and len(categories_to_encode) > 1:
            categories_to_encode = categories_to_encode[1:]
        names = tuple(
            f"{snapshot.name}={category}" for category in categories_to_encode
        )
        return ResolvedEncoding(
            column=snapshot.name,
            kind="categorical",
            requested_method=requested_method,
            actual_method=actual_method,
            feature_names=names,
            categories=tuple(categories_to_encode),
            has_missing=has_missing,
            fallback_used=fallback_used,
        )
    if actual_method == "frequency":
        return ResolvedEncoding(
            column=snapshot.name,
            kind="categorical",
            requested_method=requested_method,
            actual_method=actual_method,
            feature_names=(f"{snapshot.name}__frequency",),
            categories=categories,
            has_missing=has_missing,
            fallback_used=fallback_used,
        )
    return ResolvedEncoding(
        column=snapshot.name,
        kind="categorical",
        requested_method=requested_method,
        actual_method="target",
        feature_names=(f"{snapshot.name}__target",),
        categories=categories,
        has_missing=has_missing,
        fallback_used=fallback_used,
    )


def resolve_plan_encodings(
    dataset_columns: Mapping[str, ColumnSnapshot],
    plan: PlanConfig,
) -> Tuple[ResolvedEncoding, ...]:
    return tuple(
        resolve_encoding(dataset_columns[column], method, plan)
        for column, method in plan.encodings
    )


def _median_impute(values: np.ndarray, fit_indices: np.ndarray, target_indices: np.ndarray) -> np.ndarray:
    train = values[fit_indices]
    observed = train[~np.isnan(train)]
    replacement = float(np.median(observed)) if observed.size else 0.0
    selected = values[target_indices].copy()
    selected[np.isnan(selected)] = replacement
    return selected.astype(np.float64, copy=False)


def _encode_one_hot(
    snapshot: ColumnSnapshot,
    resolved: ResolvedEncoding,
    indices: np.ndarray,
    plan: PlanConfig,
) -> np.ndarray:
    values = snapshot.values
    categories = resolved.categories
    name_to_index = {name: index for index, name in enumerate(categories)}
    matrix = np.zeros((len(indices), len(categories)), dtype=np.float64)
    for row_position, row_index in enumerate(indices):
        value = values[int(row_index)]
        key = plan.one_hot.missing_label if value is None else str(value)
        column_index = name_to_index.get(key)
        if column_index is not None:
            matrix[row_position, column_index] = 1.0
    return matrix


def _encode_frequency(
    snapshot: ColumnSnapshot,
    resolved: ResolvedEncoding,
    fit_indices: np.ndarray,
    indices: np.ndarray,
    plan: PlanConfig,
) -> np.ndarray:
    counts: dict[str, int] = {}
    observed = 0
    for row_index in fit_indices:
        value = snapshot.values[int(row_index)]
        if value is None:
            continue
        text = str(value)
        counts[text] = counts.get(text, 0) + 1
        observed += 1
    result = np.empty((len(indices), 1), dtype=np.float64)
    for row_position, row_index in enumerate(indices):
        value = snapshot.values[int(row_index)]
        if value is None:
            result[row_position, 0] = plan.frequency.missing_code
        elif observed == 0:
            result[row_position, 0] = 0.0
        else:
            result[row_position, 0] = counts.get(str(value), 0) / observed
    return result


def _smoothed_map(
    snapshot: ColumnSnapshot,
    y: np.ndarray,
    fit_indices: np.ndarray,
    smoothing: float,
) -> tuple[dict[str, float], float]:
    if fit_indices.size:
        prior = float(np.mean(y[fit_indices]))
    else:
        prior = 0.0
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for row_index in fit_indices:
        value = snapshot.values[int(row_index)]
        if value is None:
            continue
        category = str(value)
        sums[category] = sums.get(category, 0.0) + float(y[int(row_index)])
        counts[category] = counts.get(category, 0) + 1
    mapping = {
        category: (sums[category] + prior * smoothing)
        / (counts[category] + smoothing)
        for category in sums
    }
    return mapping, prior


def _encode_target(
    snapshot: ColumnSnapshot,
    resolved: ResolvedEncoding,
    fit_indices: np.ndarray,
    indices: np.ndarray,
    y: np.ndarray,
    plan: PlanConfig,
) -> np.ndarray:
    params = plan.target_encoding
    fit_set = {int(index) for index in fit_indices}
    full_map, full_prior = _smoothed_map(snapshot, y, fit_indices, params.smoothing)
    inner_maps: dict[int, tuple[dict[str, float], float]] = {}
    for inner_fold in range(params.inner_folds):
        inner_fit = fit_indices[
            np.asarray(
                [int(index) % params.inner_folds != inner_fold for index in fit_indices],
                dtype=bool,
            )
        ]
        inner_maps[inner_fold] = _smoothed_map(
            snapshot, y, inner_fit, params.smoothing
        )

    result = np.empty((len(indices), 1), dtype=np.float64)
    for row_position, row_index in enumerate(indices):
        row_index = int(row_index)
        value = snapshot.values[row_index]
        if value is None:
            result[row_position, 0] = params.missing_code
            continue
        if row_index in fit_set:
            inner_fold = row_index % params.inner_folds
            mapping, prior = inner_maps[inner_fold]
        else:
            mapping, prior = full_map, full_prior
        result[row_position, 0] = mapping.get(str(value), prior)
    return result


def encode_column(
    snapshot: ColumnSnapshot,
    resolved: ResolvedEncoding,
    plan: PlanConfig,
    fit_indices: np.ndarray,
    indices: np.ndarray,
    y: np.ndarray,
) -> FeatureArtifact:
    fit_indices = np.asarray(fit_indices, dtype=np.int64)
    indices = np.asarray(indices, dtype=np.int64)
    if resolved.actual_method == "numeric":
        numeric = np.asarray(
            [np.nan if value is None else float(value) for value in snapshot.values],
            dtype=np.float64,
        )
        matrix = _median_impute(numeric, fit_indices, indices).reshape(-1, 1)
    elif resolved.actual_method == "one_hot":
        matrix = _encode_one_hot(snapshot, resolved, indices, plan)
    elif resolved.actual_method == "frequency":
        matrix = _encode_frequency(snapshot, resolved, fit_indices, indices, plan)
    elif resolved.actual_method == "target":
        matrix = _encode_target(snapshot, resolved, fit_indices, indices, y, plan)
    else:  # pragma: no cover - guarded by resolver
        raise AssertionError(resolved.actual_method)
    if matrix.size and not np.all(np.isfinite(matrix)):
        raise FeatureLabError(
            "E_EVALUATION_FAILED",
            "encoder produced a non-finite feature value",
            details={"column": snapshot.name, "method": resolved.actual_method},
        )
    params = {
        "requested_method": resolved.requested_method,
        "resolved_method": resolved.actual_method,
        "feature_names": list(resolved.feature_names),
        "max_cardinality": plan.max_cardinality,
        "cardinality_policy": plan.cardinality_policy,
        "fallback_method": plan.fallback_method,
        "one_hot": plan.one_hot.to_dict(),
        "target_encoding": plan.target_encoding.to_dict(),
        "frequency": plan.frequency.to_dict(),
    }
    metadata = {
        "resolved": resolved.to_dict(),
        "fit_rows": int(fit_indices.size),
        "output_rows": int(indices.size),
        "inner_folds": plan.target_encoding.inner_folds,
    }
    matrix.flags.writeable = False
    return FeatureArtifact(
        column=snapshot.name,
        actual_method=resolved.actual_method,
        feature_names=resolved.feature_names,
        matrix=matrix,
        source_fingerprint=snapshot.content_fingerprint,
        params_fingerprint=fingerprint(params),
        metadata=metadata,
    )


def combined_matrix(artifacts: Sequence[FeatureArtifact]) -> tuple[Tuple[str, ...], np.ndarray]:
    names: list[str] = []
    blocks: list[np.ndarray] = []
    for artifact in artifacts:
        names.extend(artifact.feature_names)
        blocks.append(np.asarray(artifact.matrix, dtype=np.float64))
    if len(set(names)) != len(names):
        raise FeatureLabError(
            "E_EVALUATION_FAILED",
            "encoded feature names are not unique",
            details={"feature_names": names},
        )
    if not blocks:
        return tuple(), np.zeros((0, 0), dtype=np.float64)
    return tuple(names), np.hstack(blocks)
