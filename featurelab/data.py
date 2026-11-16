"""Deterministic synthetic data generation and immutable column snapshots."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple

import numpy as np

from .errors import config_error
from .utils import (
    fingerprint,
    read_yaml,
    require_int,
    require_list,
    require_mapping,
    require_number,
    require_str,
)


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    kind: str
    generator: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "generator": dict(self.generator),
        }


@dataclass(frozen=True)
class TargetSpec:
    name: str
    terms: Tuple[Mapping[str, Any], ...]
    intercept: float
    noise_std: float


@dataclass(frozen=True)
class DataDescription:
    version: int
    seed: int
    rows: int
    missing_token: str
    columns: Tuple[ColumnSpec, ...]
    target: TargetSpec
    source_path: Path
    raw: Mapping[str, Any] = field(repr=False, compare=False)

    @property
    def fingerprint(self) -> str:
        stable = {
            "version": self.version,
            "seed": self.seed,
            "rows": self.rows,
            "missing_token": self.missing_token,
            "columns": [column.to_dict() for column in self.columns],
            "target": {
                "name": self.target.name,
                "terms": [dict(term) for term in self.target.terms],
                "intercept": self.target.intercept,
                "noise_std": self.target.noise_std,
            },
        }
        return fingerprint(stable)


@dataclass(frozen=True)
class ColumnSnapshot:
    name: str
    kind: str
    values: Tuple[Optional[float] | Optional[str], ...]
    missing_token: str
    _fingerprint: str = field(repr=False, compare=False)

    @classmethod
    def build(
        cls,
        name: str,
        kind: str,
        values: Sequence[Optional[float] | Optional[str]],
        missing_token: str,
    ) -> "ColumnSnapshot":
        if kind not in {"numeric", "categorical"}:
            raise config_error("invalid snapshot column kind", column=name, kind=kind)
        stable_values = tuple(values)
        if kind == "numeric":
            normalized: list[Optional[float]] = []
            for value in stable_values:
                if value is None:
                    normalized.append(None)
                elif isinstance(value, bool) or not isinstance(value, (int, float, np.number)):
                    raise config_error(
                        "numeric column contains a non-numeric value", column=name
                    )
                else:
                    number = float(value)
                    if not np.isfinite(number):
                        raise config_error(
                            "numeric column contains a non-finite value", column=name
                        )
                    normalized.append(number)
            stable_values = tuple(normalized)
        else:
            normalized_text: list[Optional[str]] = []
            for value in stable_values:
                if value is None:
                    normalized_text.append(None)
                elif not isinstance(value, str):
                    raise config_error(
                        "categorical column contains a non-string value", column=name
                    )
                else:
                    normalized_text.append(value)
            stable_values = tuple(normalized_text)
        content_fp = fingerprint(
            {
                "name": name,
                "kind": kind,
                "missing_token": missing_token,
                "values": list(stable_values),
            }
        )
        return cls(
            name=name,
            kind=kind,
            values=tuple(stable_values),
            missing_token=missing_token,
            _fingerprint=content_fp,
        )

    @property
    def content_fingerprint(self) -> str:
        return self._fingerprint

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def n_rows(self) -> int:
        return len(self.values)

    def numpy(self, *, copy: bool = True) -> np.ndarray:
        dtype = np.float64 if self.kind == "numeric" else object
        array = np.asarray(self.values, dtype=dtype)
        if copy:
            array = array.copy()
        else:
            array.flags.writeable = False
        return array


@dataclass(frozen=True)
class Dataset:
    description: DataDescription
    columns: Mapping[str, ColumnSnapshot]
    target: ColumnSnapshot
    column_order: Tuple[str, ...]
    target_values: Tuple[int, ...]
    target_classes: Tuple[str, ...]
    fingerprint: str

    @property
    def n_rows(self) -> int:
        return len(self.target_values)

    @property
    def feature_names(self) -> Tuple[str, ...]:
        return tuple(name for name in self.column_order if name != self.target.name)

    def column(self, name: str) -> ColumnSnapshot:
        try:
            return self.columns[name]
        except KeyError as exc:
            raise config_error("unknown data column", column=name) from exc


def _load_column_spec(raw: Any, index: int) -> ColumnSpec:
    item = require_mapping(raw, f"data.columns[{index}]")
    name = require_str(item.get("name"), f"data.columns[{index}].name")
    kind = require_str(item.get("kind"), f"data.columns[{index}].kind")
    if kind not in {"numeric", "categorical"}:
        raise config_error(
            "column kind must be numeric or categorical", column=name, kind=kind
        )
    generator = require_str(
        item.get("generator"), f"data.columns[{index}].generator"
    )
    generator_config = {
        key: value for key, value in item.items() if key not in {"name", "kind"}
    }
    allowed = {
        "numeric": {"normal", "uniform", "lognormal", "constant", "clone"},
        "categorical": {"choice", "high_cardinality", "constant", "clone"},
    }
    if generator not in allowed[kind]:
        raise config_error(
            "generator is incompatible with column kind",
            column=name,
            kind=kind,
            generator=generator,
        )
    return ColumnSpec(name=name, kind=kind, generator=generator_config)


def load_data_description(path: str | Path) -> DataDescription:
    source_path = Path(path)
    raw = read_yaml(source_path)
    root = require_mapping(raw, "data description")
    version = require_int(root.get("version", 1), "data.version", minimum=1)
    seed = require_int(root.get("seed"), "data.seed", minimum=0)
    rows = require_int(root.get("rows"), "data.rows", minimum=2)
    missing_token = require_str(root.get("missing_token", "__MISSING__"), "data.missing_token")
    if missing_token in {"", "nan", "NaN", "None", "null"}:
        raise config_error("missing_token is not a safe literal", token=missing_token)
    columns = tuple(
        _load_column_spec(item, index)
        for index, item in enumerate(require_list(root.get("columns"), "data.columns"))
    )
    if not columns:
        raise config_error("data.columns must not be empty")
    names = [column.name for column in columns]
    if len(set(names)) != len(names):
        raise config_error("data column names must be unique")

    target_raw = require_mapping(root.get("target"), "data.target")
    target_name = require_str(target_raw.get("name"), "data.target.name")
    if target_name in set(names):
        raise config_error(
            "target column must be declared separately from feature columns",
            target=target_name,
        )
    terms_raw = require_list(target_raw.get("terms", []), "data.target.terms")
    terms: list[Mapping[str, Any]] = []
    for index, term_raw in enumerate(terms_raw):
        term = require_mapping(term_raw, f"data.target.terms[{index}]")
        column_name = require_str(term.get("column"), f"data.target.terms[{index}].column")
        if column_name not in names:
            raise config_error("target term references an unknown column", column=column_name)
        if "weight" not in term and "effects" not in term:
            raise config_error("target term needs weight or effects", column=column_name)
        terms.append(dict(term))
    target = TargetSpec(
        name=target_name,
        terms=tuple(terms),
        intercept=require_number(target_raw.get("intercept", 0.0), "data.target.intercept"),
        noise_std=require_number(target_raw.get("noise_std", 0.0), "data.target.noise_std"),
    )
    if target.noise_std < 0:
        raise config_error("data.target.noise_std must be >= 0")
    return DataDescription(
        version=version,
        seed=seed,
        rows=rows,
        missing_token=missing_token,
        columns=columns,
        target=target,
        source_path=source_path,
        raw=root,
    )


def _validate_missing_rate(generator: Mapping[str, Any], column: str) -> float:
    rate = require_number(generator.get("missing_rate", 0.0), f"{column}.missing_rate")
    if rate < 0.0 or rate >= 1.0:
        raise config_error("missing_rate must be in [0, 1)", column=column)
    return rate


def _generate_column(
    spec: ColumnSpec,
    rng: np.random.Generator,
    generated: Mapping[str, np.ndarray],
    rows: int,
) -> np.ndarray:
    generator = spec.generator
    missing_rate = _validate_missing_rate(generator, spec.name)
    method = generator["generator"]
    if spec.kind == "numeric":
        if method == "normal":
            values = rng.normal(
                require_number(generator.get("mean", 0.0), f"{spec.name}.mean"),
                require_number(generator.get("std", 1.0), f"{spec.name}.std"),
                rows,
            )
        elif method == "uniform":
            low = require_number(generator.get("low", 0.0), f"{spec.name}.low")
            high = require_number(generator.get("high", 1.0), f"{spec.name}.high")
            if high <= low:
                raise config_error("uniform high must be greater than low", column=spec.name)
            values = rng.uniform(low, high, rows)
        elif method == "lognormal":
            values = rng.lognormal(
                require_number(generator.get("mean", 0.0), f"{spec.name}.mean"),
                require_number(generator.get("sigma", 1.0), f"{spec.name}.sigma"),
                rows,
            )
        elif method == "constant":
            values = np.full(
                rows,
                require_number(generator.get("value", 0.0), f"{spec.name}.value"),
                dtype=np.float64,
            )
        elif method == "clone":
            source = require_str(generator.get("source"), f"{spec.name}.source")
            if source not in generated:
                raise config_error("clone source must be declared before clone", column=spec.name)
            source_values = generated[source]
            noise = require_number(generator.get("noise_std", 0.0), f"{spec.name}.noise_std")
            if noise < 0:
                raise config_error("clone noise_std must be >= 0", column=spec.name)
            result_values: list[Optional[float]] = []
            for source_value in source_values:
                if source_value is None:
                    result_values.append(None)
                else:
                    value = float(source_value)
                    if noise:
                        value += float(rng.normal(0.0, noise))
                    result_values.append(value)
            values = np.asarray(result_values, dtype=object)
        else:  # pragma: no cover - validated earlier
            raise AssertionError(method)
        minimum = generator.get("min")
        maximum = generator.get("max")
        result = []
        for value in values:
            if value is None:
                result.append(None)
                continue
            numeric_value = float(value)
            if minimum is not None:
                numeric_value = max(
                    numeric_value, require_number(minimum, f"{spec.name}.min")
                )
            if maximum is not None:
                numeric_value = min(
                    numeric_value, require_number(maximum, f"{spec.name}.max")
                )
            result.append(numeric_value)
    else:
        if method == "choice":
            choices = [
                require_str(value, f"{spec.name}.values")
                for value in require_list(generator.get("values"), f"{spec.name}.values")
            ]
            if not choices:
                raise config_error("category choices must not be empty", column=spec.name)
            probabilities = generator.get("probabilities")
            if probabilities is not None:
                probabilities = [
                    require_number(value, f"{spec.name}.probabilities")
                    for value in require_list(probabilities, f"{spec.name}.probabilities")
                ]
                if len(probabilities) != len(choices):
                    raise config_error("probabilities length mismatch", column=spec.name)
                if any(value < 0 for value in probabilities) or abs(sum(probabilities) - 1.0) > 1e-9:
                    raise config_error("probabilities must be non-negative and sum to 1", column=spec.name)
            indexes = rng.choice(len(choices), size=rows, p=probabilities)
            result = [choices[int(index)] for index in indexes]
        elif method == "high_cardinality":
            cardinality = require_int(generator.get("cardinality"), f"{spec.name}.cardinality", minimum=1)
            prefix = require_str(generator.get("prefix", "cat"), f"{spec.name}.prefix")
            # A Zipf-like rank distribution makes high-cardinality data useful.
            ranks = rng.zipf(require_number(generator.get("zipf", 1.35), f"{spec.name}.zipf"), rows)
            indexes = (ranks - 1) % cardinality
            result = [f"{prefix}-{int(index):05d}" for index in indexes]
        elif method == "constant":
            value = require_str(generator.get("value"), f"{spec.name}.value")
            result = [value] * rows
        elif method == "clone":
            source = require_str(generator.get("source"), f"{spec.name}.source")
            if source not in generated:
                raise config_error("clone source must be declared before clone", column=spec.name)
            result = list(generated[source])
        else:  # pragma: no cover - validated earlier
            raise AssertionError(method)
    if missing_rate:
        missing = rng.random(rows) < missing_rate
        result = [None if bool(flag) else value for value, flag in zip(result, missing)]
    return np.asarray(result, dtype=object)


def _category_effect(term: Mapping[str, Any], raw_value: Any) -> float:
    if raw_value is None:
        return 0.0
    effects = term.get("effects")
    if effects is None:
        return 0.0
    effects_map = require_mapping(effects, "target term effects")
    return require_number(effects_map.get(str(raw_value), 0.0), "target effect")


def generate_dataset(description: DataDescription, target_column: str) -> Dataset:
    rng = np.random.default_rng(description.seed)
    generated: dict[str, np.ndarray] = {}
    snapshots: dict[str, ColumnSnapshot] = {}
    for spec in description.columns:
        values = _generate_column(spec, rng, generated, description.rows)
        generated[spec.name] = values
        snapshots[spec.name] = ColumnSnapshot.build(
            spec.name, spec.kind, list(values), description.missing_token
        )

    target_spec = description.target
    if target_spec.name != target_column:
        raise config_error(
            "experiment target_column does not match data description target",
            config_target=target_column,
            data_target=target_spec.name,
        )
    latent = np.full(description.rows, target_spec.intercept, dtype=np.float64)
    for term in target_spec.terms:
        column_name = str(term["column"])
        values = generated[column_name]
        snapshot = snapshots[column_name]
        if snapshot.kind == "numeric":
            if "weight" not in term:
                raise config_error("numeric target term needs weight", column=column_name)
            numeric = np.asarray(
                [0.0 if value is None else float(value) for value in values],
                dtype=np.float64,
            )
            if np.std(numeric) > 0:
                numeric = (numeric - float(np.mean(numeric))) / float(np.std(numeric))
            else:
                numeric = numeric * 0.0
            latent += require_number(term["weight"], "target weight") * numeric
        else:
            if "effects" not in term:
                raise config_error("categorical target term needs effects", column=column_name)
            latent += np.asarray(
                [_category_effect(term, value) for value in values], dtype=np.float64
            )
    if target_spec.noise_std:
        latent += rng.normal(0.0, target_spec.noise_std, description.rows)
    probabilities = 1.0 / (1.0 + np.exp(-np.clip(latent, -40.0, 40.0)))
    labels = (rng.random(description.rows) < probabilities).astype(np.int64)
    target_values = tuple(int(value) for value in labels)
    target_snapshot = ColumnSnapshot.build(
        target_spec.name,
        "categorical",
        [str(value) for value in target_values],
        description.missing_token,
    )
    classes = tuple(sorted(set(str(value) for value in target_values)))
    if len(classes) != 2:
        raise config_error(
            "target must contain exactly two observed classes", classes=list(classes)
        )
    all_columns = dict(snapshots)
    all_columns[target_spec.name] = target_snapshot
    stable = {
        "description": description.fingerprint,
        "target_column": target_column,
        "target_values": list(target_values),
        "columns": [
            {
                "name": snapshot.name,
                "kind": snapshot.kind,
                "fingerprint": snapshot.content_fingerprint,
            }
            for snapshot in all_columns.values()
        ],
    }
    return Dataset(
        description=description,
        columns=all_columns,
        target=target_snapshot,
        column_order=tuple(spec.name for spec in description.columns),
        target_values=target_values,
        target_classes=classes,
        fingerprint=fingerprint(stable),
    )


def load_dataset(description: DataDescription, target_column: str) -> Dataset:
    return generate_dataset(description, target_column)
