from __future__ import annotations

import copy
import hashlib
import itertools
import math
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import quote

import numpy as np

from .config import FeatureConfig, normalize_config, replace_config
from .errors import (
    E_PARAM_INVALID,
    E_RESOURCE_LIMIT,
    EngineError,
    bad_input,
    encoding_error,
    param_error,
    resource_error,
)
from .sample import rows_to_canonical
from .util import canonical_json, sha256_bytes, sha256_json, stable_float


@dataclass
class FeaturePlan:
    features: list[dict[str, Any]]

    @property
    def ids(self) -> list[str]:
        return [feature["id"] for feature in self.features]

    def summary(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for feature in self.features:
            counts[feature["kind"]] = counts.get(feature["kind"], 0) + 1
        return {
            "feature_count": len(self.features),
            "by_kind": dict(sorted(counts.items())),
            "feature_ids": self.ids,
        }


@dataclass
class FitContext:
    params: dict[str, Any]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"digest": self.digest, "params": copy.deepcopy(self.params)}


@dataclass
class Computation:
    config: FeatureConfig
    rows: list[dict[str, Any]]
    plan: FeaturePlan
    context: FitContext
    encodings: dict[str, dict[str, float]]
    feature_scores: list[float]
    retained_flags: list[bool]
    retained_feature_ids: list[str]
    matrix_digest: str
    selection_key: str
    row_records: list[dict[str, Any]]
    feature_keys: dict[str, str]
    computed_feature_keys: list[str]
    reused_feature_keys: list[str]
    recomputed_row_ids: list[str]
    reused_row_ids: list[str]
    affected_feature_ids: list[str]
    encoded_cells: int
    selected_score_summary: dict[str, float] = field(default_factory=dict)

    def record_for(self, row_id: str) -> dict[str, Any]:
        for record in self.row_records:
            if record["row_id"] == row_id:
                return record
        raise bad_input("row_id is not present in the committed selection", field="row_id", row_id=row_id)

    def payload(self) -> dict[str, Any]:
        return {
            "config_digest": self.config.semantic_digest,
            "matrix_digest": self.matrix_digest,
            "selection_key": self.selection_key,
            "feature_ids": self.plan.ids,
            "feature_scores": list(self.feature_scores),
            "retained_flags": list(self.retained_flags),
            "retained_feature_ids": list(self.retained_feature_ids),
            "row_records": copy.deepcopy(self.row_records),
        }

    def summary(self) -> dict[str, Any]:
        reused_rows = sorted(self.reused_row_ids)
        recomputed_rows = sorted(self.recomputed_row_ids)
        return {
            "selection_key": self.selection_key,
            "matrix_digest": self.matrix_digest,
            "feature_summary": self.plan.summary(),
            "selection": {
                "method": self.config.selector["method"],
                "target_k": self.config.selector["target_k"],
                "feature_ids": self.plan.ids,
                "scores": list(self.feature_scores),
                "retained": list(self.retained_flags),
                "retained_feature_ids": list(self.retained_feature_ids),
            },
            "cache_reuse": {
                "reused_entry_count": len(self.reused_feature_keys),
                "computed_entry_count": len(self.computed_feature_keys),
                "reused_entry_keys": list(self.reused_feature_keys),
                "computed_entry_keys": list(self.computed_feature_keys),
                "reused_entry_key_digest": sha256_json(list(self.reused_feature_keys)),
                "computed_entry_key_digest": sha256_json(list(self.computed_feature_keys)),
                "reused_row_ids": reused_rows,
                "recomputed_row_ids": recomputed_rows,
                "affected_feature_ids": list(self.affected_feature_ids),
                "encoded_cells": self.encoded_cells,
                "recomputed_cells": len(self.computed_feature_keys),
            },
            "row_count": len(self.rows),
            "rows": copy.deepcopy(self.row_records),
        }


def _feature_id(prefix: str, *parts: str) -> str:
    rendered = ".".join(quote(str(part), safe="") for part in parts)
    return f"{prefix}.{rendered}"


def build_feature_plan(config: FeatureConfig) -> FeaturePlan:
    features: list[dict[str, Any]] = []
    ids: set[str] = set()

    def add(feature: dict[str, Any]) -> None:
        feature_id = feature["id"]
        if feature_id in ids:
            raise param_error("feature id collision", field="feature.id", value=feature_id)
        ids.add(feature_id)
        features.append(feature)

    for spec in config.numeric:
        add(
            {
                "id": _feature_id("num", spec["name"]),
                "kind": "numeric",
                "source": spec["name"],
                "method": spec["method"],
            }
        )
    for spec in config.categorical:
        if spec["codec"] == "one_hot":
            for category in spec["categories"]:
                add(
                    {
                        "id": _feature_id("cat", spec["name"], category),
                        "kind": "one_hot",
                        "source": spec["name"],
                        "category": category,
                    }
                )
        else:
            add(
                {
                    "id": _feature_id("cat", spec["name"]) + "#target",
                    "kind": "target",
                    "source": spec["name"],
                }
            )
    categorical_by_name = {item["name"]: item for item in config.categorical}
    for spec in config.cross:
        columns = spec["columns"]
        if spec["codec"] in {"one_hot", "target"}:
            category_lists = [categorical_by_name[column]["categories"] for column in columns]
            combinations = list(itertools.product(*category_lists))
            if spec["codec"] == "one_hot":
                for combination in combinations:
                    add(
                        {
                            "id": _feature_id("cross", spec["slot"], *combination),
                            "kind": "cross_one_hot",
                            "slot": spec["slot"],
                            "columns": list(columns),
                            "combination": list(combination),
                        }
                    )
            else:
                add(
                    {
                        "id": _feature_id("cross", spec["slot"]) + "#target",
                        "kind": "cross_target",
                        "slot": spec["slot"],
                        "columns": list(columns),
                    }
                )
        else:
            add(
                {
                    "id": _feature_id("cross", spec["slot"]) + "#product",
                    "kind": "cross_product",
                    "slot": spec["slot"],
                    "columns": list(columns),
                }
            )
    return FeaturePlan(features=features)


def validate_rows(rows: list[dict[str, Any]], config: FeatureConfig) -> list[dict[str, Any]]:
    if not rows:
        raise bad_input("input contains no rows", field="rows")
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    categories_by_name = {item["name"]: set(item["categories"]) for item in config.categorical}
    numeric_names = {item["name"] for item in config.numeric}
    categorical_names = set(categories_by_name)
    for index, row in enumerate(rows):
        row_id = row.get("row_id")
        if not isinstance(row_id, str) or not row_id:
            raise bad_input("row_id must be a non-empty string", row_index=index, field="row_id")
        if row_id in seen:
            duplicates.append(row_id)
        seen[row_id] = seen.get(row_id, 0) + 1
        numerics = row.get("numerics")
        categoricals = row.get("categoricals")
        if not isinstance(numerics, dict) or not isinstance(categoricals, dict):
            raise bad_input("row structure is invalid", row=row_id, field="row")
        if set(numerics) != numeric_names:
            raise bad_input(
                "row numeric fields do not match config",
                row=row_id,
                field="numerics",
                expected=sorted(numeric_names),
                actual=sorted(numerics),
            )
        if set(categoricals) != categorical_names:
            raise bad_input(
                "row categorical fields do not match config",
                row=row_id,
                field="categoricals",
                expected=sorted(categorical_names),
                actual=sorted(categoricals),
            )
        for name, value in numerics.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise bad_input(
                    "numeric field contains a non-finite or invalid value",
                    row=row_id,
                    field=name,
                )
        for name, value in categoricals.items():
            rendered = str(value)
            if rendered not in categories_by_name[name]:
                raise encoding_error(
                    "categorical value is outside the configured category set",
                    row=row_id,
                    field=name,
                    value=rendered,
                    allowed=sorted(categories_by_name[name]),
                )
        label = row.get("label")
        if isinstance(label, bool) or not isinstance(label, (int, float)) or not math.isfinite(float(label)):
            raise bad_input("label contains a non-finite or invalid value", row=row_id, field=config.label_column)
    if duplicates:
        raise bad_input("input contains duplicate row_id values", field="row_id", duplicate_row_ids=sorted(duplicates))
    if config.fit_rows > len(rows):
        raise param_error(
            "fit_rows exceeds available row count",
            field="fit_rows",
            fit_rows=config.fit_rows,
            row_count=len(rows),
        )
    return sorted(rows, key=lambda item: item["row_id"])


def _category_mean(mean_sum: float, count: int, prior_weight: float, global_mean: float) -> float:
    if count == 0:
        return global_mean
    denominator = count + prior_weight
    if denominator == 0:
        return global_mean
    return (mean_sum + prior_weight * global_mean) / denominator


def fit_context(config: FeatureConfig, rows: list[dict[str, Any]]) -> FitContext:
    fit_rows = rows[: config.fit_rows]
    params: dict[str, Any] = {"numeric": {}, "categorical_target": {}, "cross_target": {}}
    labels = np.asarray([float(row["label"]) for row in fit_rows], dtype=np.float64)
    global_mean = stable_float(float(np.mean(labels))) if labels.size else 0.0
    for spec in config.numeric:
        values = np.asarray([float(row["numerics"][spec["name"]]) for row in fit_rows], dtype=np.float64)
        if spec["method"] == "standardize":
            mean = float(np.mean(values))
            scale = float(np.std(values, ddof=0))
            if not math.isfinite(scale) or scale == 0.0:
                scale = 1.0
            params["numeric"][spec["name"]] = {
                "method": "standardize",
                "mean": stable_float(mean),
                "scale": stable_float(scale),
            }
        else:
            minimum = float(np.min(values))
            maximum = float(np.max(values))
            span = maximum - minimum
            if not math.isfinite(span) or span == 0.0:
                span = 1.0
            params["numeric"][spec["name"]] = {
                "method": "minmax",
                "min": stable_float(minimum),
                "span": stable_float(span),
            }
    for spec in config.categorical:
        counts: dict[str, int] = {category: 0 for category in spec["categories"]}
        sums: dict[str, float] = {category: 0.0 for category in spec["categories"]}
        for row in fit_rows:
            category = row["categoricals"][spec["name"]]
            counts[category] += 1
            sums[category] += float(row["label"])
        encoded: dict[str, float] = {}
        for category in spec["categories"]:
            encoded[category] = stable_float(
                _category_mean(sums[category], counts[category], spec["prior_weight"], global_mean)
            )
        params["categorical_target"][spec["name"]] = {
            "global_mean": global_mean,
            "values": encoded,
        }
    categorical_by_name = {spec["name"]: spec for spec in config.categorical}
    for spec in config.cross:
        if spec["codec"] != "target":
            continue
        columns = spec["columns"]
        category_lists = [categorical_by_name[column]["categories"] for column in columns]
        combinations = list(itertools.product(*category_lists))
        counts = {"\u0000".join(combo): 0 for combo in combinations}
        sums = {"\u0000".join(combo): 0.0 for combo in combinations}
        for row in fit_rows:
            combo_values = [row["categoricals"][column] for column in columns]
            key = "\u0000".join(combo_values)
            counts[key] += 1
            sums[key] += float(row["label"])
        encoded = {}
        for combo in combinations:
            key = "\u0000".join(combo)
            encoded[key] = stable_float(
                _category_mean(sums[key], counts[key], spec["prior_weight"], global_mean)
            )
        params["cross_target"][spec["slot"]] = {
            "global_mean": global_mean,
            "values": encoded,
        }
    digest = sha256_json(
        {
            "fit_rows": config.fit_rows,
            "numeric": params["numeric"],
            "categorical_target": params["categorical_target"],
            "cross_target": params["cross_target"],
        }
    )
    return FitContext(params=params, digest=digest)


def _source_values(feature: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    kind = feature["kind"]
    if kind == "numeric" or kind == "cross_product":
        return {
            name: stable_float(row["numerics"][name])
            for name in feature.get("columns", [feature.get("source")])
        }
    return {
        name: str(row["categoricals"][name])
        for name in feature.get("columns", [feature.get("source")])
    }


def feature_cache_key(feature: dict[str, Any], row: dict[str, Any], context: FitContext) -> str:
    payload = {
        "row_id": row["row_id"],
        "feature": feature,
        "source_values": _source_values(feature, row),
        "context_digest": context.digest,
    }
    return sha256_json(payload)


def _numeric_value(name: str, row: dict[str, Any], context: FitContext) -> float:
    value = float(row["numerics"][name])
    params = context.params["numeric"][name]
    if params["method"] == "standardize":
        result = (value - params["mean"]) / params["scale"]
    else:
        result = (value - params["min"]) / params["span"]
    return stable_float(result)


def encode_feature(feature: dict[str, Any], row: dict[str, Any], context: FitContext) -> float:
    kind = feature["kind"]
    if kind == "numeric":
        return _numeric_value(feature["source"], row, context)
    if kind == "one_hot":
        return 1.0 if row["categoricals"][feature["source"]] == feature["category"] else 0.0
    if kind == "target":
        value = context.params["categorical_target"][feature["source"]]["values"][
            row["categoricals"][feature["source"]]
        ]
        return stable_float(value)
    if kind == "cross_one_hot":
        actual = [row["categoricals"][column] for column in feature["columns"]]
        return 1.0 if actual == feature["combination"] else 0.0
    if kind == "cross_target":
        key = "\u0000".join(row["categoricals"][column] for column in feature["columns"])
        values = context.params["cross_target"][feature["slot"]]["values"]
        if key not in values:
            return stable_float(context.params["cross_target"][feature["slot"]]["global_mean"])
        return stable_float(values[key])
    if kind == "cross_product":
        left, right = feature["columns"]
        return stable_float(_numeric_value(left, row, context) * _numeric_value(right, row, context))
    raise encoding_error("unsupported feature encoding kind", field="feature", value=kind)


def _label_digest(rows: list[dict[str, Any]]) -> str:
    return sha256_json([stable_float(row["label"]) for row in rows])


def matrix_digest(
    rows: list[dict[str, Any]],
    feature_ids: list[str],
    values: np.ndarray,
) -> str:
    header = canonical_json({"row_ids": [row["row_id"] for row in rows], "feature_ids": feature_ids}).encode("utf-8")
    return sha256_bytes(header + b"\n" + np.asarray(values, dtype="<f8", order="C").tobytes(order="C"))


def _mutual_info_scores(values: np.ndarray, labels: np.ndarray, bins: int) -> np.ndarray:
    n_rows, n_features = values.shape
    if n_rows == 0:
        return np.zeros(n_features, dtype=np.float64)
    label_values, label_inverse = np.unique(labels, return_inverse=True)
    n_labels = len(label_values)
    result = np.zeros(n_features, dtype=np.float64)
    for feature_index in range(n_features):
        column = values[:, feature_index]
        if np.all(column == column[0]):
            continue
        unique_count = len(np.unique(column))
        effective_bins = int(min(bins, max(2, unique_count)))
        if unique_count <= bins:
            bin_index = np.searchsorted(np.unique(column), column)
            n_bins = unique_count
        else:
            quantiles = np.linspace(0.0, 1.0, effective_bins + 1)[1:-1]
            edges = np.unique(np.quantile(column, quantiles, method="linear"))
            if len(edges) == 0:
                bin_index = np.zeros(n_rows, dtype=np.int64)
                n_bins = 1
            else:
                bin_index = np.searchsorted(edges, column, side="right")
                n_bins = len(edges) + 1
        joint = np.zeros((n_bins, n_labels), dtype=np.float64)
        np.add.at(joint, (bin_index, label_inverse), 1.0)
        joint /= n_rows
        row_marginal = joint.sum(axis=1, keepdims=True)
        label_marginal = joint.sum(axis=0, keepdims=True)
        nz = joint > 0
        score = float(np.sum(joint[nz] * np.log(joint[nz] / (row_marginal @ label_marginal)[nz])))
        if score < 0 and abs(score) < 1e-12:
            score = 0.0
        result[feature_index] = score
    return result


def select_features(
    values: np.ndarray,
    labels: np.ndarray,
    feature_ids: list[str],
    selector: dict[str, Any],
) -> tuple[list[float], list[bool], list[str]]:
    target_k = int(selector["target_k"])
    if target_k > len(feature_ids):
        raise param_error(
            "selector.target_k exceeds encoded feature count",
            field="selector.target_k",
            target_k=target_k,
            feature_count=len(feature_ids),
        )
    if selector["method"] == "variance":
        raw_scores = np.var(values, axis=0, ddof=0)
    elif selector["method"] == "mutual_info":
        raw_scores = _mutual_info_scores(values, labels, int(selector["mi_bins"]))
    else:
        raise param_error("unsupported selection method", field="selector.method", value=selector["method"])
    scores = [stable_float(float(value)) for value in raw_scores]
    order = sorted(range(len(feature_ids)), key=lambda index: (-scores[index], feature_ids[index]))
    selected_indices = order[:target_k]
    flags = [index in set(selected_indices) for index in range(len(feature_ids))]
    retained = [feature_ids[index] for index in selected_indices]
    return scores, flags, retained


def _selection_key(
    rows: list[dict[str, Any]],
    feature_ids: list[str],
    values: np.ndarray,
    selector: dict[str, Any],
) -> str:
    payload = {
        "row_ids": [row["row_id"] for row in rows],
        "feature_ids": feature_ids,
        "matrix_digest": matrix_digest(rows, feature_ids, values),
        "label_digest": _label_digest(rows),
        "selector": selector,
    }
    return sha256_json(payload)


def _row_record(
    row_id: str,
    feature_ids: list[str],
    values: list[float],
    scores: list[float],
    retained_flags: list[bool],
    selection_key: str,
) -> dict[str, Any]:
    selected_score_sum = stable_float(sum(score for score, kept in zip(scores, retained_flags) if kept))
    return {
        "row_id": row_id,
        "feature_ids": list(feature_ids),
        "encoded": list(values),
        "selection_scores": list(scores),
        "retained": list(retained_flags),
        "selected_score_sum": selected_score_sum,
        "selection_key": selection_key,
    }


def estimate_resident_bytes(row_count: int, feature_count: int) -> int:
    return row_count * feature_count * 8 + row_count * feature_count * 8 + row_count * 64


def compute(
    rows: list[dict[str, Any]],
    config: FeatureConfig,
    *,
    cache: Any | None = None,
    previous: Computation | None = None,
) -> Computation:
    sorted_rows = validate_rows(rows, config)
    plan = build_feature_plan(config)
    context = fit_context(config, sorted_rows)
    feature_count = len(plan.features)
    if config.selector["target_k"] > feature_count:
        raise param_error(
            "selector.target_k exceeds encoded feature count",
            field="selector.target_k",
            target_k=config.selector["target_k"],
            feature_count=feature_count,
        )
    needed_bytes = estimate_resident_bytes(len(sorted_rows), feature_count)
    if needed_bytes > config.limits["max_resident_bytes"]:
        raise resource_error(
            "resident encoded matrix exceeds max_resident_bytes",
            resource="encoding_matrix",
            limit=config.limits["max_resident_bytes"],
            needed=needed_bytes,
        )

    keys_by_row: dict[str, list[str]] = {}
    all_keys: list[str] = []
    for row in sorted_rows:
        keys = [feature_cache_key(feature, row, context) for feature in plan.features]
        keys_by_row[row["row_id"]] = keys
        all_keys.extend(keys)
    reused_values: dict[str, Any] = {}
    if cache is not None:
        reused_values.update(cache.fetch_encoding_values(all_keys))
    elif previous is not None:
        for row in previous.rows:
            old_keys = previous.feature_keys.get(row["row_id"])
            if old_keys:
                for key in old_keys:
                    reused_values[key] = previous.encodings[row["row_id"]][
                        previous.plan.ids[old_keys.index(key)]
                    ]

    encodings: dict[str, dict[str, float]] = {}
    computed_keys: list[str] = []
    reused_keys: list[str] = []
    recomputed_rows: list[str] = []
    reused_rows: list[str] = []
    affected_features: set[str] = set()
    for row in sorted_rows:
        row_values: dict[str, float] = {}
        row_recomputed = False
        for feature, key in zip(plan.features, keys_by_row[row["row_id"]]):
            if key in reused_values:
                try:
                    value = stable_float(float(reused_values[key]))
                except Exception as error:
                    raise encoding_error(
                        "cached encoding entry is not parseable",
                        row=row["row_id"],
                        field=feature["id"],
                        cache_key=key,
                        error=str(error),
                    ) from None
                reused_keys.append(key)
            else:
                value = encode_feature(feature, row, context)
                computed_keys.append(key)
                affected_features.add(feature["id"])
                row_recomputed = True
            row_values[feature["id"]] = value
        encodings[row["row_id"]] = row_values
        if row_recomputed:
            recomputed_rows.append(row["row_id"])
        else:
            reused_rows.append(row["row_id"])

    feature_keys = {row["row_id"]: keys_by_row[row["row_id"]] for row in sorted_rows}
    return compose_computation(
        sorted_rows,
        config,
        encodings,
        plan=plan,
        context=context,
        feature_keys=feature_keys,
        computed_feature_keys=computed_keys,
        reused_feature_keys=reused_keys,
        recomputed_row_ids=recomputed_rows,
        reused_row_ids=reused_rows,
        affected_feature_ids=affected_features,
    )


def compose_computation(
    rows: list[dict[str, Any]],
    config: FeatureConfig,
    encodings: dict[str, dict[str, float]],
    *,
    plan: FeaturePlan | None = None,
    context: FitContext | None = None,
    feature_keys: dict[str, list[str]] | None = None,
    computed_feature_keys: list[str] | None = None,
    reused_feature_keys: list[str] | None = None,
    recomputed_row_ids: list[str] | None = None,
    reused_row_ids: list[str] | None = None,
    affected_feature_ids: list[str] | None = None,
) -> Computation:
    plan = build_feature_plan(config) if plan is None else plan
    context = fit_context(config, rows) if context is None else context
    feature_ids = plan.ids
    for row in rows:
        if row["row_id"] not in encodings:
            raise encoding_error("missing row encoding", field="encodings", row=row["row_id"])
        if set(encodings[row["row_id"]]) != set(feature_ids):
            raise encoding_error(
                "row encoding does not match the feature plan",
                field="encodings",
                row=row["row_id"],
            )
    values = np.asarray(
        [[stable_float(encodings[row["row_id"]][feature_id]) for feature_id in feature_ids] for row in rows],
        dtype=np.float64,
    )
    labels = np.asarray([float(row["label"]) for row in rows], dtype=np.float64)
    scores, retained_flags, retained = select_features(values, labels, feature_ids, config.selector)
    matrix_digest_value = matrix_digest(rows, feature_ids, values)
    key = _selection_key(rows, feature_ids, values, config.selector)
    records = []
    for row in rows:
        vector = [stable_float(encodings[row["row_id"]][feature_id]) for feature_id in feature_ids]
        records.append(_row_record(row["row_id"], feature_ids, vector, scores, retained_flags, key))
    if feature_keys is None:
        feature_keys = {
            row["row_id"]: [feature_cache_key(feature, row, context) for feature in plan.features]
            for row in rows
        }
    selected_score_summary = {
        feature_ids[index]: scores[index]
        for index in sorted(range(len(feature_ids)), key=lambda item: (-scores[item], feature_ids[item]))
        if retained_flags[index]
    }
    return Computation(
        config=config,
        rows=rows,
        plan=plan,
        context=context,
        encodings={row_id: {feature_id: stable_float(value) for feature_id, value in values.items()} for row_id, values in encodings.items()},
        feature_scores=scores,
        retained_flags=retained_flags,
        retained_feature_ids=retained,
        matrix_digest=matrix_digest_value,
        selection_key=key,
        row_records=records,
        feature_keys=feature_keys,
        computed_feature_keys=sorted(computed_feature_keys or []),
        reused_feature_keys=sorted(reused_feature_keys or []),
        recomputed_row_ids=sorted(recomputed_row_ids or []),
        reused_row_ids=sorted(reused_row_ids or []),
        affected_feature_ids=sorted(affected_feature_ids or []),
        encoded_cells=len(rows) * len(feature_ids),
        selected_score_summary=selected_score_summary,
    )


def _revalidate(config: FeatureConfig) -> FeatureConfig:
    raw = config.to_v1_dict()
    raw["schema"] = config.schema
    raw["effective_at_ms"] = config.effective_at_ms
    fresh = normalize_config(
        raw,
        expected_schema=config.schema,
        fill_sources=[],
        default_snapshot_id=config.snapshot_id,
    )
    fresh.completion_sources = copy.deepcopy(config.completion_sources)
    return fresh


def apply_row_correction(
    rows: list[dict[str, Any]],
    row_id: str,
    *,
    numeric_updates: dict[str, float] | None = None,
    categorical_updates: dict[str, str] | None = None,
    label: float | None = None,
) -> list[dict[str, Any]]:
    updated = copy.deepcopy(rows)
    found = False
    for row in updated:
        if row["row_id"] == row_id:
            found = True
            for name, value in (numeric_updates or {}).items():
                if name not in row["numerics"]:
                    raise param_error("unknown numeric correction field", field=name, row=row_id)
                row["numerics"][name] = float(value)
            for name, value in (categorical_updates or {}).items():
                if name not in row["categoricals"]:
                    raise param_error("unknown categorical correction field", field=name, row=row_id)
                row["categoricals"][name] = str(value)
            if label is not None:
                row["label"] = float(label)
            break
    if not found:
        raise bad_input("correction row_id is not present", field="row_id", row_id=row_id)
    return updated


def change_categorical_codec(config: FeatureConfig, column: str, codec: str) -> FeatureConfig:
    cloned = config.clone()
    found = False
    for spec in cloned.categorical:
        if spec["name"] == column:
            spec["codec"] = codec
            found = True
            break
    if not found:
        raise param_error("categorical encoding column is not present", field=column)
    return _revalidate(cloned)


def add_cross_slot(config: FeatureConfig, slot_spec: dict[str, Any]) -> FeatureConfig:
    cloned = config.clone()
    cloned.cross.append(copy.deepcopy(slot_spec))
    return _revalidate(cloned)


def remove_cross_slot(config: FeatureConfig, slot: str) -> FeatureConfig:
    cloned = config.clone()
    before = len(cloned.cross)
    cloned.cross = [item for item in cloned.cross if item["slot"] != slot]
    if len(cloned.cross) == before:
        raise param_error("cross slot is not present", field="cross.slot", value=slot)
    return _revalidate(cloned)


def compare_score_sets(
    before: Computation,
    after: Computation,
) -> list[str]:
    if len(before.plan.ids) != len(after.plan.ids) or before.plan.ids != after.plan.ids:
        union = sorted(set(before.plan.ids) | set(after.plan.ids))
        changed: list[str] = []
        for feature_id in union:
            if feature_id not in before.plan.ids or feature_id not in after.plan.ids:
                changed.append(feature_id)
                continue
            if before.feature_scores[before.plan.ids.index(feature_id)] != after.feature_scores[
                after.plan.ids.index(feature_id)
            ]:
                changed.append(feature_id)
        return changed
    return [
        feature_id
        for feature_id, left, right in zip(before.plan.ids, before.feature_scores, after.feature_scores)
        if left != right
    ]
