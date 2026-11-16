"""Experiment configuration, immutable plans, and validation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Tuple

from .data import DataDescription, load_data_description
from .errors import config_error
from .utils import (
    fingerprint,
    read_yaml,
    require_bool,
    require_int,
    require_list,
    require_mapping,
    require_number,
    require_str,
)


CATEGORICAL_METHODS = ("one_hot", "target", "frequency")


@dataclass(frozen=True)
class OneHotParams:
    drop_first: bool = False
    missing_label: str = "__MISSING__"

    def to_dict(self) -> dict[str, Any]:
        return {"drop_first": self.drop_first, "missing_label": self.missing_label}


@dataclass(frozen=True)
class TargetEncodingParams:
    inner_folds: int = 5
    smoothing: float = 8.0
    missing_code: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "inner_folds": self.inner_folds,
            "smoothing": self.smoothing,
            "missing_code": self.missing_code,
        }


@dataclass(frozen=True)
class FrequencyParams:
    missing_code: float = -1.0

    def to_dict(self) -> dict[str, Any]:
        return {"missing_code": self.missing_code}


@dataclass(frozen=True)
class WeightEliminationParams:
    enabled: bool = True
    max_eliminations: int = 6
    min_features: int = 3
    ridge_alpha: float = 0.01

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "max_eliminations": self.max_eliminations,
            "min_features": self.min_features,
            "ridge_alpha": self.ridge_alpha,
        }


@dataclass(frozen=True)
class SelectorParams:
    variance_threshold: float = 1e-12
    correlation_threshold: float = 0.98
    weight_elimination: WeightEliminationParams = WeightEliminationParams()

    def to_dict(self) -> dict[str, Any]:
        return {
            "variance_threshold": self.variance_threshold,
            "correlation_threshold": self.correlation_threshold,
            "weight_elimination": self.weight_elimination.to_dict(),
        }


@dataclass(frozen=True)
class PlanConfig:
    encodings: Tuple[Tuple[str, str], ...]
    max_cardinality: int
    cardinality_policy: str
    fallback_method: str
    one_hot: OneHotParams
    target_encoding: TargetEncodingParams
    frequency: FrequencyParams
    selector: SelectorParams

    def encoding_map(self) -> dict[str, str]:
        return dict(self.encodings)

    def method_for(self, column: str) -> str:
        try:
            return dict(self.encodings)[column]
        except KeyError as exc:
            raise config_error("unknown encoding column", column=column) from exc

    def with_method(self, column: str, method: str) -> "PlanConfig":
        mapping = dict(self.encodings)
        if column not in mapping:
            raise config_error("unknown encoding column", column=column)
        mapping[column] = method
        return replace(
            self,
            encodings=tuple((name, mapping[name]) for name, _ in self.encodings),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "encodings": {name: method for name, method in self.encodings},
            "max_cardinality": self.max_cardinality,
            "cardinality_policy": self.cardinality_policy,
            "fallback_method": self.fallback_method,
            "one_hot": self.one_hot.to_dict(),
            "target_encoding": self.target_encoding.to_dict(),
            "frequency": self.frequency.to_dict(),
            "selector": self.selector.to_dict(),
        }

    @property
    def plan_id(self) -> str:
        return "plan-" + fingerprint(self.to_dict())[:16]


@dataclass(frozen=True)
class ExperimentConfig:
    source_path: Path
    data_description_path: Path
    data_description: DataDescription
    target_column: str
    cv_folds: int
    cv_seed: int
    latency_budget_ms: float
    max_workers: int
    task_delay_ms: float
    snapshot_dir: Path
    report_path: Path
    allowed_methods: Tuple[str, ...]
    default_method: str
    initial_plan: PlanConfig
    raw: Mapping[str, Any]

    @property
    def fingerprint(self) -> str:
        return fingerprint(
            {
                "data": self.data_description.fingerprint,
                "target": self.target_column,
                "cv_folds": self.cv_folds,
                "cv_seed": self.cv_seed,
                "plan": self.initial_plan.to_dict(),
            }
        )


def _parse_weight_params(raw: Any) -> WeightEliminationParams:
    mapping = require_mapping(raw or {}, "selector.weight_elimination")
    enabled = require_bool(mapping.get("enabled", True), "weight_elimination.enabled")
    max_eliminations = require_int(
        mapping.get("max_eliminations", 6),
        "weight_elimination.max_eliminations",
        minimum=0,
    )
    min_features = require_int(
        mapping.get("min_features", 3), "weight_elimination.min_features", minimum=1
    )
    ridge_alpha = require_number(
        mapping.get("ridge_alpha", 0.01), "weight_elimination.ridge_alpha"
    )
    if ridge_alpha <= 0:
        raise config_error("weight_elimination.ridge_alpha must be > 0")
    return WeightEliminationParams(
        enabled=enabled,
        max_eliminations=max_eliminations,
        min_features=min_features,
        ridge_alpha=ridge_alpha,
    )


def _parse_selector(raw: Any) -> SelectorParams:
    mapping = require_mapping(raw or {}, "selector")
    variance = require_number(mapping.get("variance_threshold", 1e-12), "selector.variance_threshold")
    correlation = require_number(
        mapping.get("correlation_threshold", 0.98), "selector.correlation_threshold"
    )
    if variance < 0:
        raise config_error("selector.variance_threshold must be >= 0")
    if not 0.0 < correlation <= 1.0:
        raise config_error("selector.correlation_threshold must be in (0, 1]")
    return SelectorParams(
        variance_threshold=variance,
        correlation_threshold=correlation,
        weight_elimination=_parse_weight_params(mapping.get("weight_elimination", {})),
    )


def _parse_plan(data: Any, description: DataDescription, target_column: str) -> PlanConfig:
    mapping = require_mapping(data, "plan")
    raw_encodings = require_mapping(mapping.get("encodings"), "plan.encodings")
    max_cardinality = require_int(
        mapping.get("max_cardinality"), "plan.max_cardinality", minimum=1
    )
    policy = require_str(
        mapping.get("cardinality_policy", "fallback"), "plan.cardinality_policy"
    )
    if policy not in {"fallback", "error"}:
        raise config_error("plan.cardinality_policy must be fallback or error")
    fallback = require_str(
        mapping.get("fallback_method", "frequency"), "plan.fallback_method"
    )
    if fallback not in {"target", "frequency"}:
        raise config_error("plan.fallback_method must be target or frequency")

    one_hot_raw = require_mapping(mapping.get("one_hot", {}), "plan.one_hot")
    one_hot = OneHotParams(
        drop_first=require_bool(one_hot_raw.get("drop_first", False), "one_hot.drop_first"),
        missing_label=require_str(
            one_hot_raw.get("missing_label", "__MISSING__"), "one_hot.missing_label"
        ),
    )
    target_raw = require_mapping(
        mapping.get("target_encoding", {}), "plan.target_encoding"
    )
    target_encoding = TargetEncodingParams(
        inner_folds=require_int(
            target_raw.get("inner_folds", 5),
            "target_encoding.inner_folds",
            minimum=2,
        ),
        smoothing=require_number(
            target_raw.get("smoothing", 8.0), "target_encoding.smoothing"
        ),
        missing_code=require_number(
            target_raw.get("missing_code", 0.0), "target_encoding.missing_code"
        ),
    )
    if target_encoding.smoothing < 0:
        raise config_error("target_encoding.smoothing must be >= 0")
    frequency_raw = require_mapping(mapping.get("frequency", {}), "plan.frequency")
    frequency = FrequencyParams(
        missing_code=require_number(
            frequency_raw.get("missing_code", -1.0), "frequency.missing_code"
        )
    )

    columns = {spec.name: spec for spec in description.columns}
    ordered: list[tuple[str, str]] = []
    for spec in description.columns:
        if spec.name == target_column:
            continue
        try:
            method = raw_encodings[spec.name]
        except KeyError as exc:
            raise config_error("missing encoding for column", column=spec.name) from exc
        method = require_str(method, f"encodings.{spec.name}")
        if spec.kind == "numeric" and method != "numeric":
            raise config_error(
                "numeric column must use numeric encoding",
                column=spec.name,
                method=method,
            )
        if spec.kind == "categorical" and method not in CATEGORICAL_METHODS:
            raise config_error(
                "categorical column has an unsupported encoding",
                column=spec.name,
                method=method,
            )
        if spec.kind == "numeric" and method == "numeric":
            pass
        ordered.append((spec.name, method))
    unknown = sorted(set(raw_encodings) - set(columns))
    if unknown:
        raise config_error("encoding configuration references unknown columns", columns=unknown)
    if target_column in raw_encodings:
        raise config_error("target column cannot be an encoded feature")
    if not ordered:
        raise config_error("at least one feature column is required")
    return PlanConfig(
        encodings=tuple(ordered),
        max_cardinality=max_cardinality,
        cardinality_policy=policy,
        fallback_method=fallback,
        one_hot=one_hot,
        target_encoding=target_encoding,
        frequency=frequency,
        selector=_parse_selector(mapping.get("selector", {})),
    )


def plan_from_dict(data: Mapping[str, Any], description: DataDescription, target_column: str) -> PlanConfig:
    return _parse_plan(data, description, target_column)


def apply_plan_param(plan: PlanConfig, path: str, value: Any) -> PlanConfig:
    """Return a new immutable plan after validating one interactive edit."""

    normalized = path.strip()
    if normalized.startswith("encoding."):
        normalized = normalized[len("encoding.") :]
    if normalized.startswith("target."):
        normalized = "target_encoding." + normalized[len("target.") :]
    allowed_prefixes = (
        "selector.",
        "one_hot.",
        "target_encoding.",
        "frequency.",
    )
    if not normalized.startswith(allowed_prefixes) and normalized not in {
        "max_cardinality",
        "cardinality_policy",
        "fallback_method",
    }:
        raise config_error("unknown parameter path", path=path)

    if normalized == "max_cardinality":
        return replace(plan, max_cardinality=require_int(value, path, minimum=1))
    if normalized == "cardinality_policy":
        policy = require_str(value, path)
        if policy not in {"fallback", "error"}:
            raise config_error("cardinality_policy must be fallback or error")
        return replace(plan, cardinality_policy=policy)
    if normalized == "fallback_method":
        method = require_str(value, path)
        if method not in {"target", "frequency"}:
            raise config_error("fallback_method must be target or frequency")
        return replace(plan, fallback_method=method)
    if normalized.startswith("one_hot."):
        key = normalized[len("one_hot.") :]
        if key == "drop_first":
            return replace(plan, one_hot=replace(plan.one_hot, drop_first=require_bool(value, path)))
        if key == "missing_label":
            return replace(plan, one_hot=replace(plan.one_hot, missing_label=require_str(value, path)))
    elif normalized.startswith("target_encoding."):
        key = normalized[len("target_encoding.") :]
        if key == "inner_folds":
            return replace(
                plan,
                target_encoding=replace(
                    plan.target_encoding,
                    inner_folds=require_int(value, path, minimum=2),
                ),
            )
        if key == "smoothing":
            smoothing = require_number(value, path)
            if smoothing < 0:
                raise config_error("target_encoding.smoothing must be >= 0")
            return replace(
                plan,
                target_encoding=replace(plan.target_encoding, smoothing=smoothing),
            )
        if key == "missing_code":
            return replace(
                plan,
                target_encoding=replace(
                    plan.target_encoding, missing_code=require_number(value, path)
                ),
            )
    elif normalized.startswith("frequency."):
        key = normalized[len("frequency.") :]
        if key == "missing_code":
            return replace(
                plan,
                frequency=replace(plan.frequency, missing_code=require_number(value, path)),
            )
    elif normalized == "selector.variance_threshold":
        variance = require_number(value, path)
        if variance < 0:
            raise config_error("selector.variance_threshold must be >= 0")
        return replace(plan, selector=replace(plan.selector, variance_threshold=variance))
    elif normalized == "selector.correlation_threshold":
        correlation = require_number(value, path)
        if not 0.0 < correlation <= 1.0:
            raise config_error("selector.correlation_threshold must be in (0, 1]")
        return replace(plan, selector=replace(plan.selector, correlation_threshold=correlation))
    elif normalized.startswith("selector.weight_elimination."):
        key = normalized[len("selector.weight_elimination.") :]
        weight = plan.selector.weight_elimination
        if key == "enabled":
            updated = replace(weight, enabled=require_bool(value, path))
        elif key == "max_eliminations":
            updated = replace(
                weight, max_eliminations=require_int(value, path, minimum=0)
            )
        elif key == "min_features":
            updated = replace(weight, min_features=require_int(value, path, minimum=1))
        elif key == "ridge_alpha":
            alpha = require_number(value, path)
            if alpha <= 0:
                raise config_error("weight_elimination.ridge_alpha must be > 0")
            updated = replace(weight, ridge_alpha=alpha)
        else:
            updated = None
        if updated is not None:
            return replace(plan, selector=replace(plan.selector, weight_elimination=updated))
    raise config_error("unknown parameter path", path=path)


def load_experiment_config(path: str | Path) -> ExperimentConfig:
    source_path = Path(path).resolve()
    raw = read_yaml(source_path)
    root = require_mapping(raw, "experiment configuration")
    data_path_raw = root.get("data_description")
    if data_path_raw is None:
        data_path_raw = require_mapping(root.get("data"), "data").get("description")
    data_path = Path(require_str(data_path_raw, "data_description"))
    if not data_path.is_absolute():
        data_path = (source_path.parent / data_path).resolve()
    description = load_data_description(data_path)
    target_column = require_str(root.get("target_column"), "target_column")
    if target_column != description.target.name:
        raise config_error(
            "target_column is missing from the generated dataset",
            target_column=target_column,
            available_target=description.target.name,
        )
    if target_column in {spec.name for spec in description.columns}:
        raise config_error("target column must not also be a feature column")

    encoding = require_mapping(root.get("encoding", {}), "encoding")
    methods_value = require_list(
        encoding.get("methods", list(CATEGORICAL_METHODS)), "encoding.methods"
    )
    methods = tuple(require_str(value, "encoding.methods") for value in methods_value)
    if not methods or len(set(methods)) != len(methods):
        raise config_error("encoding.methods must be a non-empty unique list")
    if any(method not in CATEGORICAL_METHODS for method in methods):
        raise config_error("encoding.methods contains an unsupported encoder")
    default_method = require_str(
        encoding.get("default_method", "frequency"), "encoding.default_method"
    )
    if default_method not in methods:
        raise config_error("encoding.default_method must be one of encoding.methods")

    cv = require_mapping(root.get("cv", {}), "cv")
    cv_folds = require_int(cv.get("folds", 4), "cv.folds", minimum=2)
    cv_seed = require_int(cv.get("seed", description.seed), "cv.seed", minimum=0)
    if description.rows < cv_folds * 2:
        raise config_error("dataset is too small for the requested CV folds")
    latency_budget_ms = require_number(
        root.get("latency_budget_ms"), "latency_budget_ms"
    )
    if latency_budget_ms <= 0:
        raise config_error("latency_budget_ms must be > 0")
    max_workers = require_int(root.get("max_workers", 1), "max_workers", minimum=1)
    scheduler = require_mapping(root.get("scheduler", {}), "scheduler")
    task_delay_ms = require_number(
        scheduler.get("task_delay_ms", 0.0), "scheduler.task_delay_ms"
    )
    if task_delay_ms < 0:
        raise config_error("scheduler.task_delay_ms must be >= 0")

    columns_raw = require_mapping(encoding.get("columns", {}), "encoding.columns")
    known_columns = {spec.name: spec for spec in description.columns}
    unknown_columns = sorted(set(columns_raw) - set(known_columns))
    if unknown_columns:
        raise config_error("unknown columns in encoding.columns", columns=unknown_columns)
    resolved_encodings: dict[str, str] = {}
    for spec in description.columns:
        if spec.name == target_column:
            continue
        if spec.name in columns_raw:
            method = require_str(columns_raw[spec.name], f"encoding.columns.{spec.name}")
        elif spec.kind == "numeric":
            method = "numeric"
        else:
            method = default_method
        if spec.kind == "numeric" and method != "numeric":
            raise config_error(
                "numeric column has an incompatible encoding",
                column=spec.name,
                method=method,
                expected="numeric",
            )
        if spec.kind == "categorical" and method not in methods:
            raise config_error(
                "categorical encoding is not in encoding.methods",
                column=spec.name,
                method=method,
            )
        resolved_encodings[spec.name] = method

    target_raw = require_mapping(encoding.get("target_encoding", {}), "encoding.target_encoding")
    one_hot_raw = require_mapping(encoding.get("one_hot", {}), "encoding.one_hot")
    frequency_raw = require_mapping(encoding.get("frequency", {}), "encoding.frequency")
    plan_mapping = {
        "encodings": resolved_encodings,
        "max_cardinality": encoding.get("max_cardinality", 12),
        "cardinality_policy": encoding.get("cardinality_policy", "fallback"),
        "fallback_method": encoding.get("fallback_method", "frequency"),
        "one_hot": dict(one_hot_raw),
        "target_encoding": dict(target_raw),
        "frequency": dict(frequency_raw),
        "selector": dict(require_mapping(root.get("selector", {}), "selector")),
    }
    plan = _parse_plan(plan_mapping, description, target_column)
    snapshot_dir = Path(
        root.get("snapshot_dir", ".featurelab/snapshots")
    )
    if not snapshot_dir.is_absolute():
        snapshot_dir = (source_path.parent.parent / snapshot_dir).resolve()
    report_path = Path(root.get("report_path", "artifacts/report.json"))
    if not report_path.is_absolute():
        report_path = (source_path.parent.parent / report_path).resolve()
    return ExperimentConfig(
        source_path=source_path,
        data_description_path=data_path,
        data_description=description,
        target_column=target_column,
        cv_folds=cv_folds,
        cv_seed=cv_seed,
        latency_budget_ms=latency_budget_ms,
        max_workers=max_workers,
        task_delay_ms=task_delay_ms,
        snapshot_dir=snapshot_dir,
        report_path=report_path,
        allowed_methods=methods,
        default_method=default_method,
        initial_plan=plan,
        raw=root,
    )
