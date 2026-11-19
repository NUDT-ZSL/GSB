"""Deterministic standardization, categorical encoding and chunk preparation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from .config import TrainingConfig
from .data import Dataset, DatasetSplit, category_levels
from .precision import PrecisionEngine, Quantizer, round_ratio_int
from .serialization import sha256_json


@dataclass(frozen=True)
class NumericStats:
    column: str
    method: str
    center: float
    spread: float
    center_units: int | None = None
    spread_units: int | None = None
    minimum: float | None = None
    maximum: float | None = None
    minimum_units: int | None = None
    maximum_units: int | None = None
    constant: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "column": self.column,
            "method": self.method,
            "center": self.center,
            "spread": self.spread,
            "center_units": self.center_units,
            "spread_units": self.spread_units,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "minimum_units": self.minimum_units,
            "maximum_units": self.maximum_units,
            "constant": self.constant,
        }


@dataclass(frozen=True)
class Standardizer:
    engine: PrecisionEngine
    method: str
    stats: dict[str, NumericStats]

    @classmethod
    def fit(
        cls,
        numeric_raw: dict[str, np.ndarray],
        train_indices: Sequence[int],
        engine: PrecisionEngine,
        method: str,
    ) -> "Standardizer":
        stats: dict[str, NumericStats] = {}
        selected = np.asarray(list(train_indices), dtype=np.int64)
        for column, raw_values in numeric_raw.items():
            values = engine.raw_numeric(np.asarray(raw_values, dtype=np.float64)[selected])
            if engine.is_fixed:
                stats[column] = _fit_fixed_stats(column, method, values, engine)
            else:
                stats[column] = _fit_float_stats(column, method, values, engine)
        return cls(engine=engine, method=method, stats=stats)

    def transform(self, numeric_raw: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
        transformed: dict[str, np.ndarray] = {}
        for column, raw_values in numeric_raw.items():
            values = self.engine.raw_numeric(np.asarray(raw_values, dtype=np.float64))
            entry = self.stats[column]
            if self.engine.is_fixed:
                transformed[column] = _transform_fixed(values, entry, self.engine)
            else:
                transformed[column] = _transform_float(values, entry, self.engine)
        return transformed

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "precision": self.engine.name,
            "fixed_scale": self.engine.scale if self.engine.is_fixed else None,
            "rounding_mode": self.engine.rounding_mode if self.engine.is_fixed else None,
            "columns": {name: entry.to_dict() for name, entry in sorted(self.stats.items())},
        }


def _fit_float_stats(
    column: str,
    method: str,
    values: np.ndarray,
    engine: PrecisionEngine,
) -> NumericStats:
    dtype = engine.dtype
    count = dtype.type(values.size)
    if method == "zscore":
        total = np.sum(values, dtype=dtype)
        mean = (total / count).astype(dtype, copy=False)
        centered = (values - mean).astype(dtype, copy=False)
        variance = (
            np.sum((centered * centered).astype(dtype, copy=False), dtype=dtype) / count
        ).astype(dtype, copy=False)
        std = np.sqrt(variance).astype(dtype, copy=False)
        constant = bool(float(std) == 0.0)
        spread = dtype.type(1.0) if constant else std
        return NumericStats(
            column=column,
            method=method,
            center=float(mean),
            spread=float(spread),
            constant=constant,
        )
    minimum = np.min(values)
    maximum = np.max(values)
    span = (maximum - minimum).astype(dtype, copy=False)
    constant = bool(float(span) == 0.0)
    spread = dtype.type(1.0) if constant else span
    return NumericStats(
        column=column,
        method=method,
        center=float(minimum),
        spread=float(spread),
        minimum=float(minimum),
        maximum=float(maximum),
        constant=constant,
    )


def _fit_fixed_stats(
    column: str,
    method: str,
    values: np.ndarray,
    engine: PrecisionEngine,
) -> NumericStats:
    assert engine.quantizer is not None
    scale = engine.scale
    count = int(values.size)
    if method == "zscore":
        total = int(np.sum(values, dtype=np.int64))
        mean_units = int(round_ratio_int(total, count, engine.rounding_mode))
        centered = values - mean_units
        sumsq = int(np.sum(centered * centered, dtype=np.int64))
        std_float = math.sqrt(float(sumsq) / float(count))
        std_units = int(Quantizer(1, engine.rounding_mode).quantize(std_float))
        constant = std_units == 0
        spread_units = 1 if constant else std_units
        return NumericStats(
            column=column,
            method=method,
            center=float(mean_units) / float(scale),
            spread=float(spread_units) / float(scale),
            center_units=mean_units,
            spread_units=spread_units,
            constant=constant,
        )
    minimum_units = int(np.min(values))
    maximum_units = int(np.max(values))
    span_units = maximum_units - minimum_units
    constant = span_units == 0
    spread_units = 1 if constant else span_units
    return NumericStats(
        column=column,
        method=method,
        center=float(minimum_units) / float(scale),
        spread=float(spread_units) / float(scale),
        minimum=float(minimum_units) / float(scale),
        maximum=float(maximum_units) / float(scale),
        minimum_units=minimum_units,
        maximum_units=maximum_units,
        constant=constant,
    )


def _transform_float(
    values: np.ndarray,
    entry: NumericStats,
    engine: PrecisionEngine,
) -> np.ndarray:
    dtype = engine.dtype
    if entry.constant:
        return np.zeros(values.shape, dtype=dtype)
    if entry.method == "zscore":
        result = (values - dtype.type(entry.center)) / dtype.type(entry.spread)
    else:
        result = (values - dtype.type(entry.minimum)) / dtype.type(entry.spread)
    return result.astype(dtype, copy=False)


def _transform_fixed(
    values: np.ndarray,
    entry: NumericStats,
    engine: PrecisionEngine,
) -> np.ndarray:
    if entry.constant:
        return np.zeros(values.shape, dtype=np.int64)
    if entry.method == "zscore":
        numerator = (values - int(entry.center_units)) * engine.scale
        return round_ratio_int(
            numerator, int(entry.spread_units), engine.rounding_mode
        ).astype(np.int64, copy=False)
    numerator = (values - int(entry.minimum_units)) * engine.scale
    return round_ratio_int(
        numerator, int(entry.spread_units), engine.rounding_mode
    ).astype(np.int64, copy=False)


@dataclass
class PreparedData:
    config: TrainingConfig
    engine: PrecisionEngine
    stats_engine: PrecisionEngine
    standardizer: Standardizer
    feature_names: tuple[str, ...]
    train_features: np.ndarray
    train_labels: np.ndarray
    validation_features: np.ndarray
    validation_labels: np.ndarray
    train_global_indices: tuple[int, ...]
    validation_global_indices: tuple[int, ...]
    train_row_ids: tuple[str, ...]
    validation_row_ids: tuple[str, ...]
    chunks: tuple[tuple[int, ...], ...]
    chunk_global_indices: tuple[tuple[int, ...], ...]
    regularized_mask: np.ndarray
    category_levels: dict[str, tuple[str, ...]]
    sample_fingerprint: dict[str, Any]
    chunk_plan_fingerprint: dict[str, Any]

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return self.feature_names

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)

    def chunk_sample_ids(self) -> list[list[str]]:
        return [
            [self.train_row_ids[index] for index in chunk]
            for chunk in self.chunks
        ]

    def statistics_payload(self) -> dict[str, Any]:
        return {
            "standardizer": self.standardizer.to_dict(),
            "compute_precision": self.engine.name,
            "sample_fingerprint": self.sample_fingerprint,
        }

    def chunk_plan_payload(self) -> dict[str, Any]:
        return {
            "chunk_size": self.config.chunk_size,
            "chunk_count": self.chunk_count,
            "chunks_local_indices": [list(chunk) for chunk in self.chunks],
            "chunks_global_indices": [
                list(chunk) for chunk in self.chunk_global_indices
            ],
            "chunk_row_ids": self.chunk_sample_ids(),
            "reduction_order": (
                "chunk_index_ascending; within_chunk_sample_index_ascending; "
                "sequential_left_fold_over_chunk_sums"
            ),
            "full_reduction_order": "sample_index_ascending; single vectorized reduction",
            "sample_fingerprint": self.sample_fingerprint,
            "chunk_plan_sha256": self.chunk_plan_fingerprint["sha256"],
        }


def _build_feature_matrix(
    standardized_numeric: dict[str, np.ndarray],
    categorical_codes: dict[str, np.ndarray],
    levels: dict[str, tuple[str, ...]],
    engine: PrecisionEngine,
    config: TrainingConfig,
) -> tuple[np.ndarray, tuple[str, ...]]:
    columns: list[np.ndarray] = []
    names: list[str] = []
    standardized_float = {
        name: engine.to_float(values) for name, values in standardized_numeric.items()
    }

    if config.model_family == "logistic_regression":
        for column in config.numeric_columns:
            columns.append(np.asarray(standardized_numeric[column]))
            names.append(column)
    else:
        thresholds = (-0.5, 0.0, 0.5)
        for column in config.numeric_columns:
            values = standardized_float[column]
            for threshold in thresholds:
                indicator = (values <= threshold).astype(np.float64)
                columns.append(engine.indicator(indicator))
                names.append(f"stump:{column}<={threshold:g}")

    for column in config.categorical_columns:
        codes = categorical_codes[column]
        for level_index, level in enumerate(levels[column]):
            indicator = (codes == level_index).astype(np.float64)
            columns.append(engine.indicator(indicator))
            names.append(f"{column}={level}")

    bias = np.full(
        next(iter(standardized_numeric.values())).shape[0],
        engine.bias_value(),
        dtype=np.int64 if engine.is_fixed else engine.dtype,
    )
    columns.append(bias)
    names.append("bias")
    matrix = np.column_stack(columns)
    matrix = matrix.astype(np.int64 if engine.is_fixed else engine.dtype, copy=False)
    return matrix, tuple(names)


def prepare_dataset(
    dataset: Dataset,
    split: DatasetSplit,
    config: TrainingConfig,
    compute_engine: PrecisionEngine,
    stats_engine: PrecisionEngine | None = None,
) -> PreparedData:
    """Prepare features with explicit standardization and reduction boundaries."""

    stats_engine = stats_engine or compute_engine
    levels = category_levels(dataset, config)
    standardizer = Standardizer.fit(
        dataset.numeric,
        split.train_indices,
        stats_engine,
        config.standardization,
    )
    standardized_all_target = standardizer.transform(dataset.numeric)

    if stats_engine.name == compute_engine.name and (
        not stats_engine.is_fixed
        or (
            stats_engine.scale == compute_engine.scale
            and stats_engine.rounding_mode == compute_engine.rounding_mode
        )
    ):
        standardized_all = standardized_all_target
    else:
        standardized_all = {
            column: compute_engine.standardized_numeric(
                stats_engine.to_float(values)
            )
            for column, values in standardized_all_target.items()
        }

    categorical_codes = {
        column: np.asarray(
            [levels[column].index(value) for value in dataset.categorical[column]],
            dtype=np.int64,
        )
        for column in config.categorical_columns
    }
    features_all, feature_names = _build_feature_matrix(
        standardized_all,
        categorical_codes,
        levels,
        compute_engine,
        config,
    )
    label_binary = dataset.label_array(config.positive_label)
    labels_all = compute_engine.labels(label_binary)

    train_indices = np.asarray(split.train_indices, dtype=np.int64)
    validation_indices = np.asarray(split.validation_indices, dtype=np.int64)
    train_features = features_all[train_indices, :].copy()
    validation_features = features_all[validation_indices, :].copy()
    train_labels = labels_all[train_indices].copy()
    validation_labels = labels_all[validation_indices].copy()

    chunks = tuple(
        tuple(range(start, min(start + config.chunk_size, split.train_count)))
        for start in range(0, split.train_count, config.chunk_size)
    )
    chunk_global = tuple(
        tuple(split.train_indices[index] for index in chunk) for chunk in chunks
    )
    sample_fingerprint = split.fingerprint(dataset)
    chunk_plan_core = {
        "chunk_size": config.chunk_size,
        "chunks_global_indices": [list(chunk) for chunk in chunk_global],
        "train_sha256": sample_fingerprint["train_sha256"],
        "validation_sha256": sample_fingerprint["validation_sha256"],
        "reduce_order": "chunk_ascending_then_sample_ascending_left_fold",
    }
    chunk_plan_fingerprint = {
        **chunk_plan_core,
        "sha256": sha256_json(chunk_plan_core),
    }
    regularized_mask = np.ones(feature_names.__len__(), dtype=bool)
    regularized_mask[-1] = False
    return PreparedData(
        config=config,
        engine=compute_engine,
        stats_engine=stats_engine,
        standardizer=standardizer,
        feature_names=feature_names,
        train_features=train_features,
        train_labels=train_labels,
        validation_features=validation_features,
        validation_labels=validation_labels,
        train_global_indices=split.train_indices,
        validation_global_indices=split.validation_indices,
        train_row_ids=tuple(dataset.row_ids[index] for index in split.train_indices),
        validation_row_ids=tuple(dataset.row_ids[index] for index in split.validation_indices),
        chunks=chunks,
        chunk_global_indices=chunk_global,
        regularized_mask=regularized_mask,
        category_levels=levels,
        sample_fingerprint=sample_fingerprint,
        chunk_plan_fingerprint=chunk_plan_fingerprint,
    )
