"""Deterministic JSONL sample generation, loading, validation and splitting."""

from __future__ import annotations

import json
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from .config import TrainingConfig
from .errors import WorkbenchError
from .serialization import canonical_dumps, sha256_bytes, sha256_file, sha256_json


@dataclass(frozen=True)
class Dataset:
    records: tuple[dict[str, Any], ...]
    row_ids: tuple[str, ...]
    numeric: dict[str, np.ndarray]
    categorical: dict[str, tuple[str, ...]]
    labels: tuple[str, ...]
    path: str
    input_sha256: str

    @property
    def row_count(self) -> int:
        return len(self.records)

    def label_array(self, positive_label: str) -> np.ndarray:
        return np.asarray([1.0 if value == positive_label else 0.0 for value in self.labels])


@dataclass(frozen=True)
class DatasetSplit:
    train_indices: tuple[int, ...]
    validation_indices: tuple[int, ...]
    label_values: tuple[str, ...]
    positive_label: str

    @property
    def train_count(self) -> int:
        return len(self.train_indices)

    @property
    def validation_count(self) -> int:
        return len(self.validation_indices)

    def fingerprint(self, dataset: Dataset) -> dict[str, Any]:
        train_ids = [dataset.row_ids[index] for index in self.train_indices]
        validation_ids = [dataset.row_ids[index] for index in self.validation_indices]
        return {
            "train_indices": list(self.train_indices),
            "validation_indices": list(self.validation_indices),
            "train_row_ids": train_ids,
            "validation_row_ids": validation_ids,
            "train_sha256": sha256_json(train_ids),
            "validation_sha256": sha256_json(validation_ids),
        }


def _input_error(message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError("E_INPUT_INVALID", message, details)


def load_dataset(path: str | Path, config: TrainingConfig) -> Dataset:
    sample_path = Path(path)
    if not sample_path.is_file():
        raise _input_error("sample file does not exist", path=str(sample_path))
    records: list[dict[str, Any]] = []
    row_ids: list[str] = []
    seen: set[str] = set()
    numeric_values: dict[str, list[float]] = {name: [] for name in config.numeric_columns}
    categorical_values: dict[str, list[str]] = {
        name: [] for name in config.categorical_columns
    }
    labels: list[str] = []

    with sample_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise _input_error(
                    "invalid JSONL record",
                    line=line_number,
                    reason=str(exc),
                ) from exc
            if not isinstance(record, dict):
                raise _input_error("JSONL record must be an object", line=line_number)
            row_id = record.get("row_id")
            if not isinstance(row_id, str) or not row_id:
                raise _input_error(
                    "row_id must be a non-empty string",
                    line=line_number,
                    field="row_id",
                )
            if row_id in seen:
                raise WorkbenchError(
                    "E_DUPLICATE_ROW_ID",
                    "duplicate row_id in input",
                    {"row_id": row_id, "line": line_number},
                )
            seen.add(row_id)
            row_ids.append(row_id)
            for column in config.numeric_columns:
                if column not in record:
                    raise _input_error(
                        "missing numeric column",
                        line=line_number,
                        row_id=row_id,
                        field=column,
                    )
                value = record[column]
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise _input_error(
                        "numeric column must contain a JSON number",
                        line=line_number,
                        row_id=row_id,
                        field=column,
                    )
                numeric = float(value)
                if not math.isfinite(numeric):
                    raise _input_error(
                        "numeric column must be finite",
                        line=line_number,
                        row_id=row_id,
                        field=column,
                    )
                numeric_values[column].append(numeric)
            for column in config.categorical_columns:
                if column not in record:
                    raise _input_error(
                        "missing categorical column",
                        line=line_number,
                        row_id=row_id,
                        field=column,
                    )
                value = record[column]
                if not isinstance(value, str):
                    raise _input_error(
                        "categorical column must contain a string",
                        line=line_number,
                        row_id=row_id,
                        field=column,
                    )
                categorical_values[column].append(value)
            if config.label_column not in record:
                raise _input_error(
                    "missing label column",
                    line=line_number,
                    row_id=row_id,
                    field=config.label_column,
                )
            label = record[config.label_column]
            if not isinstance(label, str):
                raise _input_error(
                    "label must be a string",
                    line=line_number,
                    row_id=row_id,
                    field=config.label_column,
                )
            labels.append(label)
            records.append(record)

    if not records:
        raise _input_error("sample file contains no records", path=str(sample_path))
    label_values = tuple(sorted(set(labels)))
    if len(label_values) != 2:
        raise _input_error(
            "binary classification requires exactly two label values",
            labels=list(label_values),
        )
    if config.positive_label not in label_values:
        raise _input_error(
            "positive_label does not occur in the sample",
            field="positive_label",
            value=config.positive_label,
            labels=list(label_values),
        )

    return Dataset(
        records=tuple(records),
        row_ids=tuple(row_ids),
        numeric={name: np.asarray(values, dtype=np.float64) for name, values in numeric_values.items()},
        categorical={name: tuple(values) for name, values in categorical_values.items()},
        labels=tuple(labels),
        path=str(sample_path),
        input_sha256=sha256_file(sample_path),
    )


def make_stratified_split(dataset: Dataset, config: TrainingConfig) -> DatasetSplit:
    label_values = tuple(sorted(set(dataset.labels)))
    positives = [index for index, value in enumerate(dataset.labels) if value == config.positive_label]
    negatives = [index for index, value in enumerate(dataset.labels) if value != config.positive_label]
    if len(positives) < 2 or len(negatives) < 2:
        raise _input_error(
            "each class needs at least two rows for a stratified train/validation split",
            positive_count=len(positives),
            negative_count=len(negatives),
        )

    validation: set[int] = set()
    for group in (positives, negatives):
        count = max(1, int(round(len(group) * config.validation_fraction)))
        count = min(count, len(group) - 1)
        validation.update(group[-count:])

    train = tuple(index for index in range(dataset.row_count) if index not in validation)
    validation_indices = tuple(sorted(validation))
    if not train or not validation_indices:
        raise _input_error("split produced an empty train or validation partition")
    return DatasetSplit(
        train_indices=train,
        validation_indices=validation_indices,
        label_values=label_values,
        positive_label=config.positive_label,
    )


def category_levels(dataset: Dataset, config: TrainingConfig) -> dict[str, tuple[str, ...]]:
    return {
        column: tuple(sorted(set(dataset.categorical[column])))
        for column in config.categorical_columns
    }


def write_config(path: str | Path, config: TrainingConfig) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generate_deterministic_samples(
    output_path: str | Path,
    *,
    row_count: int = 240,
    seed: int = 20261120,
    numeric_columns: Sequence[str] = ("num_0", "num_1", "num_2"),
    categorical_columns: Sequence[str] = ("cat_0", "cat_1"),
    label_column: str = "label",
    positive_label: str = "yes",
) -> dict[str, Any]:
    """Generate local tabular samples with a fixed PCG64 stream and stable JSONL order."""

    if row_count < 20:
        raise ValueError("row_count must be at least 20")
    if len(numeric_columns) < 1:
        raise ValueError("at least one numeric column is required")
    rng = np.random.Generator(np.random.PCG64(seed))

    numeric_generated: dict[str, np.ndarray] = {}
    base = rng.normal(0.0, 1.0, size=row_count)
    for index, column in enumerate(numeric_columns):
        if index == 0:
            values = base
        elif index == 1:
            values = 4.5 + 1.7 * rng.normal(size=row_count) + 0.35 * base
        else:
            values = (
                0.45 * base
                - 0.27 * (rng.normal(size=row_count))
                + 0.08 * rng.normal(size=row_count)
            )
        numeric_generated[column] = np.round(values, 12)

    categorical_generated: dict[str, np.ndarray] = {}
    category_pools = (("alpha", "beta", "gamma"), ("north", "south", "west"))
    for index, column in enumerate(categorical_columns):
        pool = category_pools[index % len(category_pools)]
        codes = rng.integers(0, len(pool), size=row_count, endpoint=False)
        categorical_generated[column] = np.asarray([pool[int(code)] for code in codes], dtype=object)

    logits = 0.95 * numeric_generated[numeric_columns[0]]
    if len(numeric_columns) > 1:
        logits = logits - 0.34 * (
            numeric_generated[numeric_columns[1]]
            - float(np.mean(numeric_generated[numeric_columns[1]]))
        )
    if len(numeric_columns) > 2:
        logits = logits + 0.55 * numeric_generated[numeric_columns[2]]
    if categorical_columns:
        first_codes = categorical_generated[categorical_columns[0]]
        logits = logits + np.where(first_codes == "beta", 0.45, 0.0)
        logits = logits + np.where(first_codes == "gamma", -0.35, 0.0)
    if len(categorical_columns) > 1:
        second_codes = categorical_generated[categorical_columns[1]]
        logits = logits + np.where(second_codes == "north", 0.25, -0.1)
    logits = logits - float(np.mean(logits))
    probabilities = 1.0 / (1.0 + np.exp(-logits))
    labels_binary = rng.random(row_count) < probabilities
    if int(np.sum(labels_binary)) < 12 or int(np.sum(~labels_binary)) < 12:
        order = np.argsort(logits)
        labels_binary = np.zeros(row_count, dtype=bool)
        labels_binary[order[row_count // 3 :]] = True

    permutation = rng.permutation(row_count)
    records: list[dict[str, Any]] = []
    for position, source_index in enumerate(permutation):
        record: dict[str, Any] = {"row_id": f"r{position + 1:05d}"}
        for column in numeric_columns:
            record[column] = float(numeric_generated[column][int(source_index)])
        for column in categorical_columns:
            record[column] = str(categorical_generated[column][int(source_index)])
        negative_label = "no" if positive_label != "no" else "negative"
        record[label_column] = (
            positive_label if bool(labels_binary[int(source_index)]) else negative_label
        )
        records.append(record)

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = "".join(canonical_dumps(record) + "\n" for record in records)
    target.write_text(payload, encoding="utf-8")
    digest = sha256_bytes(payload.encode("utf-8"))
    manifest = {
        "schema": "numeric-workbench/sample-manifest/v1",
        "path": str(target),
        "row_count": row_count,
        "seed": seed,
        "numeric_columns": list(numeric_columns),
        "categorical_columns": list(categorical_columns),
        "label_column": label_column,
        "positive_label": positive_label,
        "sha256": digest,
        "class_counts": {
            positive_label: int(np.sum(labels_binary)),
            ("no" if positive_label != "no" else "negative"): int(np.sum(~labels_binary)),
        },
    }
    manifest_path = target.with_suffix(target.suffix + ".manifest.json")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def clean_generated_state(root: str | Path, *, include_samples: bool = False) -> list[str]:
    """Remove only workbench-owned generated directories or files."""

    base = Path(root)
    names = ["snapshots", "cache", "runs"]
    if include_samples:
        names.append("samples")
    removed: list[str] = []
    for name in names:
        target = base / name
        if target.exists():
            shutil.rmtree(target)
            removed.append(str(target))
        target.mkdir(parents=True, exist_ok=True)
    return removed


def prepare_directories(root: str | Path) -> dict[str, str]:
    base = Path(root)
    paths = {
        "samples": base / "samples",
        "snapshots": base / "snapshots",
        "cache": base / "cache",
        "runs": base / "runs",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return {name: str(path) for name, path in paths.items()}
