"""Deterministic JSONL tabular data loading and fixed category encoding."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import DataConfig
from .errors import E_DATA, EngineError
from .util import canonical_json_bytes, sha256_value


def category_token(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raise EngineError(
        E_DATA,
        f"categorical value must be a JSON scalar, got {type(value).__name__}",
        stage="data_load",
    )


def row_id_token(value: Any) -> str:
    if isinstance(value, str):
        return "s:" + value
    if isinstance(value, bool):
        return "b:" + ("1" if value else "0")
    if isinstance(value, int):
        return "i:" + str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise EngineError(E_DATA, "row_id must be finite", stage="data_load")
        return "f:" + repr(value)
    raise EngineError(
        E_DATA,
        f"row_id must be a JSON scalar, got {type(value).__name__}",
        stage="data_load",
    )


def _labels_equal(left: Any, right: Any) -> bool:
    numeric_left = isinstance(left, (int, float)) and not isinstance(left, bool)
    numeric_right = isinstance(right, (int, float)) and not isinstance(right, bool)
    if numeric_left and numeric_right:
        return float(left) == float(right)
    return type(left) is type(right) and left == right


@dataclass(frozen=True)
class Dataset:
    row_ids: tuple[Any, ...]
    row_id_tokens: tuple[str, ...]
    numeric_values: dict[str, np.ndarray]
    categorical_values: dict[str, tuple[str, ...]]
    category_categories: dict[str, tuple[str, ...]]
    targets: np.ndarray
    feature_names: tuple[str, ...]
    fingerprint: str

    @property
    def n_samples(self) -> int:
        return len(self.row_ids)

    @property
    def n_features(self) -> int:
        return len(self.feature_names)


def load_dataset(config: DataConfig) -> Dataset:
    try:
        raw_bytes = config.path.read_bytes()
    except OSError as exc:
        raise EngineError(
            E_DATA,
            f"cannot read data file {config.path}: {exc}",
            stage="data_load",
            details={"path": str(config.path)},
        ) from exc
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EngineError(E_DATA, f"data file is not UTF-8: {exc}", stage="data_load") from exc

    rows: list[dict[str, Any]] = []
    line_numbers: list[int] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EngineError(
                E_DATA,
                f"invalid JSONL at line {line_number}: {exc}",
                stage="data_load",
                details={"line": line_number},
            ) from exc
        if not isinstance(row, dict):
            raise EngineError(
                E_DATA,
                f"JSONL row at line {line_number} is not an object",
                stage="data_load",
                details={"line": line_number},
            )
        rows.append(row)
        line_numbers.append(line_number)

    if not rows:
        raise EngineError(E_DATA, "data file contains no rows", stage="data_load")

    required = {
        config.row_id_column,
        config.label_column,
        *config.numeric_columns,
        *config.categorical_columns,
    }
    for row, line_number in zip(rows, line_numbers):
        missing = sorted(required - set(row))
        if missing:
            raise EngineError(
                E_DATA,
                f"row at line {line_number} is missing fields: {missing}",
                stage="data_load",
                details={"line": line_number, "missing_fields": missing},
            )

    tokens = [row_id_token(row[config.row_id_column]) for row in rows]
    if len(set(tokens)) != len(tokens):
        raise EngineError(E_DATA, "row_id values must be unique", stage="data_load")
    order = sorted(range(len(rows)), key=lambda index: (tokens[index], line_numbers[index]))
    rows = [rows[index] for index in order]
    tokens = [tokens[index] for index in order]
    line_numbers = [line_numbers[index] for index in order]

    n_samples = len(rows)
    positive_count = 0
    negative_count = 0
    targets = np.empty(n_samples, dtype=np.float64)
    category_sets: dict[str, set[str]] = {column: set() for column in config.categorical_columns}
    category_tokens_by_column: dict[str, list[str]] = {
        column: [] for column in config.categorical_columns
    }
    numeric_arrays: dict[str, np.ndarray] = {
        column: np.empty(n_samples, dtype=np.float64) for column in config.numeric_columns
    }

    for output_index, (row, line_number) in enumerate(zip(rows, line_numbers)):
        label = row[config.label_column]
        if _labels_equal(label, config.positive_label):
            targets[output_index] = 1.0
            positive_count += 1
        elif _labels_equal(label, config.negative_label):
            targets[output_index] = -1.0
            negative_count += 1
        else:
            raise EngineError(
                E_DATA,
                f"label at line {line_number} is not positive_label or negative_label",
                stage="data_load",
                details={
                    "line": line_number,
                    "label": label,
                    "positive_label": config.positive_label,
                    "negative_label": config.negative_label,
                },
            )
        for column in config.numeric_columns:
            value = row[column]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EngineError(
                    E_DATA,
                    f"numeric column {column!r} at line {line_number} is not numeric",
                    stage="data_load",
                    details={"line": line_number, "column": column},
                )
            number = float(value)
            if not math.isfinite(number):
                raise EngineError(
                    E_DATA,
                    f"numeric column {column!r} at line {line_number} is not finite",
                    stage="data_load",
                    details={"line": line_number, "column": column},
                )
            numeric_arrays[column][output_index] = number
        for column in config.categorical_columns:
            token = category_token(row[column])
            category_tokens_by_column[column].append(token)
            category_sets[column].add(token)

    if positive_count == 0 or negative_count == 0:
        raise EngineError(
            E_DATA,
            "binary training data must contain both positive and negative labels",
            stage="data_load",
            details={"positive_count": positive_count, "negative_count": negative_count},
        )

    categories = {
        column: tuple(sorted(category_sets[column], key=lambda item: item.encode("utf-8")))
        for column in config.categorical_columns
    }
    categorical_values = {
        column: tuple(category_tokens_by_column[column])
        for column in config.categorical_columns
    }
    feature_names: list[str] = [f"num:{column}" for column in config.numeric_columns]
    for column in config.categorical_columns:
        for category in categories[column]:
            feature_names.append(f"cat:{column}={category}")
    if len(set(feature_names)) != len(feature_names):
        raise EngineError(E_DATA, "encoded feature names are not unique", stage="data_load")

    fingerprint_payload = {
        "row_id_column": config.row_id_column,
        "label_column": config.label_column,
        "numeric_columns": list(config.numeric_columns),
        "categorical_columns": list(config.categorical_columns),
        "positive_label": config.positive_label,
        "negative_label": config.negative_label,
        "category_encoding": config.category_encoding,
        "rows": [
            canonical_json_bytes(
                {
                    "row_id": row[config.row_id_column],
                    "label": row[config.label_column],
                    "numeric": [row[column] for column in config.numeric_columns],
                    "categorical": [row[column] for column in config.categorical_columns],
                }
            ).decode("utf-8")
            for row in rows
        ],
    }
    fingerprint = sha256_value(fingerprint_payload)
    return Dataset(
        row_ids=tuple(row[config.row_id_column] for row in rows),
        row_id_tokens=tuple(tokens),
        numeric_values=numeric_arrays,
        categorical_values=categorical_values,
        category_categories=categories,
        targets=targets,
        feature_names=tuple(feature_names),
        fingerprint=fingerprint,
    )

