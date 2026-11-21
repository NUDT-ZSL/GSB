"""Training configuration parsing and semantic normalization."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_CONFIG, EngineError
from .util import read_json_file, resolve_path, sha256_value


_SCHEMA_VERSION = 1
_STANDARDIZATIONS = {"zscore", "minmax"}
_PRECISIONS = {"float64", "float32", "fixed_point"}
_ROUNDINGS = {"nearest_even", "nearest_away", "truncate"}
_REGULARIZATIONS = {"l2", "elastic_net"}
_REDUCTION_ORDER = "block_index_ascending"


def _fail(message: str, *, path: str | None = None) -> EngineError:
    details = {"config_path": path} if path else None
    return EngineError(E_CONFIG, message, stage="config", details=details)


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(f"{name} must be an object", path=name)
    return value


def _integer(
    value: Any,
    name: str,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _fail(f"{name} must be an integer", path=name)
    if minimum is not None and value < minimum:
        raise _fail(f"{name} must be >= {minimum}", path=name)
    if maximum is not None and value > maximum:
        raise _fail(f"{name} must be <= {maximum}", path=name)
    return value


def _number(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    strict_minimum: bool = False,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _fail(f"{name} must be numeric", path=name)
    number = float(value)
    if not math.isfinite(number):
        raise _fail(f"{name} must be finite", path=name)
    if minimum is not None:
        if strict_minimum and number <= minimum:
            raise _fail(f"{name} must be > {minimum}", path=name)
        if not strict_minimum and number < minimum:
            raise _fail(f"{name} must be >= {minimum}", path=name)
    if maximum is not None and number > maximum:
        raise _fail(f"{name} must be <= {maximum}", path=name)
    return number


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise _fail(f"{name} must be a boolean", path=name)
    return value


def _string(value: Any, name: str, *, choices: set[str] | None = None) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(f"{name} must be a non-empty string", path=name)
    if choices is not None and value not in choices:
        raise _fail(f"{name} must be one of {sorted(choices)}, got {value!r}", path=name)
    return value


def _string_list(value: Any, name: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise _fail(f"{name} must be a non-empty array", path=name)
    result: list[str] = []
    for item in value:
        result.append(_string(item, name))
    if len(set(result)) != len(result):
        raise _fail(f"{name} contains duplicate columns", path=name)
    return tuple(result)


@dataclass(frozen=True)
class DataConfig:
    path: Path
    row_id_column: str
    label_column: str
    numeric_columns: tuple[str, ...]
    categorical_columns: tuple[str, ...]
    positive_label: Any
    negative_label: Any
    category_encoding: str


@dataclass(frozen=True)
class ModelConfig:
    loss: str
    regularization: str
    l1_ratio: float
    strengths_ascending: tuple[float, ...]


@dataclass(frozen=True)
class SolverConfig:
    max_iterations: int
    tolerance: float
    iterations_per_strength: tuple[int, ...] | None
    tolerance_per_strength: tuple[float, ...] | None
    lipschitz_safety: float

    def iterations_for(self, ascending_index: int) -> int:
        if self.iterations_per_strength is None:
            return self.max_iterations
        return self.iterations_per_strength[ascending_index]

    def tolerance_for(self, ascending_index: int) -> float:
        if self.tolerance_per_strength is None:
            return self.tolerance
        return self.tolerance_per_strength[ascending_index]


@dataclass(frozen=True)
class PrecisionConfig:
    mode: str
    fixed_scale: int
    fixed_rounding: str

    @property
    def bytes_per_value(self) -> int:
        return {"float64": 8, "float32": 4, "fixed_point": 8}[self.mode]


@dataclass(frozen=True)
class ExecutionConfig:
    block_size: int
    workers: int
    min_block_size: int
    reduction_order: str
    memory_budget_bytes: int
    comparison_budget: int
    max_path_size: int
    max_iterations_per_strength: int
    allow_small_block_downgrade: bool
    allow_worker_downgrade: bool


@dataclass(frozen=True)
class SnapshotConfig:
    directory: Path
    enabled: bool
    resume: bool


@dataclass(frozen=True)
class ValidationConfig:
    monotonicity_abs_tolerance: float
    monotonicity_rel_tolerance: float
    objective_abs_tolerance: float
    objective_rel_tolerance: float


@dataclass(frozen=True)
class RunConfig:
    schema_version: int
    run_id: str
    source_path: Path
    base_dir: Path
    data: DataConfig
    standardization: str
    model: ModelConfig
    solver: SolverConfig
    precision: PrecisionConfig
    execution: ExecutionConfig
    snapshots: SnapshotConfig
    output_directory: Path
    validation: ValidationConfig
    testing: dict[str, Any]

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "data": {
                "row_id_column": self.data.row_id_column,
                "label_column": self.data.label_column,
                "numeric_columns": list(self.data.numeric_columns),
                "categorical_columns": list(self.data.categorical_columns),
                "positive_label": self.data.positive_label,
                "negative_label": self.data.negative_label,
                "category_encoding": self.data.category_encoding,
            },
            "standardization": self.standardization,
            "model": {
                "loss": self.model.loss,
                "regularization": self.model.regularization,
                "l1_ratio": self.model.l1_ratio,
                "strengths_ascending": list(self.model.strengths_ascending),
            },
            "solver": {
                "max_iterations": self.solver.max_iterations,
                "tolerance": self.solver.tolerance,
                "iterations_per_strength": (
                    list(self.solver.iterations_per_strength)
                    if self.solver.iterations_per_strength is not None
                    else None
                ),
                "tolerance_per_strength": (
                    list(self.solver.tolerance_per_strength)
                    if self.solver.tolerance_per_strength is not None
                    else None
                ),
                "lipschitz_safety": self.solver.lipschitz_safety,
            },
            "precision": {
                "mode": self.precision.mode,
                "fixed_scale": self.precision.fixed_scale,
                "fixed_rounding": self.precision.fixed_rounding,
            },
            "execution": {
                "block_size": self.execution.block_size,
                "workers": self.execution.workers,
                "min_block_size": self.execution.min_block_size,
                "reduction_order": self.execution.reduction_order,
                "memory_budget_bytes": self.execution.memory_budget_bytes,
                "comparison_budget": self.execution.comparison_budget,
                "max_path_size": self.execution.max_path_size,
                "max_iterations_per_strength": self.execution.max_iterations_per_strength,
                "allow_small_block_downgrade": self.execution.allow_small_block_downgrade,
                "allow_worker_downgrade": self.execution.allow_worker_downgrade,
            },
            "validation": {
                "monotonicity_abs_tolerance": self.validation.monotonicity_abs_tolerance,
                "monotonicity_rel_tolerance": self.validation.monotonicity_rel_tolerance,
                "objective_abs_tolerance": self.validation.objective_abs_tolerance,
                "objective_rel_tolerance": self.validation.objective_rel_tolerance,
            },
        }

    @property
    def fingerprint(self) -> str:
        return sha256_value(self.semantic_dict())


def load_config(path: str | Path) -> RunConfig:
    source_path = Path(path).expanduser().resolve()
    raw = read_json_file(source_path, stage="config")
    if not isinstance(raw, dict):
        raise _fail("top-level config must be an object")
    base_dir = source_path.parent
    schema_version = _integer(raw.get("schema_version", 1), "schema_version", minimum=1)
    if schema_version != _SCHEMA_VERSION:
        raise _fail(f"unsupported schema_version {schema_version}")
    run_id = _string(raw.get("run_id", source_path.stem), "run_id")

    data_raw = _mapping(raw.get("data"), "data")
    data_path_value = _string(data_raw.get("path"), "data.path")
    row_id_column = _string(data_raw.get("row_id_column", "row_id"), "data.row_id_column")
    label_column = _string(data_raw.get("label_column", "label"), "data.label_column")
    numeric_columns = _string_list(
        data_raw.get("numeric_columns"), "data.numeric_columns", allow_empty=True
    )
    categorical_columns = _string_list(
        data_raw.get("categorical_columns", []),
        "data.categorical_columns",
        allow_empty=True,
    )
    if not numeric_columns and not categorical_columns:
        raise _fail("at least one numeric or categorical feature is required", path="data")
    overlap = set(numeric_columns) & set(categorical_columns)
    if overlap:
        raise _fail(f"feature columns overlap: {sorted(overlap)}", path="data")
    reserved = {row_id_column, label_column}
    if reserved & (set(numeric_columns) | set(categorical_columns)):
        raise _fail("row_id or label column is also listed as a feature", path="data")
    positive_label = data_raw.get("positive_label", 1)
    negative_label = data_raw.get("negative_label", 0)
    if positive_label == negative_label:
        raise _fail("positive_label and negative_label must differ", path="data")
    category_encoding = _string(
        data_raw.get("category_encoding", "one_hot_sorted"),
        "data.category_encoding",
        choices={"one_hot_sorted"},
    )

    preprocessing_raw = _mapping(raw.get("preprocessing", {}), "preprocessing")
    standardization = _string(
        preprocessing_raw.get("standardization", "zscore"),
        "preprocessing.standardization",
        choices=_STANDARDIZATIONS,
    )

    model_raw = _mapping(raw.get("model"), "model")
    loss = _string(model_raw.get("loss", "squared_error"), "model.loss")
    if loss != "squared_error":
        raise _fail("model.loss currently supports only 'squared_error'", path="model.loss")
    regularization_raw = _mapping(model_raw.get("regularization"), "model.regularization")
    regularization = _string(
        regularization_raw.get("type"),
        "model.regularization.type",
        choices=_REGULARIZATIONS,
    )
    if regularization == "l2":
        l1_ratio = 0.0
        supplied_ratio = regularization_raw.get("l1_ratio")
        if supplied_ratio is not None and _number(
            supplied_ratio, "model.regularization.l1_ratio"
        ) != 0.0:
            raise _fail("L2 regularization requires l1_ratio=0", path="model.regularization")
    else:
        l1_ratio = _number(
            regularization_raw.get("l1_ratio"),
            "model.regularization.l1_ratio",
            minimum=0.0,
            maximum=1.0,
            strict_minimum=True,
        )
    strengths_raw = model_raw.get(
        "strengths", model_raw.get("regularization_strengths")
    )
    if not isinstance(strengths_raw, list) or not strengths_raw:
        raise _fail("model.strengths must be a non-empty array", path="model.strengths")
    strengths = [
        _number(item, "model.strengths[]", minimum=0.0, strict_minimum=True)
        for item in strengths_raw
    ]
    if len(set(strengths)) != len(strengths):
        raise _fail("model.strengths contains duplicates", path="model.strengths")
    if strengths != sorted(strengths):
        raise _fail(
            "model.strengths must be provided in strictly ascending order",
            path="model.strengths",
        )
    strengths_tuple = tuple(strengths)

    solver_raw = _mapping(raw.get("solver", {}), "solver")
    max_iterations = _integer(
        solver_raw.get("max_iterations", 500), "solver.max_iterations", minimum=1
    )
    tolerance = _number(
        solver_raw.get("tolerance", 1.0e-7),
        "solver.tolerance",
        minimum=0.0,
        strict_minimum=True,
    )
    iterations_per_strength = solver_raw.get("iterations_per_strength")
    if iterations_per_strength is not None:
        if not isinstance(iterations_per_strength, list) or len(
            iterations_per_strength
        ) != len(strengths_tuple):
            raise _fail(
                "solver.iterations_per_strength must align with model.strengths",
                path="solver.iterations_per_strength",
            )
        iterations_per_strength = tuple(
            _integer(item, "solver.iterations_per_strength[]", minimum=1)
            for item in iterations_per_strength
        )
    tolerance_per_strength = solver_raw.get("tolerance_per_strength")
    if tolerance_per_strength is not None:
        if not isinstance(tolerance_per_strength, list) or len(
            tolerance_per_strength
        ) != len(strengths_tuple):
            raise _fail(
                "solver.tolerance_per_strength must align with model.strengths",
                path="solver.tolerance_per_strength",
            )
        tolerance_per_strength = tuple(
            _number(
                item,
                "solver.tolerance_per_strength[]",
                minimum=0.0,
                strict_minimum=True,
            )
            for item in tolerance_per_strength
        )
    lipschitz_safety = _number(
        solver_raw.get("lipschitz_safety", 0.99),
        "solver.lipschitz_safety",
        minimum=0.0,
        maximum=1.0,
        strict_minimum=True,
    )

    precision_raw = _mapping(raw.get("precision"), "precision")
    precision_mode = _string(
        precision_raw.get("mode"), "precision.mode", choices=_PRECISIONS
    )
    fixed_raw = _mapping(precision_raw.get("fixed_point", {}), "precision.fixed_point")
    if precision_mode == "fixed_point":
        fixed_scale = _integer(
            fixed_raw.get("scale"), "precision.fixed_point.scale", minimum=1
        )
        if fixed_scale > 2**30:
            raise _fail(
                "precision.fixed_point.scale must be <= 2**30",
                path="precision.fixed_point.scale",
            )
        fixed_rounding = _string(
            fixed_raw.get("rounding"),
            "precision.fixed_point.rounding",
            choices=_ROUNDINGS,
        )
    else:
        fixed_scale = _integer(
            fixed_raw.get("scale", 1),
            "precision.fixed_point.scale",
            minimum=1,
        )
        fixed_rounding = _string(
            fixed_raw.get("rounding", "nearest_even"),
            "precision.fixed_point.rounding",
            choices=_ROUNDINGS,
        )

    execution_raw = _mapping(raw.get("execution", {}), "execution")
    block_size = _integer(
        execution_raw.get("block_size", 128), "execution.block_size", minimum=1
    )
    workers = _integer(
        execution_raw.get("workers", 4), "execution.workers", minimum=1, maximum=256
    )
    min_block_size = _integer(
        execution_raw.get("min_block_size", min(8, block_size)),
        "execution.min_block_size",
        minimum=1,
    )
    if min_block_size > block_size:
        raise _fail(
            "execution.min_block_size cannot exceed execution.block_size",
            path="execution.min_block_size",
        )
    reduction_order = _string(
        execution_raw.get("reduction_order", _REDUCTION_ORDER),
        "execution.reduction_order",
        choices={_REDUCTION_ORDER},
    )
    memory_budget_bytes = _integer(
        execution_raw.get("memory_budget_bytes", 64 * 1024 * 1024),
        "execution.memory_budget_bytes",
        minimum=1,
    )
    comparison_budget = _integer(
        execution_raw.get("comparison_budget", 100_000_000),
        "execution.comparison_budget",
        minimum=1,
    )
    max_path_size = _integer(
        execution_raw.get("max_path_size", 32),
        "execution.max_path_size",
        minimum=1,
    )
    max_iterations_per_strength = _integer(
        execution_raw.get("max_iterations_per_strength", 5000),
        "execution.max_iterations_per_strength",
        minimum=1,
    )
    allow_small_block_downgrade = _boolean(
        execution_raw.get("allow_small_block_downgrade", True),
        "execution.allow_small_block_downgrade",
    )
    allow_worker_downgrade = _boolean(
        execution_raw.get("allow_worker_downgrade", True),
        "execution.allow_worker_downgrade",
    )

    snapshots_raw = _mapping(raw.get("snapshots", {}), "snapshots")
    snapshot_directory = resolve_path(
        base_dir, _string(snapshots_raw.get("directory", "snapshots"), "snapshots.directory")
    )
    snapshots_enabled = _boolean(
        snapshots_raw.get("enabled", True), "snapshots.enabled"
    )
    snapshots_resume = _boolean(snapshots_raw.get("resume", True), "snapshots.resume")

    output_raw = _mapping(raw.get("output", {}), "output")
    output_directory = resolve_path(
        base_dir, _string(output_raw.get("directory", "run"), "output.directory")
    )

    validation_raw = _mapping(raw.get("validation", {}), "validation")
    validation = ValidationConfig(
        monotonicity_abs_tolerance=_number(
            validation_raw.get("monotonicity_abs_tolerance", 1.0e-12),
            "validation.monotonicity_abs_tolerance",
            minimum=0.0,
        ),
        monotonicity_rel_tolerance=_number(
            validation_raw.get("monotonicity_rel_tolerance", 0.005),
            "validation.monotonicity_rel_tolerance",
            minimum=0.0,
        ),
        objective_abs_tolerance=_number(
            validation_raw.get("objective_abs_tolerance", 1.0e-9),
            "validation.objective_abs_tolerance",
            minimum=0.0,
        ),
        objective_rel_tolerance=_number(
            validation_raw.get("objective_rel_tolerance", 1.0e-8),
            "validation.objective_rel_tolerance",
            minimum=0.0,
        ),
    )
    testing = copy.deepcopy(raw.get("testing", {}))
    if not isinstance(testing, dict):
        raise _fail("testing must be an object", path="testing")

    return RunConfig(
        schema_version=schema_version,
        run_id=run_id,
        source_path=source_path,
        base_dir=base_dir,
        data=DataConfig(
            path=resolve_path(base_dir, data_path_value),
            row_id_column=row_id_column,
            label_column=label_column,
            numeric_columns=numeric_columns,
            categorical_columns=categorical_columns,
            positive_label=positive_label,
            negative_label=negative_label,
            category_encoding=category_encoding,
        ),
        standardization=standardization,
        model=ModelConfig(
            loss=loss,
            regularization=regularization,
            l1_ratio=float(l1_ratio),
            strengths_ascending=strengths_tuple,
        ),
        solver=SolverConfig(
            max_iterations=max_iterations,
            tolerance=tolerance,
            iterations_per_strength=iterations_per_strength,
            tolerance_per_strength=tolerance_per_strength,
            lipschitz_safety=lipschitz_safety,
        ),
        precision=PrecisionConfig(
            mode=precision_mode,
            fixed_scale=fixed_scale,
            fixed_rounding=fixed_rounding,
        ),
        execution=ExecutionConfig(
            block_size=block_size,
            workers=workers,
            min_block_size=min_block_size,
            reduction_order=reduction_order,
            memory_budget_bytes=memory_budget_bytes,
            comparison_budget=comparison_budget,
            max_path_size=max_path_size,
            max_iterations_per_strength=max_iterations_per_strength,
            allow_small_block_downgrade=allow_small_block_downgrade,
            allow_worker_downgrade=allow_worker_downgrade,
        ),
        snapshots=SnapshotConfig(
            directory=snapshot_directory,
            enabled=snapshots_enabled,
            resume=snapshots_resume,
        ),
        output_directory=output_directory,
        validation=validation,
        testing=testing,
    )

