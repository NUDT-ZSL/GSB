from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .config import EngineConfig
from .errors import DiagnosticError, DiagnosticIssue
from .injection import InjectionController
from .utils import sha256_json


@dataclass(frozen=True)
class Dataset:
    rows: tuple[dict[str, Any], ...]
    sample_ids: tuple[str, ...]
    numeric: np.ndarray
    categories: tuple[dict[str, str], ...]
    labels: np.ndarray

    @property
    def sample_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class StandardizationStats:
    method: str
    columns: tuple[str, ...]
    mean: tuple[float, ...]
    scale: tuple[float, ...]
    minimum: tuple[float, ...]
    maximum: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "columns": list(self.columns),
            "mean": list(self.mean),
            "scale": list(self.scale),
            "minimum": list(self.minimum),
            "maximum": list(self.maximum),
        }


@dataclass(frozen=True)
class PreparedFeatures:
    matrix: np.ndarray
    feature_names: tuple[str, ...]
    stats: StandardizationStats
    category_levels: dict[str, tuple[str, ...]]
    issues: tuple[DiagnosticIssue, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature_names": list(self.feature_names),
            "sample_count": int(self.matrix.shape[0]),
            "feature_count": int(self.matrix.shape[1]),
            "standardization": self.stats.to_dict(),
            "category_levels": {
                key: list(values) for key, values in self.category_levels.items()
            },
            "matrix_sha256": sha256_json(self.matrix.tolist()),
            "issues": [issue.to_dict() for issue in self.issues],
        }


def load_jsonl_dataset(
    path: str | Path,
    config: EngineConfig,
) -> Dataset:
    source = Path(path)
    rows: list[dict[str, Any]] = []
    sample_ids: list[str] = []
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DiagnosticError(
                    "E_DATA_PARSE",
                    "data",
                    f"invalid JSON at line {line_number}: {exc}",
                ) from exc
            if "sample_id" not in row or "label" not in row:
                raise DiagnosticError(
                    "E_DATA_SCHEMA",
                    "data",
                    f"line {line_number} lacks sample_id or label",
                )
            sample_id = str(row["sample_id"])
            if sample_id in sample_ids:
                raise DiagnosticError(
                    "E_DATA_DUPLICATE_ID",
                    "data",
                    f"duplicate sample_id {sample_id!r}",
                )
            if int(row["label"]) not in (0, 1):
                raise DiagnosticError(
                    "E_DATA_LABEL",
                    "data",
                    f"label must be binary at sample {sample_id}",
                )
            for column in config.numeric_columns:
                if column not in row:
                    raise DiagnosticError(
                        "E_DATA_SCHEMA",
                        "data",
                        f"numeric column {column!r} is absent at {sample_id}",
                    )
            for column in config.category_columns:
                if column not in row:
                    raise DiagnosticError(
                        "E_DATA_SCHEMA",
                        "data",
                        f"category column {column!r} is absent at {sample_id}",
                    )
            sample_ids.append(sample_id)
            rows.append(row)
    if not rows:
        raise DiagnosticError("E_DATA_EMPTY", "data", "dataset contains no samples")
    numeric = np.asarray(
        [
            [float(row[column]) for column in config.numeric_columns]
            for row in rows
        ],
        dtype=np.float64,
    )
    if not np.isfinite(numeric).all():
        raise DiagnosticError(
            "E_DATA_NONFINITE", "data", "numeric features must be finite"
        )
    categories = tuple(
        {
            column: str(row[column])
            for column in config.category_columns
        }
        for row in rows
    )
    labels = np.asarray([int(row["label"]) for row in rows], dtype=np.int8)
    return Dataset(
        rows=tuple(rows),
        sample_ids=tuple(sample_ids),
        numeric=numeric,
        categories=categories,
        labels=labels,
    )


def _fit_stats(
    numeric: np.ndarray,
    columns: tuple[str, ...],
    method: str,
) -> StandardizationStats:
    means = np.mean(numeric, axis=0, dtype=np.float64)
    mins = np.min(numeric, axis=0)
    maxs = np.max(numeric, axis=0)
    if method == "zscore":
        scales = np.std(numeric, axis=0, ddof=0, dtype=np.float64)
        scales = np.where(scales == 0.0, 1.0, scales)
    elif method == "minmax":
        ranges = maxs - mins
        scales = np.where(ranges == 0.0, 1.0, ranges)
    else:
        raise DiagnosticError(
            "E_CONFIG_INVALID", "config", f"unknown standardization {method!r}"
        )
    return StandardizationStats(
        method=method,
        columns=columns,
        mean=tuple(float(item) for item in means),
        scale=tuple(float(item) for item in scales),
        minimum=tuple(float(item) for item in mins),
        maximum=tuple(float(item) for item in maxs),
    )


def prepare_features(
    dataset: Dataset,
    config: EngineConfig,
    injector: InjectionController | None = None,
) -> PreparedFeatures:
    controller = injector or InjectionController()
    rows = controller.mutate_rows(list(dataset.rows))
    numeric = dataset.numeric
    stats = _fit_stats(numeric, config.numeric_columns, config.standardization)
    if config.standardization == "zscore":
        scaled_numeric = (
            numeric - np.asarray(stats.mean, dtype=np.float64)
        ) / np.asarray(stats.scale, dtype=np.float64)
    else:
        minimum = np.asarray(stats.minimum, dtype=np.float64)
        scaled_numeric = (
            numeric - minimum
        ) / np.asarray(stats.scale, dtype=np.float64)
    columns: list[np.ndarray] = [
        scaled_numeric[:, index] for index in range(scaled_numeric.shape[1])
    ]
    feature_names: list[str] = list(config.numeric_columns)
    category_levels: dict[str, tuple[str, ...]] = {}
    issues: list[DiagnosticIssue] = []
    for column in config.category_columns:
        configured = config.category_vocab.get(column)
        if configured:
            levels = tuple(configured)
        else:
            levels = tuple(sorted({str(row[column]) for row in rows}))
        category_levels[column] = levels
        lookup = {value: index for index, value in enumerate(levels)}
        encoded = np.zeros((len(rows), len(levels)), dtype=np.float64)
        for row_index, row in enumerate(rows):
            value = str(row[column])
            position = lookup.get(value)
            if position is None:
                issue = controller.record_unknown_category(
                    row_index=row_index, column=column, value=value
                )
                issues.append(issue)
                continue
            encoded[row_index, position] = 1.0
        for level in levels:
            feature_names.append(f"{column}={level}")
            columns.append(encoded[:, lookup[level]])
    matrix = (
        np.column_stack(columns)
        if columns
        else np.empty((len(rows), 0), dtype=np.float64)
    )
    return PreparedFeatures(
        matrix=matrix,
        feature_names=tuple(feature_names),
        stats=stats,
        category_levels=category_levels,
        issues=tuple(issues),
    )

