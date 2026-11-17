from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import LoadedConfig
from .errors import E_DATA_INVALID, WorkbenchError
from .utils import sha256_bytes, stable_seed


@dataclass
class Dataset:
    feature_names: tuple[str, ...]
    feature_kinds: tuple[str, ...]
    target_name: str
    X: np.ndarray
    y: np.ndarray
    category_maps: dict[str, dict[str, int]]
    fill_values: dict[str, Any]
    missing_counts: dict[str, int]
    data_fingerprint: str
    seed: int

    @property
    def rows(self) -> int:
        return int(self.X.shape[0])

    @property
    def feature_count(self) -> int:
        return int(self.X.shape[1])

    def public_summary(self) -> dict[str, Any]:
        return {
            "fingerprint": self.data_fingerprint,
            "rows": self.rows,
            "target_name": self.target_name,
            "feature_names": list(self.feature_names),
            "feature_kinds": list(self.feature_kinds),
            "category_maps": self.category_maps,
            "fill_values": self.fill_values,
            "missing_counts": self.missing_counts,
            "positive_rate": float(np.mean(self.y)),
        }


def _rng(seed: int, *parts: object) -> np.random.Generator:
    return np.random.Generator(np.random.PCG64(stable_seed(seed, *parts)))


def _missing_mask(rng: np.random.Generator, rows: int, rate: float) -> np.ndarray:
    count = int(round(float(rate) * rows))
    mask = np.zeros(rows, dtype=bool)
    if count > 0:
        mask[rng.choice(rows, size=min(count, rows), replace=False)] = True
    return mask


def _median(values: np.ndarray) -> float:
    finite = np.sort(values[np.isfinite(values)].astype(np.float64))
    if len(finite) == 0:
        return 0.0
    midpoint = len(finite) // 2
    if len(finite) % 2 == 1:
        return float(finite[midpoint])
    return float((finite[midpoint - 1] + finite[midpoint]) / 2.0)


def _most_common_category(values: list[str | None], categories: list[str]) -> str:
    counts = Counter(value for value in values if value is not None)
    if not counts:
        return sorted(categories)[0]
    return min(sorted(categories), key=lambda item: (-counts.get(item, 0), item))


def _sigmoid(value: float) -> float:
    if value >= 0.0:
        return 1.0 / (1.0 + math.exp(-value))
    exp_value = math.exp(value)
    return exp_value / (1.0 + exp_value)


def generate_dataset(loaded: LoadedConfig) -> Dataset:
    spec = loaded.data_spec
    config = loaded.config
    rows = int(spec["rows"])
    seed = config.seed
    columns: list[dict[str, Any]] = spec["columns"]

    raw_values: dict[str, np.ndarray | list[str | None]] = {}
    raw_missing: dict[str, np.ndarray] = {}
    pending = list(columns)
    attempts = 0
    while pending and attempts <= len(columns) + 1:
        attempts += 1
        next_pending: list[dict[str, Any]] = []
        for column in pending:
            name = column["name"]
            kind = column["kind"]
            missing_rate = float(column.get("missing_rate", 0.0))
            if kind == "numeric":
                generator = _rng(seed, "column", name, "value")
                mean = float(column["mean"])
                std = float(column["std"])
                values = generator.normal(mean, std, size=rows).astype(np.float64)
                minimum = column.get("min")
                maximum = column.get("max")
                if minimum is not None or maximum is not None:
                    lower = -np.inf if minimum is None else float(minimum)
                    upper = np.inf if maximum is None else float(maximum)
                    values = np.clip(values, lower, upper)
                missing_rng = _rng(seed, "column", name, "missing")
                missing = _missing_mask(missing_rng, rows, missing_rate)
                values = values.copy()
                values[missing] = np.nan
                raw_values[name] = values
                raw_missing[name] = missing
            elif kind == "categorical":
                generator = _rng(seed, "column", name, "value")
                categories = list(column["categories"])
                weights = column.get("weights")
                probabilities = None if weights is None else np.asarray(weights, dtype=np.float64)
                if probabilities is not None:
                    probabilities = probabilities / np.sum(probabilities)
                sampled = generator.choice(categories, size=rows, p=probabilities)
                values = [str(item) for item in sampled.tolist()]
                missing_rng = _rng(seed, "column", name, "missing")
                missing = _missing_mask(missing_rng, rows, missing_rate)
                for index in np.flatnonzero(missing):
                    values[int(index)] = None
                raw_values[name] = values
                raw_missing[name] = missing
            elif kind == "redundant":
                source = str(column["source"])
                if source not in raw_values:
                    next_pending.append(column)
                    continue
                source_values = raw_values[source]
                if isinstance(source_values, np.ndarray):
                    generator = _rng(seed, "column", name, "redundant_noise")
                    noise = generator.normal(
                        0.0,
                        float(column.get("noise_std", 0.0)),
                        size=rows,
                    ).astype(np.float64)
                    values = np.asarray(source_values, dtype=np.float64).copy() + noise
                else:
                    values = list(source_values)
                missing_rng = _rng(seed, "column", name, "missing")
                own_missing = _missing_mask(missing_rng, rows, missing_rate)
                inherited_missing = raw_missing[source].copy()
                missing = own_missing | inherited_missing
                if isinstance(values, np.ndarray):
                    values[missing] = np.nan
                else:
                    values = list(values)
                    for index in np.flatnonzero(missing):
                        values[int(index)] = None
                raw_values[name] = values
                raw_missing[name] = missing
        pending = next_pending
    if pending:
        names = [column["name"] for column in pending]
        raise WorkbenchError(
            E_DATA_INVALID,
            "data_generation",
            "redundant column dependency cycle detected",
            details={"columns": names},
        )

    feature_names = tuple(column["name"] for column in columns)
    feature_kinds = tuple(column["kind"] for column in columns)
    matrix = np.empty((rows, len(columns)), dtype=np.float64)
    category_maps: dict[str, dict[str, int]] = {}
    fill_values: dict[str, Any] = {}
    missing_counts: dict[str, int] = {}

    for feature_index, column in enumerate(columns):
        name = column["name"]
        kind = column["kind"]
        values = raw_values[name]
        missing_counts[name] = int(np.sum(raw_missing[name]))
        if isinstance(values, np.ndarray):
            fill_value = _median(values)
            fill_values[name] = fill_value
            filled = values.copy()
            filled[~np.isfinite(filled)] = fill_value
            matrix[:, feature_index] = filled.astype(np.float64)
        else:
            categories = sorted(set(column["categories"]))
            mapping = {category: index for index, category in enumerate(categories)}
            category_maps[name] = mapping
            fill_category = _most_common_category(values, categories)
            fill_values[name] = fill_category
            encoded = np.empty(rows, dtype=np.float64)
            for row_index, value in enumerate(values):
                encoded[row_index] = float(mapping[fill_category if value is None else value])
            matrix[:, feature_index] = encoded

    target = spec["target"]
    target_name = str(target["name"])
    positive_rate = float(target.get("positive_rate", 0.5))
    intercept_value = target.get("intercept")
    if intercept_value is None:
        intercept = math.log(positive_rate / (1.0 - positive_rate))
    else:
        intercept = float(intercept_value)
    linear = np.full(rows, intercept, dtype=np.float64)

    for feature_index, column in enumerate(columns):
        name = column["name"]
        kind = column["kind"]
        effect = float(column.get("effect", 0.0))
        if kind == "numeric":
            std = float(column["std"])
            mean = float(column["mean"])
            if std > 0.0 and effect != 0.0:
                linear += effect * ((matrix[:, feature_index] - mean) / std)
        elif kind == "categorical":
            effects = column.get("effects", {})
            if effects:
                inverse_map = {code: category for category, code in category_maps[name].items()}
                for row_index in range(rows):
                    category = inverse_map[int(matrix[row_index, feature_index])]
                    linear[row_index] += float(effects.get(category, 0.0))
        elif kind == "redundant" and effect != 0.0:
            source = str(column["source"])
            source_column = next(item for item in columns if item["name"] == source)
            if source_column["kind"] == "numeric" and float(source_column["std"]) > 0.0:
                linear += effect * (
                    (matrix[:, feature_index] - float(source_column["mean"]))
                    / float(source_column["std"])
                )

    target_rng = _rng(seed, "target", target_name)
    noise_std = float(target.get("noise_std", 0.0))
    if noise_std > 0.0:
        linear = linear + target_rng.normal(0.0, noise_std, size=rows)
    probabilities = np.asarray([_sigmoid(float(value)) for value in linear], dtype=np.float64)
    target_values = (target_rng.random(rows) < probabilities).astype(np.uint8)

    schema_json = repr(
        (
            feature_names,
            feature_kinds,
            target_name,
            {name: sorted(mapping.items()) for name, mapping in category_maps.items()},
        )
    ).encode("utf-8")
    fingerprint = sha256_bytes(
        schema_json,
        np.ascontiguousarray(matrix.astype(">f8")).tobytes(order="C"),
        np.ascontiguousarray(target_values.astype("u1")).tobytes(order="C"),
    )
    return Dataset(
        feature_names=feature_names,
        feature_kinds=feature_kinds,
        target_name=target_name,
        X=np.ascontiguousarray(matrix, dtype=np.float64),
        y=np.ascontiguousarray(target_values, dtype=np.uint8),
        category_maps=category_maps,
        fill_values=fill_values,
        missing_counts=missing_counts,
        data_fingerprint=fingerprint,
        seed=seed,
    )

