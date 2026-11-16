"""Deterministic synthetic tabular dataset generation driven by configuration."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import ColumnSpec, Config, ScaleSpec
from .hashing import fingerprint

MISSING_TOKEN = "__MISSING__"


@dataclass
class SyntheticDataset:
    scale_id: str
    samples: int
    columns: list[str]
    types: list[str]
    numeric: np.ndarray          # float64 matrix, NaN marks missing values
    numeric_names: list[str]
    categorical: np.ndarray      # object matrix of strings, MISSING_TOKEN marks missing
    categorical_names: list[str]
    target: np.ndarray
    extra_columns: list[str]     # cloned column names introduced by feature multiplier
    generation_fingerprint: str
    scale_cardinality: dict[str, int]


def _child_rng(config_seed: int, scale_id: str, tag: str) -> np.random.Generator:
    seed_material = f"{config_seed}|{scale_id}|{tag}"
    digest = fingerprint(seed_material, prefix="rng:")
    return np.random.default_rng(int(digest[:16], 16))


def _cardinality(spec: ColumnSpec, scale: ScaleSpec) -> int:
    if not spec.type.startswith("categorical"):
        return 0
    return max(1, int(round(spec.base_cardinality * scale.cardinality_multiplier)))


def _maybe_missing(values: np.ndarray, spec: ColumnSpec,
                   rng: np.random.Generator) -> np.ndarray:
    if spec.missing_rate <= 0.0:
        return values
    mask = rng.random(values.shape[0]) < spec.missing_rate
    out = values.copy()
    if out.dtype.kind == "f":
        out[mask] = np.nan
    else:
        out[mask] = MISSING_TOKEN
    return out


def _generate_column(spec: ColumnSpec, n_samples: int,
                     rng: np.random.Generator, cardinality: int,
                     source: np.ndarray | None) -> np.ndarray:
    if spec.redundant_of is not None and source is not None:
        if spec.type.startswith("numeric"):
            return source + rng.normal(0.0, 0.01, size=n_samples)
        return source.copy()
    if spec.type.startswith("numeric"):
        return rng.normal(0.0, 1.0, size=n_samples)
    categories = np.array([f"{spec.name}_cat{idx}" for idx in range(cardinality)],
                          dtype=object)
    return categories[rng.integers(0, cardinality, size=n_samples)]


def generate_dataset(config: Config, scale: ScaleSpec) -> SyntheticDataset:
    rng = _child_rng(config.seed, scale.id, "generate")
    n_samples = scale.samples
    target_count = len(config.columns)
    expanded_count = max(target_count,
                         int(round(target_count * scale.feature_multiplier)))
    base_values: dict[str, np.ndarray] = {}
    cardinalities = {spec.name: _cardinality(spec, scale) for spec in config.columns}

    numeric_blocks: list[np.ndarray] = []
    categorical_blocks: list[np.ndarray] = []
    numeric_names: list[str] = []
    categorical_names: list[str] = []
    all_names: list[str] = []
    all_types: list[str] = []
    extra_names: list[str] = []

    ordered = list(config.columns)
    for position in range(expanded_count):
        if position < target_count:
            spec = ordered[position]
            column_name = spec.name
        else:
            template = ordered[position % target_count]
            spec = ColumnSpec(
                name=f"clone{position - target_count:03d}__{template.name}",
                type=template.type,
                base_cardinality=template.base_cardinality,
                missing_rate=template.missing_rate,
                redundant_of=template.name,
            )
            column_name = spec.name
            extra_names.append(column_name)
        source = base_values.get(spec.redundant_of) if spec.redundant_of else None
        values = _generate_column(spec, n_samples, rng,
                                  cardinalities.get(template_name(spec, config), 0),
                                  source)
        values = _maybe_missing(values, spec, rng)
        if position < target_count:
            base_values[spec.name] = values
        all_names.append(column_name)
        all_types.append(spec.type)
        if spec.type.startswith("numeric"):
            numeric_blocks.append(values.astype(np.float64, copy=False))
            numeric_names.append(column_name)
        else:
            categorical_blocks.append(values.astype(object, copy=False))
            categorical_names.append(column_name)

    numeric = np.column_stack(numeric_blocks) if numeric_blocks else \
        np.empty((n_samples, 0), dtype=np.float64)
    categorical = np.column_stack(categorical_blocks) if categorical_blocks else \
        np.empty((n_samples, 0), dtype=object)

    target_idx = all_names.index(config.target_column)
    target_spec = ordered[target_idx]
    if target_spec.type.startswith("numeric"):
        target = numeric[:, numeric_names.index(config.target_column)].copy()
    else:
        col = categorical[:, categorical_names.index(config.target_column)]
        target = _encode_target(col, rng)
    signal_rng = _child_rng(config.seed, scale.id, "target")
    target = target + signal_rng.normal(0.0, 0.05, size=n_samples)

    generation_fingerprint = fingerprint({
        "config": config.config_fingerprint,
        "scale": scale.__dict__,
        "shape": [n_samples, expanded_count],
    }, prefix="dataset:")
    return SyntheticDataset(
        scale_id=scale.id, samples=n_samples, columns=all_names, types=all_types,
        numeric=numeric, numeric_names=numeric_names,
        categorical=categorical, categorical_names=categorical_names,
        target=target, extra_columns=extra_names,
        generation_fingerprint=generation_fingerprint,
        scale_cardinality=cardinalities,
    )


def template_name(spec: ColumnSpec, config: Config) -> str:
    return spec.redundant_of if spec.redundant_of in {c.name for c in config.columns} \
        else spec.name


def _encode_target(col: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    categories = sorted({value for value in col.tolist() if value != MISSING_TOKEN})
    mapping = {name: rng.normal(0.0, 1.0) for name in categories}
    out = np.array([mapping.get(value, 0.0) for value in col], dtype=np.float64)
    return out
