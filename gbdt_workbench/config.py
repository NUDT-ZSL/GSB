from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .errors import E_CONFIG_INVALID, ConfigValidationError, Failure
from .precision import SUPPORTED_PRECISION_TIERS
from .utils import digest_json


CONFIG_SCHEMA_VERSION = 1
DATA_SPEC_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ExperimentConfig:
    seed: int
    n_trees: int
    max_depth: int
    learning_rate: float
    row_subsample: float
    col_subsample: float
    min_samples_leaf: int
    n_bins: int
    precision: str
    tolerance: float
    n_jobs: int
    snapshot_interval: int
    decision_threshold: float
    include_baseline: bool
    fault_tree_indices: tuple[int, ...]
    config_path: Path
    data_spec_path: Path

    def normalized(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "seed": self.seed,
            "experiment": {
                "n_trees": self.n_trees,
                "max_depth": self.max_depth,
                "learning_rate": self.learning_rate,
                "row_subsample": self.row_subsample,
                "col_subsample": self.col_subsample,
                "min_samples_leaf": self.min_samples_leaf,
                "n_bins": self.n_bins,
                "precision": self.precision,
                "tolerance": self.tolerance,
            },
            "runtime": {
                "n_jobs": self.n_jobs,
                "snapshot_interval": self.snapshot_interval,
            },
            "diagnostics": {
                "decision_threshold": self.decision_threshold,
                "include_baseline": self.include_baseline,
                "fault_tree_indices": list(self.fault_tree_indices),
            },
        }


@dataclass(frozen=True)
class LoadedConfig:
    config: ExperimentConfig
    raw_config: dict[str, Any]
    data_spec: dict[str, Any]
    config_fingerprint: str


def _failure(field: str, message: str, **details: Any) -> Failure:
    return Failure(
        code=E_CONFIG_INVALID,
        stage="config",
        field=field,
        message=message,
        details=details,
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _load_yaml(path: Path, stage_field: str, failures: list[Failure]) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError:
        failures.append(_failure(stage_field, "file does not exist", path=str(path)))
    except OSError as exc:
        failures.append(
            _failure(stage_field, "file cannot be read", path=str(path), error=str(exc))
        )
    except yaml.YAMLError as exc:
        failures.append(_failure(stage_field, "invalid YAML", error=str(exc)))
    return None


def _reject_unknown_keys(
    value: dict[str, Any],
    allowed: set[str],
    prefix: str,
    failures: list[Failure],
) -> None:
    for key in sorted(value):
        if key not in allowed:
            failures.append(
                _failure(
                    f"{prefix}.{key}",
                    "unknown configuration field",
                    reason="unknown_field",
                )
            )


def _validate_data_spec(data_spec: Any, failures: list[Failure]) -> None:
    if not isinstance(data_spec, dict):
        failures.append(_failure("data_spec", "data specification must be a mapping"))
        return
    _reject_unknown_keys(
        data_spec,
        {"schema_version", "rows", "columns", "target", "notes"},
        "data_spec",
        failures,
    )
    version = data_spec.get("schema_version")
    if version != DATA_SPEC_SCHEMA_VERSION:
        failures.append(
            _failure(
                "data_spec.schema_version",
                f"expected schema version {DATA_SPEC_SCHEMA_VERSION}",
                actual=version,
            )
        )
    rows = data_spec.get("rows")
    if not _is_int(rows) or rows <= 0:
        failures.append(_failure("data_spec.rows", "rows must be a positive integer"))

    columns = data_spec.get("columns")
    if not isinstance(columns, list) or not columns:
        failures.append(
            _failure("data_spec.columns", "columns must be a non-empty list")
        )
        columns = []

    known_names: set[str] = set()
    valid_columns: list[tuple[int, dict[str, Any], str, str]] = []
    allowed_column_keys = {
        "name",
        "kind",
        "mean",
        "std",
        "min",
        "max",
        "missing_rate",
        "effect",
        "categories",
        "weights",
        "effects",
        "source",
        "noise_std",
    }
    for index, column in enumerate(columns):
        prefix = f"data_spec.columns[{index}]"
        if not isinstance(column, dict):
            failures.append(_failure(prefix, "column definition must be a mapping"))
            continue
        _reject_unknown_keys(column, allowed_column_keys, prefix, failures)
        name = column.get("name")
        if not isinstance(name, str) or not name.strip():
            failures.append(_failure(f"{prefix}.name", "column name must be a non-empty string"))
            continue
        if name in known_names:
            failures.append(
                _failure(f"{prefix}.name", "duplicate column name", column=name)
            )
        known_names.add(name)

        kind = column.get("kind")
        if not isinstance(kind, str):
            failures.append(_failure(f"{prefix}.kind", "column kind must be a string"))
            continue
        if kind not in {"numeric", "categorical", "redundant"}:
            failures.append(
                _failure(
                    f"{prefix}.kind",
                    "unknown column kind",
                    reason="unknown_column_kind",
                    column=name,
                    kind=kind,
                )
            )
            continue
        valid_columns.append((index, column, name, kind))

        missing_rate = column.get("missing_rate", 0.0)
        if not _is_number(missing_rate) or not 0.0 <= float(missing_rate) < 1.0:
            failures.append(
                _failure(
                    f"{prefix}.missing_rate",
                    "missing_rate must be a number in [0, 1)",
                )
            )

        if kind == "numeric":
            mean = column.get("mean")
            std = column.get("std")
            effect = column.get("effect", 0.0)
            if not _is_number(mean):
                failures.append(_failure(f"{prefix}.mean", "numeric mean must be finite"))
            if not _is_number(std) or float(std) < 0.0:
                failures.append(
                    _failure(f"{prefix}.std", "numeric std must be a non-negative finite number")
                )
            if not _is_number(effect):
                failures.append(_failure(f"{prefix}.effect", "effect must be finite"))
            minimum = column.get("min")
            maximum = column.get("max")
            if minimum is not None and not _is_number(minimum):
                failures.append(_failure(f"{prefix}.min", "min must be finite"))
            if maximum is not None and not _is_number(maximum):
                failures.append(_failure(f"{prefix}.max", "max must be finite"))
            if (
                _is_number(minimum)
                and _is_number(maximum)
                and float(minimum) > float(maximum)
            ):
                failures.append(_failure(prefix, "min must not exceed max"))

        elif kind == "categorical":
            categories = column.get("categories")
            if (
                not isinstance(categories, list)
                or not categories
                or any(not isinstance(item, str) or not item for item in categories)
            ):
                failures.append(
                    _failure(
                        f"{prefix}.categories",
                        "categories must be a non-empty list of strings",
                    )
                )
                categories = []
            elif len(set(categories)) != len(categories):
                failures.append(
                    _failure(f"{prefix}.categories", "categories must be unique")
                )
            weights = column.get("weights")
            if weights is not None:
                if (
                    not isinstance(weights, list)
                    or len(weights) != len(categories)
                    or any(not _is_number(weight) or float(weight) <= 0.0 for weight in weights)
                ):
                    failures.append(
                        _failure(
                            f"{prefix}.weights",
                            "weights must be positive numbers matching categories",
                        )
                    )
            effects = column.get("effects", {})
            if not isinstance(effects, dict):
                failures.append(_failure(f"{prefix}.effects", "effects must be a mapping"))
            else:
                for category, effect in effects.items():
                    if category not in categories:
                        failures.append(
                            _failure(
                                f"{prefix}.effects.{category}",
                                "effect references an unknown category",
                                reason="unknown_column_value",
                            )
                        )
                    if not _is_number(effect):
                        failures.append(
                            _failure(f"{prefix}.effects.{category}", "effect must be finite")
                        )

        elif kind == "redundant":
            source = column.get("source")
            if not isinstance(source, str) or not source:
                failures.append(
                    _failure(f"{prefix}.source", "redundant source must be a column name")
                )
            noise_std = column.get("noise_std", 0.0)
            if not _is_number(noise_std) or float(noise_std) < 0.0:
                failures.append(
                    _failure(
                        f"{prefix}.noise_std",
                        "noise_std must be a non-negative finite number",
                    )
                )
            effect = column.get("effect", 0.0)
            if not _is_number(effect):
                failures.append(_failure(f"{prefix}.effect", "effect must be finite"))

    for index, column, name, kind in valid_columns:
        if kind != "redundant":
            continue
        source = column.get("source")
        if source == name:
            failures.append(
                _failure(
                    f"data_spec.columns[{index}].source",
                    "redundant source cannot reference itself",
                    reason="unknown_column",
                )
            )
        elif isinstance(source, str) and source not in known_names:
            failures.append(
                _failure(
                    f"data_spec.columns[{index}].source",
                    "redundant source references an unknown column",
                    reason="unknown_column",
                    source=source,
                )
            )

    redundant_edges = {
        column["name"]: column.get("source")
        for _, column, _, kind in valid_columns
        if kind == "redundant" and isinstance(column.get("source"), str)
    }
    for start in sorted(redundant_edges):
        seen: set[str] = set()
        current: str | None = start
        while current in redundant_edges:
            if current in seen:
                failures.append(
                    _failure(
                        "data_spec.columns",
                        "redundant column dependency cycle detected",
                        reason="redundant_cycle",
                        start=start,
                    )
                )
                break
            seen.add(current)
            current = redundant_edges.get(current)

    target = data_spec.get("target")
    if not isinstance(target, dict):
        failures.append(
            _failure(
                "data_spec.target",
                "target column is missing",
                reason="target_column_missing",
            )
        )
        return
    _reject_unknown_keys(
        target,
        {"name", "positive_rate", "noise_std", "intercept"},
        "data_spec.target",
        failures,
    )
    target_name = target.get("name")
    if not isinstance(target_name, str) or not target_name.strip():
        failures.append(
            _failure(
                "data_spec.target.name",
                "target column is missing",
                reason="target_column_missing",
            )
        )
    elif target_name in known_names:
        failures.append(
            _failure(
                "data_spec.target.name",
                "target column collides with a feature column",
                reason="target_column_conflict",
            )
        )
    positive_rate = target.get("positive_rate", 0.5)
    if not _is_number(positive_rate) or not 0.0 < float(positive_rate) < 1.0:
        failures.append(
            _failure(
                "data_spec.target.positive_rate",
                "positive_rate must be in (0, 1)",
            )
        )
    noise_std = target.get("noise_std", 0.0)
    if not _is_number(noise_std) or float(noise_std) < 0.0:
        failures.append(
            _failure(
                "data_spec.target.noise_std",
                "target noise_std must be a non-negative finite number",
            )
        )
    intercept = target.get("intercept")
    if intercept is not None and not _is_number(intercept):
        failures.append(_failure("data_spec.target.intercept", "intercept must be finite"))


def load_config(config_path: str | Path) -> LoadedConfig:
    path = Path(config_path).resolve()
    failures: list[Failure] = []
    raw_config = _load_yaml(path, "config", failures)
    if not isinstance(raw_config, dict):
        if raw_config is not None:
            failures.append(_failure("config", "configuration root must be a mapping"))
        if failures:
            raise ConfigValidationError(failures)
        raise ConfigValidationError([_failure("config", "configuration is empty")])

    _reject_unknown_keys(
        raw_config,
        {
            "schema_version",
            "seed",
            "data_spec",
            "experiment",
            "runtime",
            "diagnostics",
            "notes",
        },
        "config",
        failures,
    )
    if raw_config.get("schema_version") != CONFIG_SCHEMA_VERSION:
        failures.append(
            _failure(
                "config.schema_version",
                f"expected schema version {CONFIG_SCHEMA_VERSION}",
                actual=raw_config.get("schema_version"),
            )
        )
    seed = raw_config.get("seed")
    if not _is_int(seed) or seed < 0:
        failures.append(_failure("config.seed", "seed must be a non-negative integer"))
        seed = 0

    data_spec_value = raw_config.get("data_spec")
    data_spec_path = path.parent / "data_spec.yaml"
    data_spec_raw: Any = None
    if not isinstance(data_spec_value, str) or not data_spec_value.strip():
        failures.append(
            _failure("config.data_spec", "data_spec must be a non-empty relative path")
        )
    else:
        data_spec_path = (path.parent / data_spec_value).resolve()
        data_spec_raw = _load_yaml(data_spec_path, "data_spec", failures)
        if data_spec_raw is None:
            failures.append(
                _failure("data_spec", "data specification is empty or invalid")
            )
        else:
            _validate_data_spec(data_spec_raw, failures)

    experiment = raw_config.get("experiment")
    if not isinstance(experiment, dict):
        failures.append(_failure("config.experiment", "experiment must be a mapping"))
        experiment = {}
    else:
        _reject_unknown_keys(
            experiment,
            {
                "n_trees",
                "max_depth",
                "learning_rate",
                "row_subsample",
                "col_subsample",
                "min_samples_leaf",
                "n_bins",
                "precision",
                "tolerance",
            },
            "config.experiment",
            failures,
        )

    n_trees = experiment.get("n_trees")
    if not _is_int(n_trees) or n_trees <= 0:
        failures.append(
            _failure("config.experiment.n_trees", "n_trees must be a positive integer")
        )
        n_trees = 1
    max_depth = experiment.get("max_depth")
    if not _is_int(max_depth) or max_depth <= 0:
        failures.append(
            _failure("config.experiment.max_depth", "max_depth must be a positive integer")
        )
        max_depth = 1
    learning_rate = experiment.get("learning_rate")
    if not _is_number(learning_rate) or float(learning_rate) < 0.0:
        failures.append(
            _failure(
                "config.experiment.learning_rate",
                "learning_rate must be a non-negative finite number",
            )
        )
        learning_rate = 0.0
    row_subsample = experiment.get("row_subsample")
    if not _is_number(row_subsample) or not 0.0 < float(row_subsample) <= 1.0:
        failures.append(
            _failure(
                "config.experiment.row_subsample",
                "row_subsample must be in (0, 1]",
            )
        )
        row_subsample = 1.0
    col_subsample = experiment.get("col_subsample")
    if not _is_number(col_subsample) or not 0.0 < float(col_subsample) <= 1.0:
        failures.append(
            _failure(
                "config.experiment.col_subsample",
                "col_subsample must be in (0, 1]",
            )
        )
        col_subsample = 1.0
    min_samples_leaf = experiment.get("min_samples_leaf")
    if not _is_int(min_samples_leaf) or min_samples_leaf <= 0:
        failures.append(
            _failure(
                "config.experiment.min_samples_leaf",
                "min_samples_leaf must be a positive integer",
            )
        )
        min_samples_leaf = 1
    n_bins = experiment.get("n_bins")
    if not _is_int(n_bins) or n_bins < 2:
        failures.append(
            _failure("config.experiment.n_bins", "n_bins must be an integer >= 2")
        )
        n_bins = 2
    precision = experiment.get("precision")
    if precision not in SUPPORTED_PRECISION_TIERS:
        failures.append(
            _failure(
                "config.experiment.precision",
                "precision tier is not supported",
                supported=list(SUPPORTED_PRECISION_TIERS),
            )
        )
        precision = "float32"
    tolerance = experiment.get("tolerance", 1e-6)
    if not _is_number(tolerance) or float(tolerance) < 0.0:
        failures.append(
            _failure(
                "config.experiment.tolerance",
                "tolerance must be a non-negative finite number",
            )
        )
        tolerance = 1e-6

    runtime = raw_config.get("runtime", {})
    if not isinstance(runtime, dict):
        failures.append(_failure("config.runtime", "runtime must be a mapping"))
        runtime = {}
    else:
        _reject_unknown_keys(
            runtime,
            {"n_jobs", "snapshot_interval"},
            "config.runtime",
            failures,
        )
    n_jobs = runtime.get("n_jobs", 1)
    if not _is_int(n_jobs) or n_jobs <= 0:
        failures.append(
            _failure("config.runtime.n_jobs", "n_jobs must be a positive integer")
        )
        n_jobs = 1
    snapshot_interval = runtime.get("snapshot_interval", 1)
    if not _is_int(snapshot_interval) or snapshot_interval <= 0:
        failures.append(
            _failure(
                "config.runtime.snapshot_interval",
                "snapshot_interval must be a positive integer",
            )
        )
        snapshot_interval = 1

    diagnostics = raw_config.get("diagnostics", {})
    if not isinstance(diagnostics, dict):
        failures.append(_failure("config.diagnostics", "diagnostics must be a mapping"))
        diagnostics = {}
    else:
        _reject_unknown_keys(
            diagnostics,
            {"decision_threshold", "include_baseline", "fault_tree_indices"},
            "config.diagnostics",
            failures,
        )
    decision_threshold = diagnostics.get("decision_threshold", 0.5)
    if (
        not _is_number(decision_threshold)
        or not 0.0 < float(decision_threshold) < 1.0
    ):
        failures.append(
            _failure(
                "config.diagnostics.decision_threshold",
                "decision_threshold must be in (0, 1)",
            )
        )
        decision_threshold = 0.5
    include_baseline = diagnostics.get("include_baseline", True)
    if not isinstance(include_baseline, bool):
        failures.append(
            _failure("config.diagnostics.include_baseline", "include_baseline must be boolean")
        )
        include_baseline = False
    fault_values = diagnostics.get("fault_tree_indices", [])
    fault_indices: list[int] = []
    if not isinstance(fault_values, list) or any(
        not _is_int(item) or item < 0 for item in fault_values
    ):
        failures.append(
            _failure(
                "config.diagnostics.fault_tree_indices",
                "fault_tree_indices must be a list of non-negative integers",
            )
        )
    else:
        fault_indices = sorted(set(int(item) for item in fault_values))

    if failures:
        raise ConfigValidationError(failures)

    config = ExperimentConfig(
        seed=int(seed),
        n_trees=int(n_trees),
        max_depth=int(max_depth),
        learning_rate=float(learning_rate),
        row_subsample=float(row_subsample),
        col_subsample=float(col_subsample),
        min_samples_leaf=int(min_samples_leaf),
        n_bins=int(n_bins),
        precision=str(precision),
        tolerance=float(tolerance),
        n_jobs=int(n_jobs),
        snapshot_interval=int(snapshot_interval),
        decision_threshold=float(decision_threshold),
        include_baseline=bool(include_baseline),
        fault_tree_indices=tuple(fault_indices),
        config_path=path,
        data_spec_path=data_spec_path,
    )
    fingerprint = digest_json(
        {
            "config": config.normalized(),
            "data_spec": data_spec_raw,
        }
    )
    return LoadedConfig(
        config=config,
        raw_config=raw_config,
        data_spec=data_spec_raw,
        config_fingerprint=fingerprint,
    )
