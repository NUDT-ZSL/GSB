"""Deterministic synthetic table generation and tabular preprocessing."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .config import DataConfig, ExperimentConfig, SplitConfig
from .errors import E_CONFIG_INVALID, Failure

MISSING_CATEGORY = "__MISSING__"
SUPPORTED_COLUMN_TYPES = {"numeric", "categorical", "redundant", "target"}


@dataclass
class ColumnSpec:
    name: str
    kind: str
    options: dict[str, Any]


@dataclass
class DataSpec:
    schema_version: int
    seed: int
    rows: int
    target_column: str
    columns: list[ColumnSpec]
    failures: list[Failure] = field(default_factory=list)

    @property
    def columns_by_name(self) -> dict[str, ColumnSpec]:
        return {column.name: column for column in self.columns}


@dataclass
class Dataset:
    X_train: np.ndarray
    X_validation: np.ndarray
    y_train: np.ndarray
    y_validation: np.ndarray
    feature_names: tuple[str, ...]
    source_feature_columns: tuple[str, ...]
    target_column: str
    data_fingerprint: str
    split_indices: tuple[int, ...]
    numeric_means: dict[str, float]
    numeric_scales: dict[str, float]
    categorical_levels: dict[str, tuple[str, ...]]
    audit: dict[str, Any]

    @property
    def train_size(self) -> int:
        return int(self.X_train.shape[0])

    @property
    def validation_size(self) -> int:
        return int(self.X_validation.shape[0])

    @property
    def feature_count(self) -> int:
        return int(self.X_train.shape[1])


def _invalid(
    failures: list[Failure],
    message: str,
    *,
    column: str | None = None,
    scheme: str | None = None,
    phase: str = "data_validation",
) -> None:
    failures.append(
        Failure(
            business_code=E_CONFIG_INVALID,
            phase=phase,
            stage=phase,
            message=message,
            exception_type="DataSpecValidationError",
            column=column,
            scheme=scheme,
        )
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _missing_rate(value: Any, column: str, failures: list[Failure]) -> float:
    number = _finite_number(value)
    if number is None or not 0.0 <= number <= 1.0:
        _invalid(
            failures,
            f"column {column!r} missing_rate must be a finite number in [0, 1]",
            column=column,
        )
        return 0.0
    return number


def _parse_columns(raw: Any, failures: list[Failure]) -> list[ColumnSpec]:
    entries: list[tuple[str | None, Any]] = []
    if isinstance(raw, list):
        entries = [(None, item) for item in raw]
    elif isinstance(raw, dict):
        entries = [(str(name), item) for name, item in raw.items()]
    else:
        _invalid(failures, "data columns must be a list or mapping", column="columns")
        return []

    parsed: list[ColumnSpec] = []
    seen: set[str] = set()
    for index, (mapping_name, item) in enumerate(entries):
        label = f"columns[{index}]"
        if not isinstance(item, dict):
            _invalid(failures, f"{label} must be a mapping", column=label)
            continue
        name = mapping_name if mapping_name is not None else item.get("name")
        if not isinstance(name, str) or not name.strip():
            _invalid(failures, f"{label}.name must be a non-empty string", column=label)
            continue
        name = name.strip()
        if name in seen:
            _invalid(failures, f"duplicate column {name!r}", column=name)
            continue
        seen.add(name)

        kind_raw = item.get("type")
        kind = kind_raw.strip().lower() if isinstance(kind_raw, str) else ""
        if kind not in SUPPORTED_COLUMN_TYPES:
            _invalid(
                failures,
                f"column {name!r} has unsupported or mismatched type {kind_raw!r}",
                column=name,
            )
            continue

        options = dict(item)
        if kind == "numeric":
            distribution = options.get("distribution", "normal")
            if not isinstance(distribution, str) or distribution.lower() not in {"normal", "uniform"}:
                _invalid(failures, f"numeric column {name!r} distribution must be normal or uniform", column=name)
                distribution = "normal"
            options["distribution"] = distribution.lower()
            if distribution.lower() == "normal":
                mean = _finite_number(options.get("mean", 0.0))
                std = _finite_number(options.get("std", 1.0))
                if mean is None:
                    _invalid(failures, f"numeric column {name!r} mean must be finite", column=name)
                    mean = 0.0
                if std is None or std <= 0.0:
                    _invalid(failures, f"numeric column {name!r} std must be positive and finite", column=name)
                    std = 1.0
                options["mean"], options["std"] = mean, std
            else:
                low = _finite_number(options.get("low", 0.0))
                high = _finite_number(options.get("high", 1.0))
                if low is None:
                    _invalid(failures, f"numeric column {name!r} low must be finite", column=name)
                    low = 0.0
                if high is None or high <= low:
                    _invalid(failures, f"numeric column {name!r} high must be finite and greater than low", column=name)
                    high = low + 1.0
                options["low"], options["high"] = low, high
            options["missing_rate"] = _missing_rate(options.get("missing_rate", 0.0), name, failures)

        elif kind == "categorical":
            categories_raw = options.get("categories")
            categories: list[str] = []
            if not isinstance(categories_raw, list) or not categories_raw:
                _invalid(failures, f"categorical column {name!r} needs a non-empty categories list", column=name)
            else:
                for category in categories_raw:
                    if not isinstance(category, (str, int, float)) or isinstance(category, bool):
                        _invalid(failures, f"categorical column {name!r} has a non-scalar category", column=name)
                        continue
                    text = str(category)
                    if text and text not in categories:
                        categories.append(text)
            if not categories:
                categories = [MISSING_CATEGORY]
            probabilities_raw = options.get("probabilities")
            probabilities: list[float] | None = None
            if probabilities_raw is not None:
                if not isinstance(probabilities_raw, list) or len(probabilities_raw) != len(categories):
                    _invalid(
                        failures,
                        f"categorical column {name!r} probabilities must match the categories length",
                        column=name,
                    )
                else:
                    parsed_probabilities = [_finite_number(value) for value in probabilities_raw]
                    if any(value is None or value < 0.0 for value in parsed_probabilities):
                        _invalid(failures, f"categorical column {name!r} probabilities must be non-negative", column=name)
                    elif sum(value for value in parsed_probabilities if value is not None) <= 0.0:
                        _invalid(failures, f"categorical column {name!r} probabilities must have positive sum", column=name)
                    else:
                        probabilities = [float(value) for value in parsed_probabilities if value is not None]
            options["categories"] = categories
            options["probabilities"] = probabilities
            options["missing_rate"] = _missing_rate(options.get("missing_rate", 0.0), name, failures)

        elif kind == "redundant":
            source = options.get("source")
            if not isinstance(source, str) or not source.strip():
                _invalid(failures, f"redundant column {name!r} needs a source column", column=name)
                source = ""
            noise_std = _finite_number(options.get("noise_std", 0.0))
            if noise_std is None or noise_std < 0.0:
                _invalid(failures, f"redundant column {name!r} noise_std must be non-negative", column=name)
                noise_std = 0.0
            options["source"], options["noise_std"] = source.strip(), noise_std

        elif kind == "target":
            formula = options.get("formula", {})
            if not isinstance(formula, dict):
                _invalid(failures, f"target column {name!r} formula must be a mapping", column=name)
                formula = {}
            options["formula"] = formula
            noise_std = _finite_number(options.get("noise_std", 0.0))
            if noise_std is None or noise_std < 0.0:
                _invalid(failures, f"target column {name!r} noise_std must be non-negative", column=name)
                noise_std = 0.0
            options["noise_std"] = noise_std

        parsed.append(ColumnSpec(name=name, kind=kind, options=options))
    return parsed


def load_data_spec(path: str | Path) -> DataSpec:
    source = Path(path).expanduser().resolve()
    failures: list[Failure] = []
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except Exception as exc:
        _invalid(
            failures,
            f"unable to read data description: {exc}",
            phase="data_load",
        )
        raw = {}
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        _invalid(failures, "data description must be a mapping", phase="data_load")
        raw = {}

    schema_version = raw.get("schema_version", 1)
    if schema_version != 1:
        _invalid(failures, "data description schema_version must be 1", column="schema_version")
        schema_version = 1

    seed = raw.get("seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
        _invalid(failures, "data seed must be a non-negative integer", column="seed")
        seed = 0

    rows = _positive_int(raw.get("rows"))
    if rows is None:
        _invalid(failures, "data rows must be a positive integer", column="rows")
        rows = 0

    target_column = raw.get("target_column")
    if not isinstance(target_column, str) or not target_column.strip():
        _invalid(failures, "data target_column must be a non-empty string", column="target_column")
        target_column = ""
    target_column = target_column.strip()

    columns = _parse_columns(raw.get("columns"), failures)
    by_name = {column.name: column for column in columns}

    targets = [column for column in columns if column.kind == "target"]
    if len(targets) > 1:
        _invalid(failures, "exactly one target column is allowed; found multiple", column="target")
    if target_column and target_column not in by_name:
        _invalid(failures, f"target column {target_column!r} does not exist", column=target_column)
    elif target_column and by_name[target_column].kind != "target":
        _invalid(
            failures,
            f"target column {target_column!r} has type {by_name[target_column].kind!r}, expected target",
            column=target_column,
        )

    for column in columns:
        if column.kind == "redundant":
            source_name = column.options.get("source", "")
            source_column = by_name.get(source_name)
            if not source_name:
                continue
            if source_column is None:
                _invalid(
                    failures,
                    f"redundant column {column.name!r} references unknown source {source_name!r}",
                    column=column.name,
                )
            elif source_column.kind not in {"numeric", "redundant"}:
                _invalid(
                    failures,
                    f"redundant column {column.name!r} source {source_name!r} must be numeric",
                    column=column.name,
                )
        elif column.kind == "target":
            formula = column.options.get("formula", {})
            coefficients = formula.get("coefficients", {})
            if not isinstance(coefficients, dict):
                _invalid(failures, f"target formula coefficients must be a mapping", column=column.name)
                coefficients = {}
            for term in coefficients:
                if term not in by_name:
                    _invalid(
                        failures,
                        f"target formula references unknown column {term!r}",
                        column=column.name,
                    )
                elif by_name[term].kind not in {"numeric", "redundant"}:
                    _invalid(
                        failures,
                        f"target numeric term {term!r} is not numeric",
                        column=column.name,
                    )
                elif _finite_number(coefficients[term]) is None:
                    _invalid(
                        failures,
                        f"target numeric coefficient for {term!r} must be finite",
                        column=column.name,
                    )
            category_effects = formula.get("category_effects", {})
            if not isinstance(category_effects, dict):
                _invalid(failures, f"target category_effects must be a mapping", column=column.name)
                category_effects = {}
            for effect_name in category_effects:
                effect_column = by_name.get(effect_name)
                if effect_column is None:
                    _invalid(
                        failures,
                        f"target category effect references unknown column {effect_name!r}",
                        column=column.name,
                    )
                elif effect_column.kind != "categorical":
                    _invalid(
                        failures,
                        f"target category effect {effect_name!r} does not refer to a categorical column",
                        column=column.name,
                    )
                elif not isinstance(category_effects[effect_name], dict):
                    _invalid(
                        failures,
                        f"target category effects for {effect_name!r} must be a mapping",
                        column=column.name,
                    )
                else:
                    for category, effect in category_effects[effect_name].items():
                        if _finite_number(effect) is None:
                            _invalid(
                                failures,
                                f"target category effect {effect_name}={category!r} must be finite",
                                column=column.name,
                            )
            bias = formula.get("bias", 0.0)
            if _finite_number(bias) is None:
                _invalid(failures, "target formula bias must be finite", column=column.name)

    return DataSpec(
        schema_version=1,
        seed=seed,
        rows=rows,
        target_column=target_column,
        columns=columns,
        failures=failures,
    )


def _normalized_probabilities(options: dict[str, Any]) -> np.ndarray | None:
    probabilities = options.get("probabilities")
    if not probabilities:
        return None
    values = np.asarray(probabilities, dtype=np.float64)
    total = float(np.sum(values))
    if total <= 0.0:
        return None
    return values / total


def _generate_columns(spec: DataSpec, target_name: str) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    rng = np.random.default_rng(spec.seed)
    values: dict[str, np.ndarray] = {}
    audit: dict[str, Any] = {
        "seed": spec.seed,
        "rows": spec.rows,
        "target_column": target_name,
        "columns": {},
        "generation_order": [column.name for column in spec.columns],
    }

    for column in spec.columns:
        if column.name == target_name or column.kind == "target":
            continue
        options = column.options
        if column.kind == "numeric":
            if options["distribution"] == "uniform":
                generated = rng.uniform(options["low"], options["high"], size=spec.rows)
            else:
                generated = rng.normal(options["mean"], options["std"], size=spec.rows)
            missing_mask = rng.random(spec.rows) < options["missing_rate"]
            observed = generated[~missing_mask]
            fill_value = float(np.median(observed)) if observed.size else 0.0
            generated = generated.copy()
            generated[missing_mask] = fill_value
            values[column.name] = generated.astype(np.float64, copy=False)
            audit["columns"][column.name] = {
                "type": "numeric",
                "missing_count": int(np.sum(missing_mask)),
                "imputation": "median",
                "fill_value": fill_value,
            }
        elif column.kind == "categorical":
            categories = list(options["categories"])
            probabilities = _normalized_probabilities(options)
            generated = rng.choice(categories, size=spec.rows, p=probabilities)
            missing_mask = rng.random(spec.rows) < options["missing_rate"]
            observed = generated[~missing_mask]
            if observed.size:
                observed_values, counts = np.unique(observed.astype(str), return_counts=True)
                max_count = int(np.max(counts))
                fill_value = sorted(str(value) for value, count in zip(observed_values, counts) if int(count) == max_count)[0]
            else:
                fill_value = MISSING_CATEGORY
            generated = generated.astype(str).copy()
            generated[missing_mask] = fill_value
            values[column.name] = generated
            audit["columns"][column.name] = {
                "type": "categorical",
                "missing_count": int(np.sum(missing_mask)),
                "imputation": "mode",
                "fill_value": fill_value,
                "categories": categories,
            }
        elif column.kind == "redundant":
            source_name = options.get("source", "")
            source_values = values.get(source_name)
            if source_values is None:
                generated = np.zeros(spec.rows, dtype=np.float64)
            else:
                generated = np.asarray(source_values, dtype=np.float64).copy()
                generated = generated + rng.normal(0.0, options["noise_std"], size=spec.rows)
            values[column.name] = generated
            audit["columns"][column.name] = {
                "type": "redundant",
                "source": source_name,
                "noise_std": options["noise_std"],
            }

    target_column = next((column for column in spec.columns if column.name == target_name), None)
    if target_column is None or target_column.kind != "target":
        return values, audit

    formula = target_column.options.get("formula", {})
    bias = _finite_number(formula.get("bias", 0.0))
    y = np.full(spec.rows, 0.0 if bias is None else bias, dtype=np.float64)
    coefficients = formula.get("coefficients", {})
    if isinstance(coefficients, dict):
        for term in sorted(coefficients):
            coefficient = _finite_number(coefficients[term])
            source_values = values.get(term)
            if coefficient is not None and source_values is not None:
                y = y + coefficient * np.asarray(source_values, dtype=np.float64)
    category_effects = formula.get("category_effects", {})
    if isinstance(category_effects, dict):
        for effect_name in sorted(category_effects):
            source_values = values.get(effect_name)
            effects = category_effects[effect_name]
            if source_values is None or not isinstance(effects, dict):
                continue
            for category in sorted(effects):
                effect = _finite_number(effects[category])
                if effect is not None:
                    y = y + np.where(source_values.astype(str) == str(category), effect, 0.0)
    y = y + rng.normal(0.0, target_column.options["noise_std"], size=spec.rows)
    values[target_name] = y.astype(np.float64, copy=False)
    audit["target"] = {
        "formula": formula,
        "noise_std": target_column.options["noise_std"],
    }
    return values, audit


def _validate_cross_configuration(
    config: ExperimentConfig,
    spec: DataSpec,
    failures: list[Failure],
) -> tuple[str, list[str]]:
    by_name = spec.columns_by_name
    target_name = config.data.target_column_override or spec.target_column
    if target_name:
        if target_name not in by_name:
            _invalid(failures, f"configured target column {target_name!r} is unknown", column=target_name)
            target_name = spec.target_column
        elif by_name[target_name].kind != "target":
            _invalid(
                failures,
                f"configured target column {target_name!r} has mismatched type {by_name[target_name].kind!r}",
                column=target_name,
            )
            target_name = spec.target_column

    for column_name, expected_type in config.data.expected_column_types.items():
        column = by_name.get(column_name)
        if column is None:
            _invalid(
                failures,
                f"expected column {column_name!r} is unknown",
                column=column_name,
            )
        elif column.kind != expected_type:
            _invalid(
                failures,
                f"column {column_name!r} type mismatch: expected {expected_type}, found {column.kind}",
                column=column_name,
            )

    requested = list(config.data.feature_columns)
    selected: list[str] = []
    if requested:
        for name in requested:
            column = by_name.get(name)
            if column is None:
                _invalid(failures, f"feature column {name!r} is unknown", column=name)
                continue
            if name == target_name or column.kind == "target":
                _invalid(failures, f"feature column {name!r} must not be the target column", column=name)
                continue
            selected.append(name)
    else:
        selected = [column.name for column in spec.columns if column.name != target_name and column.kind != "target"]
    return target_name, selected


def _one_hot(names: list[str], values: np.ndarray, levels: tuple[str, ...]) -> np.ndarray:
    encoded = np.zeros((values.shape[0], len(levels)), dtype=np.float64)
    for level_index, level in enumerate(levels):
        encoded[:, level_index] = (values.astype(str) == level).astype(np.float64)
    return encoded


def _fingerprint_arrays(
    feature_names: tuple[str, ...],
    X_train: np.ndarray,
    X_validation: np.ndarray,
    y_train: np.ndarray,
    y_validation: np.ndarray,
    split_indices: tuple[int, ...],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"regularization-lab-data-v1\0")
    digest.update("\0".join(feature_names).encode("utf-8"))
    digest.update(b"\0")
    digest.update(np.asarray(split_indices, dtype="<i8").tobytes(order="C"))
    for array in (X_train, X_validation, y_train, y_validation):
        contiguous = np.ascontiguousarray(array, dtype="<f8")
        digest.update(contiguous.tobytes(order="C"))
    return digest.hexdigest()


def build_dataset(
    config: ExperimentConfig,
    spec: DataSpec | None = None,
) -> tuple[Dataset | None, list[Failure]]:
    """Generate and preprocess a dataset, returning isolated validation failures."""

    spec = spec if spec is not None else load_data_spec(config.data.spec_path)
    failures = [*spec.failures]
    if spec.rows < 2:
        _invalid(failures, "data generation requires at least two rows", column="rows")
        return None, failures

    by_name = spec.columns_by_name
    target_name, selected_names = _validate_cross_configuration(config, spec, failures)
    if not target_name or target_name not in by_name or by_name[target_name].kind != "target":
        _invalid(failures, "target column is missing or has a mismatched type", column="target")
        return None, failures
    if not selected_names:
        _invalid(failures, "no usable feature columns remain after validation", column="feature_columns")
        return None, failures

    values, audit = _generate_columns(spec, target_name)
    if target_name not in values:
        _invalid(failures, "target values were not generated", column=target_name)
        return None, failures

    raw_blocks: list[np.ndarray] = []
    feature_names: list[str] = []
    numeric_indices: list[int] = []
    numeric_means: dict[str, float] = {}
    numeric_scales: dict[str, float] = {}
    categorical_levels: dict[str, tuple[str, ...]] = {}

    for name in selected_names:
        column = by_name[name]
        if column.kind in {"numeric", "redundant"}:
            feature_names.append(name)
            numeric_indices.append(len(feature_names) - 1)
            raw_blocks.append(np.asarray(values[name], dtype=np.float64).reshape(-1, 1))
        elif column.kind == "categorical":
            levels = tuple(sorted({*map(str, column.options.get("categories", [])), MISSING_CATEGORY}))
            categorical_levels[name] = levels
            for level in levels:
                feature_names.append(f"{name}={level}")
            raw_blocks.append(_one_hot(feature_names, np.asarray(values[name], dtype=str), levels))

    if not feature_names:
        _invalid(failures, "categorical and numeric encoding produced no features", column="feature_columns")
        return None, failures

    X_raw = np.concatenate(raw_blocks, axis=1) if raw_blocks else np.empty((spec.rows, 0), dtype=np.float64)
    y = np.asarray(values[target_name], dtype=np.float64)

    split_rng = np.random.default_rng(config.split.seed)
    permutation = split_rng.permutation(spec.rows)
    train_count = int(spec.rows * config.split.train_ratio)
    train_count = min(max(train_count, 1), spec.rows - 1)
    split_indices = tuple(int(index) for index in permutation)
    train_indices = np.asarray(split_indices[:train_count], dtype=np.int64)
    validation_indices = np.asarray(split_indices[train_count:], dtype=np.int64)

    X_train = np.array(X_raw[train_indices], dtype=np.float64, copy=True)
    X_validation = np.array(X_raw[validation_indices], dtype=np.float64, copy=True)
    y_train = np.array(y[train_indices], dtype=np.float64, copy=True)
    y_validation = np.array(y[validation_indices], dtype=np.float64, copy=True)

    for name, feature_index in zip([name for name in selected_names if by_name[name].kind in {"numeric", "redundant"}], numeric_indices):
        column_values = X_train[:, feature_index]
        mean = float(np.mean(column_values))
        scale = float(np.std(column_values, ddof=0))
        if not math.isfinite(scale) or scale == 0.0:
            scale = 1.0
        X_train[:, feature_index] = (X_train[:, feature_index] - mean) / scale
        X_validation[:, feature_index] = (X_validation[:, feature_index] - mean) / scale
        numeric_means[name] = mean
        numeric_scales[name] = scale

    final_feature_names = tuple(feature_names)
    fingerprint = _fingerprint_arrays(
        final_feature_names,
        X_train,
        X_validation,
        y_train,
        y_validation,
        split_indices,
    )

    audit = {
        **audit,
        "target_column": target_name,
        "selected_feature_columns": selected_names,
        "feature_names": list(final_feature_names),
        "numeric_standardization": {
            name: {"mean": numeric_means[name], "scale": numeric_scales[name]}
            for name in numeric_means
        },
        "categorical_levels": {name: list(levels) for name, levels in categorical_levels.items()},
        "train_size": int(train_indices.size),
        "validation_size": int(validation_indices.size),
    }
    dataset = Dataset(
        X_train=X_train,
        X_validation=X_validation,
        y_train=y_train,
        y_validation=y_validation,
        feature_names=final_feature_names,
        source_feature_columns=tuple(selected_names),
        target_column=target_name,
        data_fingerprint=fingerprint,
        split_indices=split_indices,
        numeric_means=numeric_means,
        numeric_scales=numeric_scales,
        categorical_levels=categorical_levels,
        audit=audit,
    )
    return dataset, failures
