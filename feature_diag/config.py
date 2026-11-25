from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import EngineError, E_PARAM_INVALID, param_error
from .util import sha256_json


DEFAULT_EPOCH_MS = 1704067200000
DEFAULT_LIMITS = {
    "max_resident_bytes": 64 * 1024 * 1024,
    "max_cache_entries": 20000,
    "max_batch_rows": 8,
    "max_query_workers": 2,
}
DEFAULT_RESOURCE_POLICY = {
    "on_batch_rows": "degrade_serial",
    "on_query_workers": "degrade_serial",
    "on_cache_limit": "error",
    "priority": [
        "encoding_matrix",
        "cache",
        "batch_rows",
        "query_workers",
    ],
}


def _detail(error: EngineError) -> dict[str, Any]:
    return copy.deepcopy(error.detail)


@dataclass
class FeatureConfig:
    schema: str
    snapshot_id: str
    snapshot_version: int
    effective_at_sec: float
    effective_at_ms: int
    epoch_ms: int
    seed: int
    fit_rows: int
    row_id_column: str
    label_column: str
    numeric: list[dict[str, Any]]
    categorical: list[dict[str, Any]]
    cross: list[dict[str, Any]]
    selector: dict[str, Any]
    limits: dict[str, Any]
    resource_policy: dict[str, Any]
    completion_sources: list[dict[str, Any]] = field(default_factory=list)

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed,
            "fit_rows": self.fit_rows,
            "row_id_column": self.row_id_column,
            "label_column": self.label_column,
            "numeric": copy.deepcopy(self.numeric),
            "categorical": copy.deepcopy(self.categorical),
            "cross": copy.deepcopy(self.cross),
            "selector": copy.deepcopy(self.selector),
            "limits": copy.deepcopy(self.limits),
            "resource_policy": copy.deepcopy(self.resource_policy),
        }

    def encoding_dict(self) -> dict[str, Any]:
        data = self.semantic_dict()
        data.pop("selector", None)
        data.pop("limits", None)
        data.pop("resource_policy", None)
        return data

    @property
    def semantic_digest(self) -> str:
        return sha256_json(self.semantic_dict())

    @property
    def encoding_digest(self) -> str:
        return sha256_json(self.encoding_dict())

    def clone(self) -> "FeatureConfig":
        return copy.deepcopy(self)

    def to_v1_dict(self) -> dict[str, Any]:
        return {
            "schema": "feature_config/v1",
            "snapshot_id": self.snapshot_id,
            "snapshot_version": self.snapshot_version,
            "effective_at_sec": round(float(self.effective_at_sec), 9),
            "epoch_ms": self.epoch_ms,
            "seed": self.seed,
            "fit_rows": self.fit_rows,
            "row_id_column": self.row_id_column,
            "label_column": self.label_column,
            "numeric": copy.deepcopy(self.numeric),
            "categorical": copy.deepcopy(self.categorical),
            "cross": copy.deepcopy(self.cross),
            "selector": copy.deepcopy(self.selector),
            "limits": copy.deepcopy(self.limits),
            "resource_policy": copy.deepcopy(self.resource_policy),
        }

    def to_v2_parts(self) -> dict[str, dict[str, Any]]:
        return {
            "base.json": {
                "schema": "feature_config/v2",
                "snapshot_id": self.snapshot_id,
                "snapshot_version": self.snapshot_version,
                "effective_at_ms": self.effective_at_ms,
                "epoch_ms": self.epoch_ms,
                "seed": self.seed,
                "fit_rows": self.fit_rows,
                "row_id_column": self.row_id_column,
                "label_column": self.label_column,
            },
            "numeric.json": {"numeric": copy.deepcopy(self.numeric)},
            "categorical.json": {"categorical": copy.deepcopy(self.categorical)},
            "cross.json": {"cross": copy.deepcopy(self.cross)},
            "selection.json": {"selector": copy.deepcopy(self.selector)},
            "resources.json": {
                "limits": copy.deepcopy(self.limits),
                "resource_policy": copy.deepcopy(self.resource_policy),
            },
        }


def _as_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise param_error("field must be an object", field=path)
    return value


def _as_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise param_error("field must be an array", field=path)
    return value


def _as_int(value: Any, path: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise param_error("field must be an integer", field=path, value=value)
    if minimum is not None and value < minimum:
        raise param_error("field is below minimum", field=path, minimum=minimum, value=value)
    return value


def _as_float(value: Any, path: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise param_error("field must be numeric", field=path, value=value)
    result = float(value)
    if not math.isfinite(result):
        raise param_error("field must be finite", field=path, value=value)
    if minimum is not None and result < minimum:
        raise param_error("field is below minimum", field=path, minimum=minimum, value=value)
    return result


def _as_string(value: Any, path: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise param_error("field must be a string", field=path, value=value)
    if nonempty and not value:
        raise param_error("field must not be empty", field=path)
    return value


def _record_fill(
    fills: list[dict[str, Any]],
    path: str,
    action: str,
    source: str,
    value: Any,
) -> None:
    fills.append(
        {
            "field": path,
            "action": action,
            "source": source,
            "value": copy.deepcopy(value),
        }
    )


def _normalize_numeric(raw: Any, fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = _as_list(raw, "numeric")
    if not items:
        raise param_error("numeric feature list must not be empty", field="numeric")
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(items):
        path = f"numeric[{index}]"
        item = _as_mapping(item, path)
        name = _as_string(item.get("name"), f"{path}.name")
        method = _as_string(item.get("method"), f"{path}.method")
        if method not in {"standardize", "minmax"}:
            raise param_error("unsupported numeric method", field=f"{path}.method", value=method)
        if name in names:
            raise param_error("duplicate numeric feature name", field=f"{path}.name", value=name)
        names.add(name)
        result.append({"name": name, "method": method})
    return result


def _normalize_categorical(raw: Any, fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = _as_list(raw, "categorical")
    names: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        path = f"categorical[{index}]"
        item = _as_mapping(item, path)
        name = _as_string(item.get("name"), f"{path}.name")
        codec = _as_string(item.get("codec"), f"{path}.codec")
        if codec not in {"one_hot", "target"}:
            raise param_error("unsupported categorical codec", field=f"{path}.codec", value=codec)
        if name in names:
            raise param_error("duplicate categorical feature name", field=f"{path}.name", value=name)
        names.add(name)
        categories_raw = _as_list(item.get("categories"), f"{path}.categories")
        if not categories_raw:
            raise param_error("categorical categories must not be empty", field=f"{path}.categories")
        categories: list[str] = []
        for category_index, category in enumerate(categories_raw):
            if isinstance(category, bool) or not isinstance(category, (str, int, float)):
                raise param_error(
                    "category value must be string or number",
                    field=f"{path}.categories[{category_index}]",
                    value=category,
                )
            rendered = str(category)
            if rendered in categories:
                raise param_error(
                    "duplicate category value",
                    field=f"{path}.categories[{category_index}]",
                    value=rendered,
                )
            categories.append(rendered)
        if "prior_weight" not in item:
            prior_weight = 5.0
            _record_fill(fills, f"{path}.prior_weight", "default", "engine.defaults", prior_weight)
        else:
            prior_weight = _as_float(item["prior_weight"], f"{path}.prior_weight", minimum=0.0)
        result.append(
            {
                "name": name,
                "codec": codec,
                "categories": categories,
                "prior_weight": prior_weight,
            }
        )
    return result


def _normalize_cross(
    raw: Any,
    numeric: list[dict[str, Any]],
    categorical: list[dict[str, Any]],
    fills: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    items = _as_list(raw, "cross")
    numeric_names = {item["name"] for item in numeric}
    categorical_names = {item["name"] for item in categorical}
    slots: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        path = f"cross[{index}]"
        item = _as_mapping(item, path)
        slot = _as_string(item.get("slot"), f"{path}.slot")
        if slot in slots:
            raise param_error("duplicate cross slot", field=f"{path}.slot", value=slot)
        slots.add(slot)
        columns_raw = _as_list(item.get("columns"), f"{path}.columns")
        if len(columns_raw) != 2:
            raise param_error("cross slot must reference exactly two columns", field=f"{path}.columns")
        columns: list[str] = []
        for column_index, column in enumerate(columns_raw):
            column = _as_string(column, f"{path}.columns[{column_index}]")
            if column not in numeric_names and column not in categorical_names:
                raise param_error(
                    "cross slot references a missing column",
                    field=f"{path}.columns[{column_index}]",
                    value=column,
                )
            if column in columns:
                raise param_error("cross slot columns must be distinct", field=f"{path}.columns")
            columns.append(column)
        codec = _as_string(item.get("codec"), f"{path}.codec")
        if codec not in {"one_hot", "target", "product"}:
            raise param_error("unsupported cross codec", field=f"{path}.codec", value=codec)
        if codec == "product":
            if any(column not in numeric_names for column in columns):
                raise param_error("product cross slot requires numeric columns", field=f"{path}.columns")
        else:
            if any(column not in categorical_names for column in columns):
                raise param_error("categorical cross slot requires categorical columns", field=f"{path}.columns")
        if "prior_weight" not in item:
            prior_weight = 5.0
            _record_fill(fills, f"{path}.prior_weight", "default", "engine.defaults", prior_weight)
        else:
            prior_weight = _as_float(item["prior_weight"], f"{path}.prior_weight", minimum=0.0)
        result.append(
            {
                "slot": slot,
                "columns": columns,
                "codec": codec,
                "prior_weight": prior_weight,
            }
        )
    return result


def _normalize_selector(raw: Any, fills: list[dict[str, Any]]) -> dict[str, Any]:
    selector = _as_mapping(raw, "selector")
    method = _as_string(selector.get("method"), "selector.method")
    if method not in {"variance", "mutual_info"}:
        raise param_error("unsupported selection method", field="selector.method", value=method)
    target_k = _as_int(selector.get("target_k"), "selector.target_k", minimum=1)
    if "mi_bins" not in selector:
        mi_bins = 8
        _record_fill(fills, "selector.mi_bins", "default", "engine.defaults", mi_bins)
    else:
        mi_bins = _as_int(selector["mi_bins"], "selector.mi_bins", minimum=2)
        if mi_bins > 64:
            raise param_error("selector.mi_bins exceeds maximum", field="selector.mi_bins", maximum=64)
    if "tie_break" not in selector:
        tie_break = "feature_id_asc"
        _record_fill(fills, "selector.tie_break", "default", "engine.defaults", tie_break)
    else:
        tie_break = _as_string(selector["tie_break"], "selector.tie_break")
    if tie_break != "feature_id_asc":
        raise param_error("unsupported selection tie-break", field="selector.tie_break", value=tie_break)
    return {
        "method": method,
        "target_k": target_k,
        "mi_bins": mi_bins,
        "tie_break": tie_break,
    }


def _normalize_limits(raw: Any, fills: list[dict[str, Any]]) -> dict[str, Any]:
    limits = copy.deepcopy(DEFAULT_LIMITS)
    if raw is not None:
        incoming = _as_mapping(raw, "limits")
        for key, value in incoming.items():
            if key not in DEFAULT_LIMITS:
                raise param_error("unsupported resource limit", field=f"limits.{key}", value=key)
            limits[key] = _as_int(value, f"limits.{key}", minimum=1)
    for key, value in DEFAULT_LIMITS.items():
        if not isinstance(raw, dict) or key not in raw:
            _record_fill(fills, f"limits.{key}", "default", "engine.defaults", value)
    return limits


def _normalize_resource_policy(raw: Any, fills: list[dict[str, Any]]) -> dict[str, Any]:
    policy = copy.deepcopy(DEFAULT_RESOURCE_POLICY)
    if raw is not None:
        incoming = _as_mapping(raw, "resource_policy")
        for key, value in incoming.items():
            if key not in DEFAULT_RESOURCE_POLICY:
                raise param_error("unsupported resource policy key", field=f"resource_policy.{key}", value=key)
            policy[key] = copy.deepcopy(value)
    for key, default in DEFAULT_RESOURCE_POLICY.items():
        if not isinstance(raw, dict) or key not in raw:
            _record_fill(fills, f"resource_policy.{key}", "default", "engine.defaults", default)
    if policy["on_batch_rows"] not in {"error", "degrade_serial"}:
        raise param_error("unsupported batch-row resource policy", field="resource_policy.on_batch_rows")
    if policy["on_query_workers"] not in {"error", "degrade_serial"}:
        raise param_error("unsupported query-worker resource policy", field="resource_policy.on_query_workers")
    if policy["on_cache_limit"] not in {"error"}:
        raise param_error("unsupported cache resource policy", field="resource_policy.on_cache_limit")
    priority = _as_list(policy["priority"], "resource_policy.priority")
    expected = {"encoding_matrix", "cache", "batch_rows", "query_workers"}
    if set(priority) != expected or len(priority) != len(expected):
        raise param_error("resource policy priority must enumerate all resources exactly once", field="resource_policy.priority")
    policy["priority"] = [str(item) for item in priority]
    return policy


def normalize_config(
    raw: dict[str, Any],
    *,
    expected_schema: str | None = None,
    fill_sources: list[dict[str, Any]] | None = None,
    default_snapshot_id: str = "config",
) -> FeatureConfig:
    fills = fill_sources if fill_sources is not None else []
    raw = _as_mapping(raw, "config")
    schema = raw.get("schema")
    if schema is None:
        raise param_error("config schema is required", field="schema")
    schema = _as_string(schema, "schema")
    if expected_schema is not None and schema != expected_schema:
        raise param_error("unexpected config schema", field="schema", expected=expected_schema, value=schema)
    if schema not in {"feature_config/v1", "feature_config/v2"}:
        raise param_error("unsupported config schema", field="schema", value=schema)

    snapshot_id = raw.get("snapshot_id")
    if snapshot_id is None:
        _record_fill(fills, "snapshot_id", "derived", "default_snapshot_id", default_snapshot_id)
        snapshot_id = default_snapshot_id
    snapshot_id = _as_string(snapshot_id, "snapshot_id")
    snapshot_version_raw = raw.get("snapshot_version")
    if snapshot_version_raw is None:
        _record_fill(fills, "snapshot_version", "default", "engine.defaults", 1)
        snapshot_version = 1
    else:
        snapshot_version = _as_int(snapshot_version_raw, "snapshot_version", minimum=1)

    effective_time = raw.get("effective_time")
    if effective_time is not None:
        effective_time = _as_mapping(effective_time, "effective_time")
        if "absolute_ms" in effective_time:
            raw = copy.deepcopy(raw)
            raw["effective_at_ms"] = effective_time["absolute_ms"]
        if "epoch_ms" in effective_time:
            raw = copy.deepcopy(raw)
            raw["epoch_ms"] = effective_time["epoch_ms"]
    epoch_ms_raw = raw.get("epoch_ms")
    if epoch_ms_raw is None:
        _record_fill(fills, "epoch_ms", "default", "engine.defaults", DEFAULT_EPOCH_MS)
        epoch_ms = DEFAULT_EPOCH_MS
    else:
        epoch_ms = _as_int(epoch_ms_raw, "epoch_ms", minimum=0)
    if raw.get("effective_at_ms") is not None:
        effective_at_ms = _as_int(raw["effective_at_ms"], "effective_at_ms", minimum=0)
    else:
        effective_at_ms = None
    if raw.get("effective_at_sec") is not None:
        effective_at_sec = _as_float(raw["effective_at_sec"], "effective_at_sec", minimum=0.0)
    else:
        if effective_at_ms is None:
            raise param_error("one of effective_at_sec or effective_at_ms is required", field="effective_at_sec")
        effective_at_sec = (effective_at_ms - epoch_ms) / 1000.0
        _record_fill(fills, "effective_at_sec", "derived", "effective_at_ms/epoch_ms", round(effective_at_sec, 9))
    if effective_at_ms is None:
        effective_at_ms = epoch_ms + round(effective_at_sec * 1000.0)
        _record_fill(fills, "effective_at_ms", "derived", "epoch_ms+effective_at_sec", effective_at_ms)

    seed_raw = raw.get("seed")
    if seed_raw is None:
        raise param_error("config seed is required", field="seed")
    seed = _as_int(seed_raw, "seed")
    fit_rows_raw = raw.get("fit_rows")
    if fit_rows_raw is None:
        raise param_error("config fit_rows is required", field="fit_rows")
    fit_rows = _as_int(fit_rows_raw, "fit_rows", minimum=1)
    row_id_column = raw.get("row_id_column")
    if row_id_column is None:
        _record_fill(fills, "row_id_column", "default", "engine.defaults", "row_id")
        row_id_column = "row_id"
    row_id_column = _as_string(row_id_column, "row_id_column")
    label_column = raw.get("label_column")
    if label_column is None:
        _record_fill(fills, "label_column", "default", "engine.defaults", "label")
        label_column = "label"
    label_column = _as_string(label_column, "label_column")

    numeric = _normalize_numeric(raw.get("numeric"), fills)
    categorical = _normalize_categorical(raw.get("categorical"), fills)
    numeric_names = {item["name"] for item in numeric}
    categorical_names = {item["name"] for item in categorical}
    overlap = numeric_names & categorical_names
    if overlap:
        raise param_error("feature columns cannot be both numeric and categorical", columns=sorted(overlap))
    if row_id_column in numeric_names | categorical_names:
        raise param_error("row id column cannot also be an encoded feature", field="row_id_column")
    if label_column in numeric_names | categorical_names:
        raise param_error("label column cannot also be an encoded feature", field="label_column")
    if row_id_column == label_column:
        raise param_error("row id and label columns must differ", field="label_column")
    cross = _normalize_cross(raw.get("cross"), numeric, categorical, fills)
    selector = _normalize_selector(raw.get("selector"), fills)
    limits = _normalize_limits(raw.get("limits"), fills)
    resource_policy = _normalize_resource_policy(raw.get("resource_policy"), fills)

    return FeatureConfig(
        schema=schema,
        snapshot_id=snapshot_id,
        snapshot_version=snapshot_version,
        effective_at_sec=effective_at_sec,
        effective_at_ms=effective_at_ms,
        epoch_ms=epoch_ms,
        seed=seed,
        fit_rows=fit_rows,
        row_id_column=row_id_column,
        label_column=label_column,
        numeric=numeric,
        categorical=categorical,
        cross=cross,
        selector=selector,
        limits=limits,
        resource_policy=resource_policy,
        completion_sources=fills,
    )


def replace_config(config: FeatureConfig, **changes: Any) -> FeatureConfig:
    cloned = config.clone()
    for key, value in changes.items():
        if not hasattr(cloned, key):
            raise param_error("unknown config field", field=key)
        setattr(cloned, key, copy.deepcopy(value))
    return cloned


def wrap_config_error(error: EngineError) -> EngineError:
    if error.code == E_PARAM_INVALID:
        return EngineError(
            E_PARAM_INVALID,
            error.message,
            _detail(error),
            error.completed_steps,
        )
    return error
