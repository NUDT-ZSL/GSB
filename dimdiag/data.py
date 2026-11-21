"""Deterministic local sample generation and CSV loading."""

from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from .config import validate_config
from .errors import E_INPUT_INVALID, BusinessError
from .serialization import atomic_write_bytes, pretty_json_bytes, sha256_bytes, sha256_file


@dataclass
class Dataset:
    row_ids: list[str]
    numeric: np.ndarray
    categorical: np.ndarray
    labels: np.ndarray
    numeric_columns: list[str]
    categorical_columns: list[str]
    category_levels: dict[str, list[str]]
    checksum: str
    source: str

    @property
    def n_rows(self) -> int:
        return int(self.numeric.shape[0])

    @property
    def n_features(self) -> int:
        return int(self.numeric.shape[1]) + sum(
            len(self.category_levels[name]) for name in self.categorical_columns
        )


def _clean_float(value: float) -> float:
    if value == 0.0:
        return 0.0
    return float(value)


def _format_float(value: float) -> str:
    return format(_clean_float(value), ".17g")


def generate_table_arrays(
    *,
    seed: int,
    n_rows: int,
    n_numeric: int,
    n_categorical: int,
    n_classes: int,
    class_sep: float = 1.15,
) -> tuple[list[str], list[str], np.ndarray, np.ndarray, np.ndarray]:
    rng = np.random.Generator(np.random.PCG64(int(seed)))
    label_indices = rng.integers(0, n_classes, size=n_rows, dtype=np.int64)
    centers = rng.standard_normal((n_classes, n_numeric)) * float(class_sep)
    base = rng.standard_normal((n_rows, n_numeric))
    scales = np.power(10.0, np.linspace(-0.55, 0.55, n_numeric))
    numeric = centers[label_indices] + base * scales

    numeric_columns = [f"x_{index:02d}" for index in range(n_numeric)]
    categorical_columns = [f"cat_{index:02d}" for index in range(n_categorical)]
    categorical = np.empty((n_rows, n_categorical), dtype="<U24")
    levels: dict[str, list[str]] = {}
    for column_index in range(n_categorical):
        level_count = 3 + (column_index % 3)
        weights = np.linspace(0.75, 1.25, level_count)
        weights = weights / weights.sum()
        values = rng.choice(level_count, size=n_rows, p=weights).astype(np.int64)
        column_name = categorical_columns[column_index]
        level_names = [f"c{column_index}_{level}" for level in range(level_count)]
        levels[column_name] = level_names
        categorical[:, column_index] = np.asarray(level_names, dtype="<U24")[values]

    labels = np.asarray([f"class_{index}" for index in label_indices], dtype="<U24")
    row_ids = [f"r{index:06d}" for index in range(n_rows)]
    return row_ids, numeric_columns + categorical_columns, numeric, categorical, labels


def build_table_csv(
    *,
    row_ids: list[str],
    numeric_columns: list[str],
    categorical_columns: list[str],
    numeric: np.ndarray,
    categorical: np.ndarray,
    labels: np.ndarray,
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["row_id", *numeric_columns, *categorical_columns, "label"])
    for row_index, row_id in enumerate(row_ids):
        row: list[str] = [row_id]
        row.extend(_format_float(value) for value in numeric[row_index])
        row.extend(str(value) for value in categorical[row_index])
        row.append(str(labels[row_index]))
        writer.writerow(row)
    return buffer.getvalue().encode("utf-8")


def _dataset_from_arrays(
    *,
    row_ids: list[str],
    numeric_columns: list[str],
    categorical_columns: list[str],
    numeric: np.ndarray,
    categorical: np.ndarray,
    labels: np.ndarray,
    source: str,
) -> Dataset:
    category_levels = {
        name: sorted({str(value) for value in categorical[:, index]})
        for index, name in enumerate(categorical_columns)
    }
    payload = build_table_csv(
        row_ids=row_ids,
        numeric_columns=numeric_columns,
        categorical_columns=categorical_columns,
        numeric=numeric,
        categorical=categorical,
        labels=labels,
    )
    return Dataset(
        row_ids=list(row_ids),
        numeric=np.asarray(numeric, dtype=np.float64),
        categorical=np.asarray(categorical, dtype="<U24"),
        labels=np.asarray(labels, dtype="<U24"),
        numeric_columns=list(numeric_columns),
        categorical_columns=list(categorical_columns),
        category_levels=category_levels,
        checksum=sha256_bytes(payload),
        source=source,
    )


def _generated_dataset(dataset_config: Mapping[str, Any]) -> Dataset:
    row_ids, all_columns, numeric, categorical, labels = generate_table_arrays(
        seed=int(dataset_config["seed"]),
        n_rows=int(dataset_config["n_rows"]),
        n_numeric=int(dataset_config["n_numeric"]),
        n_categorical=int(dataset_config["n_categorical"]),
        n_classes=int(dataset_config["n_classes"]),
        class_sep=float(dataset_config["class_sep"]),
    )
    numeric_columns = all_columns[: int(dataset_config["n_numeric"])]
    categorical_columns = all_columns[int(dataset_config["n_numeric"]) :]
    return _dataset_from_arrays(
        row_ids=row_ids,
        numeric_columns=numeric_columns,
        categorical_columns=categorical_columns,
        numeric=numeric,
        categorical=categorical,
        labels=labels,
        source="generated_table",
    )


def load_dataset(config: Mapping[str, Any], base_dir: str | Path) -> Dataset:
    dataset_config = config["dataset"]
    if dataset_config["kind"] == "generated_table":
        return _generated_dataset(dataset_config)

    path = Path(dataset_config["path"])
    if not path.is_absolute():
        path = Path(base_dir) / path
    if not path.exists():
        raise BusinessError(
            E_INPUT_INVALID,
            "dataset file does not exist",
            {"field": "dataset.path", "path": str(path)},
        )
    raw_bytes = path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BusinessError(
            E_INPUT_INVALID,
            "dataset must be UTF-8 encoded",
            {"field": "dataset.path", "path": str(path), "reason": str(exc)},
        ) from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise BusinessError(
            E_INPUT_INVALID,
            "dataset has no header",
            {"field": "dataset.path", "path": str(path)},
        )
    headers = list(reader.fieldnames)
    row_id_column = dataset_config["row_id_column"]
    label_column = dataset_config["label_column"]
    numeric_columns = list(dataset_config["numeric_columns"])
    categorical_columns = list(dataset_config["categorical_columns"])
    required = [row_id_column, label_column, *numeric_columns, *categorical_columns]
    missing = [name for name in required if name not in headers]
    if missing:
        raise BusinessError(
            E_INPUT_INVALID,
            "dataset is missing configured columns",
            {"field": "dataset", "missing": missing},
        )
    overlaps = (
        set(numeric_columns) & set(categorical_columns)
        | {row_id_column, label_column} & set(numeric_columns)
        | {row_id_column, label_column} & set(categorical_columns)
    )
    if overlaps:
        raise BusinessError(
            E_INPUT_INVALID,
            "column roles overlap",
            {"field": "dataset", "columns": sorted(overlaps)},
        )

    row_ids: list[str] = []
    numeric_rows: list[list[float]] = []
    categorical_rows: list[list[str]] = []
    labels: list[str] = []
    seen_row_ids: set[str] = set()
    for line_number, row in enumerate(reader, start=2):
        row_id = row.get(row_id_column, "")
        if not row_id:
            raise BusinessError(
                E_INPUT_INVALID,
                "row_id must not be empty",
                {"field": f"dataset.{row_id_column}", "line": line_number},
            )
        if row_id in seen_row_ids:
            raise BusinessError(
                E_INPUT_INVALID,
                "row_id must be unique",
                {"field": f"dataset.{row_id_column}", "line": line_number, "row_id": row_id},
            )
        seen_row_ids.add(row_id)
        values: list[float] = []
        for column in numeric_columns:
            try:
                value = float(row[column])
            except (TypeError, ValueError) as exc:
                raise BusinessError(
                    E_INPUT_INVALID,
                    "numeric column contains a non-numeric value",
                    {
                        "field": f"dataset.{column}",
                        "line": line_number,
                        "value": row.get(column),
                    },
                ) from exc
            if not math.isfinite(value):
                raise BusinessError(
                    E_INPUT_INVALID,
                    "numeric column must contain finite values",
                    {"field": f"dataset.{column}", "line": line_number, "value": value},
                )
            values.append(value)
        row_ids.append(row_id)
        numeric_rows.append(values)
        categorical_rows.append([row[column] for column in categorical_columns])
        label = row.get(label_column, "")
        if not label:
            raise BusinessError(
                E_INPUT_INVALID,
                "label must not be empty",
                {"field": f"dataset.{label_column}", "line": line_number},
            )
        labels.append(label)

    if not row_ids:
        raise BusinessError(
            E_INPUT_INVALID,
            "dataset has no data rows",
            {"field": "dataset.path", "path": str(path)},
        )
    numeric = np.asarray(numeric_rows, dtype=np.float64)
    categorical = np.asarray(categorical_rows, dtype="<U24")
    return _dataset_from_arrays(
        row_ids=row_ids,
        numeric_columns=numeric_columns,
        categorical_columns=categorical_columns,
        numeric=numeric,
        categorical=categorical,
        labels=np.asarray(labels, dtype="<U24"),
        source=str(path),
    )


def demo_config() -> dict[str, Any]:
    raw = {
        "schema_version": 1,
        "dataset": {
            "kind": "csv",
            "path": "../data/demo.csv",
            "row_id_column": "row_id",
            "label_column": "label",
            "numeric_columns": [f"x_{index:02d}" for index in range(12)],
            "categorical_columns": ["cat_00", "cat_01"],
        },
        "method": "pca",
        "target_dim": 4,
        "standardize": True,
        "random_projection": {"seed": 1701, "density": 0.45},
        "two_stage": {"intermediate_dim": 8, "seed": 1901, "density": 0.55},
        "precision": {
            "mode": "float64",
            "fixed_scale": 16384,
            "rounding": "half_even",
        },
        "evaluation": {
            "k_neighbors": 7,
            "class_consistency_threshold": 0.72,
            "variance_explained_threshold": 0.74,
            "threshold_strategy": "tolerance_band",
            "threshold_tolerance": 0.003,
            "subnormal_floor": 1e-300,
            "boundary_decision": "accept",
        },
        "error_tolerance": {
            "standardization_max_abs": 5e-3,
            "standardization_max_rel": 5e-3,
            "model_construction_max_abs": 5e-3,
            "model_construction_max_rel": 5e-3,
            "coordinates_max_abs": 5e-3,
            "coordinates_max_rel": 5e-3,
            "two_stage_max_abs": 5e-3,
            "two_stage_max_rel": 5e-3,
        },
        "resources": {
            "max_features": 512,
            "max_dim_matrix_bytes": 64 * 1024 * 1024,
            "max_projection_buffer_bytes": 64 * 1024 * 1024,
            "overflow_policy": "error",
        },
        "diagnostics": {"top_error_elements": 8},
    }
    return validate_config(raw)


def write_demo_sample(workspace_root: str | Path) -> tuple[Path, Path]:
    root = Path(workspace_root)
    dataset_config = {
        "seed": 20261122,
        "n_rows": 240,
        "n_numeric": 12,
        "n_categorical": 2,
        "n_classes": 4,
        "class_sep": 1.15,
    }
    row_ids, all_columns, numeric, categorical, labels = generate_table_arrays(
        **dataset_config
    )
    numeric_columns = all_columns[: dataset_config["n_numeric"]]
    categorical_columns = all_columns[dataset_config["n_numeric"] :]
    csv_bytes = build_table_csv(
        row_ids=row_ids,
        numeric_columns=numeric_columns,
        categorical_columns=categorical_columns,
        numeric=numeric,
        categorical=categorical,
        labels=labels,
    )
    data_path = root / "data" / "demo.csv"
    config_path = root / "configs" / "demo.json"
    atomic_write_bytes(data_path, csv_bytes)

    config = demo_config()
    config["dataset"]["numeric_columns"] = numeric_columns
    config["dataset"]["categorical_columns"] = categorical_columns
    atomic_write_bytes(config_path, pretty_json_bytes(config))
    metadata = {
        "generator": "dimdiag.data.generate_table_arrays",
        "generator_version": 1,
        "seed": dataset_config["seed"],
        "n_rows": dataset_config["n_rows"],
        "n_numeric": dataset_config["n_numeric"],
        "n_categorical": dataset_config["n_categorical"],
        "n_classes": dataset_config["n_classes"],
        "class_sep": dataset_config["class_sep"],
        "csv_checksum": sha256_file(data_path),
    }
    atomic_write_bytes(
        data_path.with_suffix(".meta.json"),
        pretty_json_bytes(metadata),
    )
    return data_path, config_path
