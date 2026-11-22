"""Configuration defaults, loading and strict business validation."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigInvalid

DEFAULT_CONFIG: dict[str, Any] = {
    "name": "precision_propagation_demo",
    "seed": 20261123,
    "series": {
        "length": 320,
        "target": {
            "trend": 0.035,
            "cycles": [
                {"amplitude": 1.5, "period": 24.0, "phase": 0.2},
                {"amplitude": 0.45, "period": 7.0, "phase": 1.1},
            ],
            "noise_std": 0.08,
            "event_effects": {"promo": 0.9},
        },
        "exogenous": [
            {
                "name": "load",
                "seed": 41001,
                "trend": 0.002,
                "amplitude": 0.8,
                "period": 18.0,
                "phase": 0.4,
                "noise_std": 0.05,
            },
            {
                "name": "temperature",
                "seed": 41002,
                "trend": -0.001,
                "amplitude": 0.6,
                "period": 31.0,
                "phase": 2.0,
                "noise_std": 0.04,
            },
        ],
        "events": [
            {
                "name": "promo",
                "seed": 42001,
                "probability": 0.08,
            }
        ],
    },
    "window": {
        "length": 16,
        "horizon": 4,
        "train_fraction": 0.72,
    },
    "features": {
        "lags": [1, 2, 3, 4],
        "rolling": [
            {"stat": "mean", "window": 3},
            {"stat": "std", "window": 4},
        ],
        "differences": [1],
        "include_exogenous": True,
        "include_events": True,
        "standardize": True,
    },
    "model": {
        "family": "linear_autoregressive",
        "epochs": 10,
        "learning_rate": 0.03,
        "l2": 0.0005,
        "confidence_z": 1.96,
        "stump_rounds": 12,
        "stump_learning_rate": 0.12,
        "stump_min_leaf": 4,
    },
    "training": {
        "chunk_size": 8,
        "compensated_sum": True,
        "subnormal_guard": True,
        "reduction_order": "window_then_anchor",
    },
    "precision": {
        "profile": "float64",
        "fixed_scale": 1000000,
        "rounding": "nearest_even",
    },
    "evaluation": {
        "threshold": 0.6,
        "tolerance": 0.02,
        "threshold_strategy": "tolerance_band",
        "metrics": ["mae", "rmse", "mape", "smape", "max_abs_error"],
        "confidence_z": 1.96,
        "metric_epsilon": 1e-12,
    },
    "snapshot": {
        "enabled": True,
        "directory": "reports/snapshots",
        "names": ["window_stats", "precision_config", "error_spectrum"],
        "version_mode": "auto",
        "versions": {},
    },
}

_PROFILES = {"float64", "float32", "fixed_point"}
_ROUNDING_MODES = {"nearest_even", "nearest_away", "toward_zero", "floor", "ceil"}
_MODEL_FAMILIES = {"linear_autoregressive", "stump_gradient_boosting"}
_THRESHOLD_STRATEGIES = {"strict", "tolerance_band"}
_ROLLING_STATS = {"mean", "std", "min", "max"}
_METRICS = {"mae", "rmse", "mape", "smape", "max_abs_error", "threshold_pass_rate"}
_SNAPSHOT_NAMES = {"window_stats", "precision_config", "error_spectrum"}


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in override.items():
        if (
            isinstance(value, Mapping)
            and isinstance(result.get(key), Mapping)
            and not isinstance(value, list)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _require_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigInvalid(field, f"{field} must be an object")
    return value


def _require_int(value: Any, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigInvalid(field, f"{field} must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigInvalid(field, f"{field} must be >= {minimum}")
    return int(value)


def _require_number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    strictly_positive: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigInvalid(field, f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigInvalid(field, f"{field} must be finite")
    if strictly_positive and result <= 0.0:
        raise ConfigInvalid(field, f"{field} must be > 0")
    if minimum is not None and result < minimum:
        raise ConfigInvalid(field, f"{field} must be >= {minimum}")
    return result


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigInvalid(field, f"{field} must be a boolean")
    return value


def _require_choice(value: Any, field: str, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        allowed = ", ".join(sorted(choices))
        raise ConfigInvalid(field, f"{field} must be one of: {allowed}")
    return value


def _validate_unique_names(names: list[str], field: str) -> None:
    if len(names) != len(set(names)):
        raise ConfigInvalid(field, f"{field} contains duplicate names")


def normalize_config(raw: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Merge user config with defaults and validate every business constraint."""

    if raw is None:
        raw = {}
    _require_mapping(raw, "$")
    config = _deep_merge(DEFAULT_CONFIG, raw)

    if not isinstance(config.get("name"), str) or not config["name"]:
        raise ConfigInvalid("name", "name must be a non-empty string")
    seed = _require_int(config.get("seed"), "seed", minimum=0)
    config["seed"] = seed

    series = _require_mapping(config.get("series"), "series")
    series["length"] = _require_int(series.get("length"), "series.length", minimum=8)
    target = _require_mapping(series.get("target"), "series.target")
    target["trend"] = _require_number(target.get("trend"), "series.target.trend")
    target["noise_std"] = _require_number(
        target.get("noise_std"),
        "series.target.noise_std",
        minimum=0.0,
    )
    cycles = target.get("cycles")
    if not isinstance(cycles, list):
        raise ConfigInvalid("series.target.cycles", "cycles must be an array")
    for index, cycle in enumerate(cycles):
        cycle = _require_mapping(cycle, f"series.target.cycles[{index}]")
        cycle["amplitude"] = _require_number(
            cycle.get("amplitude"),
            f"series.target.cycles[{index}].amplitude",
        )
        cycle["period"] = _require_number(
            cycle.get("period"),
            f"series.target.cycles[{index}].period",
            strictly_positive=True,
        )
        cycle["phase"] = _require_number(
            cycle.get("phase"),
            f"series.target.cycles[{index}].phase",
        )
    event_effects = _require_mapping(
        target.get("event_effects", {}),
        "series.target.event_effects",
    )
    for name, weight in event_effects.items():
        _require_number(weight, f"series.target.event_effects.{name}")

    exogenous = series.get("exogenous")
    if not isinstance(exogenous, list):
        raise ConfigInvalid("series.exogenous", "exogenous must be an array")
    exogenous_names: list[str] = []
    for index, item in enumerate(exogenous):
        item = _require_mapping(item, f"series.exogenous[{index}]")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigInvalid(
                f"series.exogenous[{index}].name",
                "exogenous name must be a non-empty string",
            )
        exogenous_names.append(name)
        item["seed"] = _require_int(
            item.get("seed"),
            f"series.exogenous[{index}].seed",
            minimum=0,
        )
        item["trend"] = _require_number(
            item.get("trend"),
            f"series.exogenous[{index}].trend",
        )
        item["amplitude"] = _require_number(
            item.get("amplitude"),
            f"series.exogenous[{index}].amplitude",
        )
        item["period"] = _require_number(
            item.get("period"),
            f"series.exogenous[{index}].period",
            strictly_positive=True,
        )
        item["phase"] = _require_number(
            item.get("phase"),
            f"series.exogenous[{index}].phase",
        )
        item["noise_std"] = _require_number(
            item.get("noise_std"),
            f"series.exogenous[{index}].noise_std",
            minimum=0.0,
        )
    _validate_unique_names(exogenous_names, "series.exogenous.name")

    events = series.get("events")
    if not isinstance(events, list):
        raise ConfigInvalid("series.events", "events must be an array")
    event_names: list[str] = []
    for index, item in enumerate(events):
        item = _require_mapping(item, f"series.events[{index}]")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ConfigInvalid(
                f"series.events[{index}].name",
                "event name must be a non-empty string",
            )
        event_names.append(name)
        item["seed"] = _require_int(
            item.get("seed"),
            f"series.events[{index}].seed",
            minimum=0,
        )
        probability = _require_number(
            item.get("probability"),
            f"series.events[{index}].probability",
            minimum=0.0,
        )
        if probability > 1.0:
            raise ConfigInvalid(
                f"series.events[{index}].probability",
                "event probability must be <= 1",
            )
        item["probability"] = probability
    _validate_unique_names(event_names, "series.events.name")
    stripped_names = {name.strip().lower() for name in exogenous_names + event_names}
    if len(stripped_names) != len(exogenous_names) + len(event_names):
        raise ConfigInvalid(
            "series",
            "exogenous and event names must not collide after trimming",
        )

    window = _require_mapping(config.get("window"), "window")
    window["length"] = _require_int(window.get("length"), "window.length", minimum=2)
    window["horizon"] = _require_int(window.get("horizon"), "window.horizon", minimum=1)
    if "split_index" in window:
        window["split_index"] = _require_int(
            window.get("split_index"),
            "window.split_index",
            minimum=0,
        )
    else:
        window["train_fraction"] = _require_number(
            window.get("train_fraction"),
            "window.train_fraction",
            minimum=0.0,
        )
        if not 0.0 < window["train_fraction"] < 1.0:
            raise ConfigInvalid(
                "window.train_fraction",
                "window.train_fraction must be strictly between 0 and 1",
            )

    features = _require_mapping(config.get("features"), "features")
    lags = features.get("lags")
    if not isinstance(lags, list):
        raise ConfigInvalid("features.lags", "features.lags must be an array")
    normalized_lags = [
        _require_int(value, f"features.lags[{index}]", minimum=1)
        for index, value in enumerate(lags)
    ]
    if len(normalized_lags) != len(set(normalized_lags)):
        raise ConfigInvalid("features.lags", "features.lags contains duplicates")
    features["lags"] = normalized_lags

    rolling = features.get("rolling")
    if not isinstance(rolling, list):
        raise ConfigInvalid("features.rolling", "features.rolling must be an array")
    rolling_keys: set[tuple[str, int]] = set()
    for index, item in enumerate(rolling):
        item = _require_mapping(item, f"features.rolling[{index}]")
        stat = _require_choice(
            item.get("stat"),
            f"features.rolling[{index}].stat",
            _ROLLING_STATS,
        )
        size = _require_int(
            item.get("window"),
            f"features.rolling[{index}].window",
            minimum=1,
        )
        key = (stat, size)
        if key in rolling_keys:
            raise ConfigInvalid(
                f"features.rolling[{index}]",
                "duplicate rolling statistic",
            )
        rolling_keys.add(key)
        item["stat"] = stat
        item["window"] = size

    differences = features.get("differences")
    if not isinstance(differences, list):
        raise ConfigInvalid(
            "features.differences",
            "features.differences must be an array",
        )
    normalized_diffs = [
        _require_int(value, f"features.differences[{index}]", minimum=1)
        for index, value in enumerate(differences)
    ]
    if len(normalized_diffs) != len(set(normalized_diffs)):
        raise ConfigInvalid(
            "features.differences",
            "features.differences contains duplicates",
        )
    features["differences"] = normalized_diffs
    features["include_exogenous"] = _require_bool(
        features.get("include_exogenous"),
        "features.include_exogenous",
    )
    features["include_events"] = _require_bool(
        features.get("include_events"),
        "features.include_events",
    )
    features["standardize"] = _require_bool(
        features.get("standardize"),
        "features.standardize",
    )

    lookback_candidates = [lag - 1 for lag in normalized_lags]
    lookback_candidates.extend(item["window"] - 1 for item in rolling)
    lookback_candidates.extend(normalized_diffs)
    lookback = max(lookback_candidates, default=0)
    if window["length"] <= lookback:
        raise ConfigInvalid(
            "features",
            "window.length must be greater than the maximum feature lookback",
        )
    if features["include_exogenous"] and not exogenous:
        raise ConfigInvalid(
            "features.include_exogenous",
            "include_exogenous requires at least one exogenous series",
        )
    if features["include_events"] and not events:
        raise ConfigInvalid(
            "features.include_events",
            "include_events requires at least one event series",
        )

    model = _require_mapping(config.get("model"), "model")
    model["family"] = _require_choice(
        model.get("family"),
        "model.family",
        _MODEL_FAMILIES,
    )
    model["epochs"] = _require_int(model.get("epochs"), "model.epochs", minimum=1)
    model["learning_rate"] = _require_number(
        model.get("learning_rate"),
        "model.learning_rate",
        strictly_positive=True,
    )
    model["l2"] = _require_number(model.get("l2"), "model.l2", minimum=0.0)
    model["confidence_z"] = _require_number(
        model.get("confidence_z"),
        "model.confidence_z",
        strictly_positive=True,
    )
    model["stump_rounds"] = _require_int(
        model.get("stump_rounds"),
        "model.stump_rounds",
        minimum=1,
    )
    model["stump_learning_rate"] = _require_number(
        model.get("stump_learning_rate"),
        "model.stump_learning_rate",
        strictly_positive=True,
    )
    model["stump_min_leaf"] = _require_int(
        model.get("stump_min_leaf"),
        "model.stump_min_leaf",
        minimum=1,
    )

    training = _require_mapping(config.get("training"), "training")
    training["chunk_size"] = _require_int(
        training.get("chunk_size"),
        "training.chunk_size",
        minimum=1,
    )
    training["compensated_sum"] = _require_bool(
        training.get("compensated_sum"),
        "training.compensated_sum",
    )
    training["subnormal_guard"] = _require_bool(
        training.get("subnormal_guard"),
        "training.subnormal_guard",
    )
    training["reduction_order"] = _require_choice(
        training.get("reduction_order"),
        "training.reduction_order",
        {"window_then_anchor"},
    )

    precision = _require_mapping(config.get("precision"), "precision")
    precision["profile"] = _require_choice(
        precision.get("profile"),
        "precision.profile",
        _PROFILES,
    )
    precision["fixed_scale"] = _require_int(
        precision.get("fixed_scale"),
        "precision.fixed_scale",
        minimum=1,
    )
    precision["rounding"] = _require_choice(
        precision.get("rounding"),
        "precision.rounding",
        _ROUNDING_MODES,
    )

    evaluation = _require_mapping(config.get("evaluation"), "evaluation")
    evaluation["threshold"] = _require_number(
        evaluation.get("threshold"),
        "evaluation.threshold",
        minimum=0.0,
    )
    evaluation["tolerance"] = _require_number(
        evaluation.get("tolerance"),
        "evaluation.tolerance",
        minimum=0.0,
    )
    evaluation["threshold_strategy"] = _require_choice(
        evaluation.get("threshold_strategy"),
        "evaluation.threshold_strategy",
        _THRESHOLD_STRATEGIES,
    )
    metrics = evaluation.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        raise ConfigInvalid(
            "evaluation.metrics",
            "evaluation.metrics must be a non-empty array",
        )
    for index, metric in enumerate(metrics):
        _require_choice(metric, f"evaluation.metrics[{index}]", _METRICS)
    evaluation["metrics"] = [str(metric) for metric in metrics]
    evaluation["confidence_z"] = _require_number(
        evaluation.get("confidence_z"),
        "evaluation.confidence_z",
        strictly_positive=True,
    )
    evaluation["metric_epsilon"] = _require_number(
        evaluation.get("metric_epsilon"),
        "evaluation.metric_epsilon",
        strictly_positive=True,
    )

    snapshot = _require_mapping(config.get("snapshot"), "snapshot")
    snapshot["enabled"] = _require_bool(snapshot.get("enabled"), "snapshot.enabled")
    if not isinstance(snapshot.get("directory"), str) or not snapshot["directory"]:
        raise ConfigInvalid(
            "snapshot.directory",
            "snapshot.directory must be a non-empty string",
        )
    names = snapshot.get("names")
    if not isinstance(names, list) or not names:
        raise ConfigInvalid("snapshot.names", "snapshot.names must be a non-empty array")
    for index, name in enumerate(names):
        _require_choice(name, f"snapshot.names[{index}]", _SNAPSHOT_NAMES)
    if len(names) != len(set(names)):
        raise ConfigInvalid("snapshot.names", "snapshot.names contains duplicates")
    snapshot["names"] = [str(name) for name in names]
    snapshot["version_mode"] = _require_choice(
        snapshot.get("version_mode"),
        "snapshot.version_mode",
        {"auto", "explicit"},
    )
    versions = snapshot.get("versions", {})
    if not isinstance(versions, Mapping):
        raise ConfigInvalid("snapshot.versions", "snapshot.versions must be an object")
    normalized_versions: dict[str, int] = {}
    for name, version in versions.items():
        normalized_versions[str(name)] = _require_int(
            version,
            f"snapshot.versions.{name}",
            minimum=1,
        )
    snapshot["versions"] = normalized_versions

    length = series["length"]
    horizon = window["horizon"]
    window_length = window["length"]
    if length < window_length + horizon + 2:
        raise ConfigInvalid(
            "series.length",
            "series.length is too short for window length and horizon",
        )
    split_index = window.get(
        "split_index",
        int(round(length * float(window["train_fraction"]))),
    )
    if not 0 < split_index < length:
        raise ConfigInvalid("window.split_index", "split_index must lie inside series")
    window["split_index"] = split_index
    has_train = any(
        start + window_length + horizon <= split_index
        for start in range(length - window_length - horizon + 1)
    )
    has_test = any(
        start >= split_index
        for start in range(length - window_length - horizon + 1)
    )
    if not has_train:
        raise ConfigInvalid(
            "window.split_index",
            "split_index leaves no complete training window",
        )
    if not has_test:
        raise ConfigInvalid(
            "window.split_index",
            "split_index leaves no complete test window",
        )

    config["window"] = window
    config["features"] = features
    config["model"] = model
    config["training"] = training
    config["precision"] = precision
    config["evaluation"] = evaluation
    config["snapshot"] = snapshot
    config["_feature_lookback"] = lookback
    return config


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigInvalid("config", f"config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigInvalid(
            "config",
            f"config is not valid JSON: {exc.msg} at line {exc.lineno}",
        ) from exc
    return normalize_config(raw)
