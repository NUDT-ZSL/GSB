"""YAML experiment configuration loading, validation and normalization."""
from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from typing import Any

import yaml

from .errors import BenchError, E_CONFIG_INVALID, E_TARGET_MISSING
from .hashing import fingerprint

KNOWN_COLUMN_TYPES = {"numeric", "categorical_low", "categorical_high",
                      "numeric_redundant", "categorical_redundant"}
KNOWN_DEGRADE = {"hash_bucket", "skip"}
KNOWN_METHODS = ("pca", "random_projection", "variance")
DEFAULT_STRATEGY_TABLE = (
    {"name": "small_sample_variance", "path": "variance", "max_order": 1,
     "n_ge": 0, "n_lt": 200, "p_gt": 0.0, "occupancy_gt": 0.0,
     "small_sample_gt": 0.0},
    {"name": "wide_sparse_rp", "path": "random_projection", "max_order": 2,
     "n_ge": 200, "n_lt": 2000, "p_gt": 200.0, "occupancy_gt": 0.5,
     "small_sample_gt": 0.0},
    {"name": "structured_pca", "path": "pca", "max_order": 3,
     "n_ge": 2000, "n_lt": 10 ** 12, "p_gt": 0.0, "occupancy_gt": 0.0,
     "small_sample_gt": 0.0},
)


@dataclass
class ColumnSpec:
    name: str
    type: str
    base_cardinality: int = 4
    missing_rate: float = 0.0
    redundant_of: str | None = None


@dataclass
class ScaleSpec:
    id: str
    samples: int
    feature_multiplier: float = 1.0
    cardinality_multiplier: float = 1.0
    note: str = ""


@dataclass
class CrossSpec:
    max_order: int
    whitelist: list[str]
    cardinality_budget: int
    degrade_strategy: str
    min_order: int = 1


@dataclass
class ReductionSpec:
    methods: list[str]
    target_dim: int
    variance_threshold: float
    rp_seed_tag: str = "random_projection"


@dataclass
class Config:
    raw: dict[str, Any]
    experiment: str
    seed: int
    impute_missing: bool
    workers: int
    work_dir: str
    columns: list[ColumnSpec]
    target_column: str
    scales: list[ScaleSpec]
    cross: CrossSpec
    reduction: ReductionSpec
    strategy_table: list[dict[str, Any]]
    config_fingerprint: str


def _require(mapping: dict, key: str, ctx: str, expected_type: type | tuple):
    if key not in mapping:
        raise BenchError(E_CONFIG_INVALID, f"{ctx}: missing key '{key}'")
    value = mapping[key]
    if not isinstance(value, expected_type) or (
            expected_type is int and isinstance(value, bool)):
        raise BenchError(E_CONFIG_INVALID,
                         f"{ctx}.{key}: expected {expected_type}, got {type(value).__name__}")
    return value


def _as_int(value: Any, ctx: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BenchError(E_CONFIG_INVALID, f"{ctx}: expected integer, got {type(value).__name__}")
    if value < minimum:
        raise BenchError(E_CONFIG_INVALID, f"{ctx}: must be >= {minimum}, got {value}")
    return value


def _as_float(value: Any, ctx: str, minimum: float | None = None,
              maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BenchError(E_CONFIG_INVALID, f"{ctx}: expected number, got {type(value).__name__}")
    value = float(value)
    if minimum is not None and value < minimum:
        raise BenchError(E_CONFIG_INVALID, f"{ctx}: must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise BenchError(E_CONFIG_INVALID, f"{ctx}: must be <= {maximum}")
    return value


def _parse_columns(raw_columns: Any) -> list[ColumnSpec]:
    if not isinstance(raw_columns, list) or not raw_columns:
        raise BenchError(E_CONFIG_INVALID, "columns: must be a non-empty list")
    specs: list[ColumnSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_columns):
        ctx = f"columns[{index}]"
        if not isinstance(item, dict):
            raise BenchError(E_CONFIG_INVALID, f"{ctx}: must be a mapping")
        name = _require(item, "name", ctx, str)
        if not name:
            raise BenchError(E_CONFIG_INVALID, f"{ctx}.name: must not be empty")
        if name in seen:
            raise BenchError(E_CONFIG_INVALID, f"{ctx}.name: duplicate column '{name}'")
        seen.add(name)
        ctype = _require(item, "type", ctx, str)
        if ctype not in KNOWN_COLUMN_TYPES:
            raise BenchError(E_CONFIG_INVALID,
                             f"{ctx}.type: unknown column type '{ctype}'")
        spec = ColumnSpec(
            name=name,
            type=ctype,
            base_cardinality=_as_int(item.get("base_cardinality", 4),
                                     f"{ctx}.base_cardinality", minimum=1),
            missing_rate=_as_float(item.get("missing_rate", 0.0),
                                   f"{ctx}.missing_rate", 0.0, 1.0),
            redundant_of=item.get("redundant_of"),
        )
        if spec.redundant_of is not None and not isinstance(spec.redundant_of, str):
            raise BenchError(E_CONFIG_INVALID, f"{ctx}.redundant_of: must be a string")
        specs.append(spec)
    return specs


def _parse_scales(raw_scales: Any) -> list[ScaleSpec]:
    if not isinstance(raw_scales, list) or not raw_scales:
        raise BenchError(E_CONFIG_INVALID, "scales: must be a non-empty list")
    scales: list[ScaleSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_scales):
        ctx = f"scales[{index}]"
        if not isinstance(item, dict):
            raise BenchError(E_CONFIG_INVALID, f"{ctx}: must be a mapping")
        sid = _require(item, "id", ctx, str)
        if sid in seen:
            raise BenchError(E_CONFIG_INVALID, f"{ctx}.id: duplicate scale id '{sid}'")
        seen.add(sid)
        samples = _as_int(_require(item, "samples", ctx, int), f"{ctx}.samples", minimum=1)
        feature_multiplier = _as_float(item.get("feature_multiplier", 1.0),
                                       f"{ctx}.feature_multiplier", minimum=0.1)
        cardinality_multiplier = _as_float(item.get("cardinality_multiplier", 1.0),
                                           f"{ctx}.cardinality_multiplier", minimum=1.0)
        scales.append(ScaleSpec(
            id=sid, samples=samples,
            feature_multiplier=feature_multiplier,
            cardinality_multiplier=cardinality_multiplier,
            note=str(item.get("note", "")),
        ))
    return scales


def _parse_cross(raw: Any, column_names: set[str]) -> CrossSpec:
    if not isinstance(raw, dict):
        raise BenchError(E_CONFIG_INVALID, "cross: must be a mapping")
    max_order = _as_int(_require(raw, "max_order", "cross", int),
                        "cross.max_order", minimum=1)
    min_order = _as_int(raw.get("min_order", 1), "cross.min_order", minimum=1)
    if min_order > max_order:
        raise BenchError(E_CONFIG_INVALID, "cross.min_order must be <= max_order")
    whitelist = _require(raw, "whitelist", "cross", list)
    if not whitelist:
        raise BenchError(E_CONFIG_INVALID, "cross.whitelist: must not be empty")
    for name in whitelist:
        if not isinstance(name, str):
            raise BenchError(E_CONFIG_INVALID, "cross.whitelist entries must be strings")
        if name not in column_names:
            raise BenchError(E_CONFIG_INVALID,
                             f"cross.whitelist references unknown column '{name}'")
    budget = _as_int(_require(raw, "cardinality_budget", "cross", int),
                     "cross.cardinality_budget", minimum=1)
    strategy = _require(raw, "degrade_strategy", "cross", str)
    if strategy not in KNOWN_DEGRADE:
        raise BenchError(E_CONFIG_INVALID,
                         f"cross.degrade_strategy: unknown strategy '{strategy}'")
    return CrossSpec(max_order=max_order, min_order=min_order, whitelist=list(whitelist),
                     cardinality_budget=budget, degrade_strategy=strategy)


def _parse_reduction(raw: Any) -> ReductionSpec:
    if not isinstance(raw, dict):
        raise BenchError(E_CONFIG_INVALID, "reduction: must be a mapping")
    methods = _require(raw, "methods", "reduction", list)
    if not methods:
        raise BenchError(E_CONFIG_INVALID, "reduction.methods: must not be empty")
    for method in methods:
        if method not in KNOWN_METHODS:
            raise BenchError(E_CONFIG_INVALID,
                             f"reduction.methods: unknown method '{method}'")
    target_dim = _as_int(_require(raw, "target_dim", "reduction", int),
                         "reduction.target_dim", minimum=1)
    variance_threshold = _as_float(raw.get("variance_threshold", 0.0),
                                   "reduction.variance_threshold", minimum=0.0)
    return ReductionSpec(methods=list(methods), target_dim=target_dim,
                         variance_threshold=variance_threshold,
                         rp_seed_tag=str(raw.get("rp_seed_tag", "random_projection")))


def _parse_strategy_table(raw: Any) -> list[dict[str, Any]]:
    table = copy.deepcopy(DEFAULT_STRATEGY_TABLE) if raw is None else raw
    if not isinstance(table, (list, tuple)) or not table:
        raise BenchError(E_CONFIG_INVALID, "strategy_table: must be a non-empty list")
    required = {"name", "path", "max_order", "n_ge", "n_lt"}
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(table):
        ctx = f"strategy_table[{index}]"
        if not isinstance(item, dict):
            raise BenchError(E_CONFIG_INVALID, f"{ctx}: must be a mapping")
        missing = required - set(item)
        if missing:
            raise BenchError(E_CONFIG_INVALID, f"{ctx}: missing keys {sorted(missing)}")
        if item["path"] not in KNOWN_METHODS:
            raise BenchError(E_CONFIG_INVALID, f"{ctx}.path: unknown method")
        normalized.append({
            "name": str(item["name"]),
            "path": str(item["path"]),
            "max_order": _as_int(item["max_order"], f"{ctx}.max_order", minimum=1),
            "n_ge": _as_int(item["n_ge"], f"{ctx}.n_ge", minimum=0),
            "n_lt": _as_int(item["n_lt"], f"{ctx}.n_lt", minimum=1),
            "p_gt": _as_float(item.get("p_gt", 0.0), f"{ctx}.p_gt", minimum=0.0),
            "occupancy_gt": _as_float(item.get("occupancy_gt", 0.0),
                                      f"{ctx}.occupancy_gt", 0.0, 1.0),
            "small_sample_gt": _as_float(item.get("small_sample_gt", 0.0),
                                         f"{ctx}.small_sample_gt", 0.0, 1.0),
        })
    return normalized


def _normalize(raw: dict[str, Any], path: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BenchError(E_CONFIG_INVALID, "top-level YAML document must be a mapping")
    experiment = _require(raw, "experiment", "root", str)
    seed = _as_int(_require(raw, "seed", "root", int), "seed", minimum=0)
    if seed > 2 ** 32 - 1:
        raise BenchError(E_CONFIG_INVALID, "seed: must fit in unsigned 32 bits")
    columns = _parse_columns(_require(raw, "columns", "root", list))
    target_column = _require(raw, "target_column", "root", str)
    column_names = {spec.name for spec in columns}
    if target_column not in column_names:
        raise BenchError(E_TARGET_MISSING,
                         f"target_column '{target_column}' is not present in columns")
    for spec in columns:
        if spec.redundant_of is not None and spec.redundant_of not in column_names:
            raise BenchError(E_CONFIG_INVALID,
                             f"column '{spec.name}': redundant_of references "
                             f"unknown column '{spec.redundant_of}'")
    scales = _parse_scales(_require(raw, "scales", "root", list))
    cross = _parse_cross(_require(raw, "cross", "root", dict), column_names)
    reduction = _parse_reduction(_require(raw, "reduction", "root", dict))
    workers = _as_int(raw.get("workers", 1), "workers", minimum=1)
    if workers > 128:
        raise BenchError(E_CONFIG_INVALID, "workers: must be <= 128")
    impute_missing = raw.get("impute_missing", True)
    if not isinstance(impute_missing, bool):
        raise BenchError(E_CONFIG_INVALID, "impute_missing: must be a boolean")
    work_dir = raw.get("work_dir", os.path.join(os.path.dirname(os.path.abspath(path))
                                                 or ".", ".bench_work"))
    if not isinstance(work_dir, str) or not work_dir:
        raise BenchError(E_CONFIG_INVALID, "work_dir: must be a non-empty string")
    strategy_table = _parse_strategy_table(raw.get("strategy_table"))
    # work_dir is intentionally excluded from the fingerprint so identical
    # experiment content produces identical fingerprints across machines/dirs.
    # workers is an execution parameter rather than experiment content: it
    # must not change fingerprints, RP seeds or cache namespaces.
    fingerprint_material = {
        "experiment": experiment,
        "seed": seed,
        "impute_missing": impute_missing,
        "target_column": target_column,
        "columns": [spec.__dict__ for spec in columns],
        "scales": [spec.__dict__ for spec in scales],
        "cross": cross.__dict__,
        "reduction": reduction.__dict__,
        "strategy_table": strategy_table,
    }
    normalized = dict(fingerprint_material)
    normalized["workers"] = workers
    normalized["work_dir"] = os.path.abspath(work_dir)
    return normalized, fingerprint_material


def load_config(path: str) -> Config:
    if not os.path.isfile(path):
        raise BenchError(E_CONFIG_INVALID, f"config file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise BenchError(E_CONFIG_INVALID, f"YAML parse error: {exc}") from exc
    normalized, fingerprint_material = _normalize(
        raw if isinstance(raw, dict) else {}, path)
    config_fingerprint = fingerprint(fingerprint_material, prefix="config:")
    columns = [ColumnSpec(**item) for item in normalized["columns"]]
    scales = [ScaleSpec(**item) for item in normalized["scales"]]
    return Config(
        raw=normalized,
        experiment=normalized["experiment"],
        seed=normalized["seed"],
        impute_missing=normalized["impute_missing"],
        workers=normalized["workers"],
        work_dir=normalized["work_dir"],
        columns=columns,
        target_column=normalized["target_column"],
        scales=scales,
        cross=CrossSpec(**normalized["cross"]),
        reduction=ReductionSpec(**normalized["reduction"]),
        strategy_table=normalized["strategy_table"],
        config_fingerprint=config_fingerprint,
    )


def effective_column_count(config: Config, scale: ScaleSpec) -> int:
    """Number of columns after feature_multiplier scaling (cloned columns added)."""
    base = len(config.columns)
    return max(base, int(round(base * scale.feature_multiplier)))


def categorical_columns(config: Config) -> list[ColumnSpec]:
    return [spec for spec in config.columns
            if spec.type.startswith("categorical") and spec.name != config.target_column]
