"""Feature configuration parsing and validation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import read_json, sha256_obj
from .data import DEFAULT_ID_COLUMN, DEFAULT_LABEL_COLUMN
from .errors import E_CONFIG_INVALID, E_SLOT_DUP, BusinessError


@dataclass(frozen=True)
class CategoryFeature:
    name: str
    encoding: str
    smoothing: float
    folds: int
    seed: int
    raw: dict[str, Any]


@dataclass(frozen=True)
class NumericFeature:
    name: str
    standardization: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class CrossSlot:
    slot_id: str
    columns: tuple[str, ...]
    representation: str
    column_prefix: str
    name_separator: str
    value_separator: str
    ordinal_suffix: str
    raw: dict[str, Any]

    @property
    def combination_key(self) -> tuple[str, ...]:
        return tuple(sorted(self.columns))


@dataclass(frozen=True)
class SelectorSpec:
    type: str
    mode: str
    target_count: int | None
    threshold: float | None
    bins: int
    default_variance_threshold: float
    raw: dict[str, Any]


@dataclass(frozen=True)
class FeatureConfig:
    raw: dict[str, Any]
    config_version: int
    statistics_version: int
    selection_version: int
    seed: int
    id_column: str
    label_column: str
    category_features: tuple[CategoryFeature, ...]
    numeric_features: tuple[NumericFeature, ...]
    cross_slots: tuple[CrossSlot, ...]
    selector: SelectorSpec

    @property
    def digest(self) -> str:
        return sha256_obj(self.raw)

    @property
    def slots_digest(self) -> str:
        return sha256_obj([slot.raw for slot in self.cross_slots])

    def category(self, name: str) -> CategoryFeature | None:
        return next((feature for feature in self.category_features if feature.name == name), None)

    def numeric(self, name: str) -> NumericFeature | None:
        return next((feature for feature in self.numeric_features if feature.name == name), None)

    def stage_feature_digest(self, stage: str, feature: str) -> str:
        if stage == "encode":
            candidate = self.category(feature)
        elif stage == "standardize":
            candidate = self.numeric(feature)
        elif stage == "cross":
            candidate = next(
                (slot for slot in self.cross_slots if slot.slot_id == feature),
                None,
            )
        else:
            candidate = None
        if candidate is None:
            return sha256_obj({"stage": stage, "feature": feature})
        return sha256_obj(candidate.raw)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)


def _require_mapping(value: Any, field: str, *, stage: str, feature: str | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(
            E_CONFIG_INVALID,
            f"{field} must be an object",
            stage=stage,
            feature=feature,
            details={"field": field},
        )
    return value


def _require_int(
    value: Any,
    field: str,
    *,
    stage: str,
    feature: str | None,
    minimum: int | None = None,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BusinessError(
            E_CONFIG_INVALID,
            f"{field} must be an integer",
            stage=stage,
            feature=feature,
            details={"field": field, "value": value},
        )
    if minimum is not None and value < minimum:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"{field} must be >= {minimum}",
            stage=stage,
            feature=feature,
            details={"field": field, "value": value, "minimum": minimum},
        )
    return value


def _require_number(
    value: Any,
    field: str,
    *,
    stage: str,
    feature: str | None,
    minimum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BusinessError(
            E_CONFIG_INVALID,
            f"{field} must be a finite number",
            stage=stage,
            feature=feature,
            details={"field": field, "value": value},
        )
    number = float(value)
    if not math.isfinite(number):
        raise BusinessError(
            E_CONFIG_INVALID,
            f"{field} must be finite",
            stage=stage,
            feature=feature,
            details={"field": field, "value": value},
        )
    if minimum is not None and number < minimum:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"{field} must be >= {minimum}",
            stage=stage,
            feature=feature,
            details={"field": field, "value": number, "minimum": minimum},
        )
    return number


def parse_config(raw: dict[str, Any]) -> FeatureConfig:
    if not isinstance(raw, dict):
        raise BusinessError(
            E_CONFIG_INVALID,
            "config must be a JSON object",
            stage="config",
            details={"field": "$"},
        )

    config_version = _require_int(
        raw.get("config_version"),
        "config_version",
        stage="config",
        feature=None,
        minimum=1,
    )
    statistics_version = _require_int(
        raw.get("statistics_version", config_version),
        "statistics_version",
        stage="config",
        feature=None,
        minimum=1,
    )
    selection_version = _require_int(
        raw.get("selection_version", config_version),
        "selection_version",
        stage="config",
        feature=None,
        minimum=1,
    )
    seed = _require_int(raw.get("seed"), "seed", stage="encode", feature=None)
    id_column = raw.get("row_id_column", DEFAULT_ID_COLUMN)
    label_column = raw.get("label_column", DEFAULT_LABEL_COLUMN)
    if not isinstance(id_column, str) or not id_column:
        raise BusinessError(
            E_CONFIG_INVALID,
            "row_id_column must be a non-empty string",
            stage="config",
            feature=None,
            details={"field": "row_id_column"},
        )
    if not isinstance(label_column, str) or not label_column:
        raise BusinessError(
            E_CONFIG_INVALID,
            "label_column must be a non-empty string",
            stage="config",
            feature=None,
            details={"field": "label_column"},
        )

    category_raw = raw.get("category_features")
    if not isinstance(category_raw, list):
        raise BusinessError(
            E_CONFIG_INVALID,
            "category_features must be a list",
            stage="encode",
            details={"field": "category_features"},
        )
    category_features: list[CategoryFeature] = []
    category_names: set[str] = set()
    for index, item in enumerate(category_raw):
        feature_raw = _require_mapping(
            item,
            f"category_features[{index}]",
            stage="encode",
            feature=None,
        )
        name = feature_raw.get("name")
        if not isinstance(name, str) or not name:
            raise BusinessError(
                E_CONFIG_INVALID,
                "category feature name must be a non-empty string",
                stage="encode",
                feature=None,
                details={"field": f"category_features[{index}].name"},
            )
        if name in category_names:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"duplicate category feature: {name}",
                stage="encode",
                feature=name,
                details={"field": f"category_features[{index}].name"},
            )
        category_names.add(name)
        encoding = feature_raw.get("encoding")
        if encoding not in {"one-hot", "target"}:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"unknown encoding for column {name}: {encoding!r}",
                stage="encode",
                feature=name,
                details={
                    "field": f"category_features[{index}].encoding",
                    "allowed": ["one-hot", "target"],
                    "value": encoding,
                },
            )
        smoothing = _require_number(
            feature_raw.get("smoothing", 10.0),
            f"category_features[{index}].smoothing",
            stage="encode",
            feature=name,
            minimum=0.0,
        )
        folds = _require_int(
            feature_raw.get("folds", 5),
            f"category_features[{index}].folds",
            stage="encode",
            feature=name,
            minimum=1,
        )
        feature_seed = _require_int(
            feature_raw.get("seed", seed),
            f"category_features[{index}].seed",
            stage="encode",
            feature=name,
        )
        category_features.append(
            CategoryFeature(
                name=name,
                encoding=encoding,
                smoothing=smoothing,
                folds=folds,
                seed=feature_seed,
                raw=dict(feature_raw),
            )
        )

    numeric_raw = raw.get("numeric_features")
    if not isinstance(numeric_raw, list):
        raise BusinessError(
            E_CONFIG_INVALID,
            "numeric_features must be a list",
            stage="standardize",
            details={"field": "numeric_features"},
        )
    numeric_features: list[NumericFeature] = []
    numeric_names: set[str] = set()
    for index, item in enumerate(numeric_raw):
        feature_raw = _require_mapping(
            item,
            f"numeric_features[{index}]",
            stage="standardize",
            feature=None,
        )
        name = feature_raw.get("name")
        if not isinstance(name, str) or not name:
            raise BusinessError(
                E_CONFIG_INVALID,
                "numeric feature name must be a non-empty string",
                stage="standardize",
                feature=None,
                details={"field": f"numeric_features[{index}].name"},
            )
        if name in numeric_names:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"duplicate numeric feature: {name}",
                stage="standardize",
                feature=name,
                details={"field": f"numeric_features[{index}].name"},
            )
        numeric_names.add(name)
        standardization = feature_raw.get("standardization")
        if standardization not in {"z-score", "min-max"}:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"unknown standardization for column {name}: {standardization!r}",
                stage="standardize",
                feature=name,
                details={
                    "field": f"numeric_features[{index}].standardization",
                    "allowed": ["z-score", "min-max"],
                    "value": standardization,
                },
            )
        numeric_features.append(
            NumericFeature(name=name, standardization=standardization, raw=dict(feature_raw))
        )

    overlap = category_names & numeric_names
    if overlap:
        conflicting = sorted(overlap)[0]
        raise BusinessError(
            E_CONFIG_INVALID,
            f"column appears as both category and numeric: {conflicting}",
            stage="config",
            feature=conflicting,
            details={"field": "numeric_features.name"},
        )

    slots_raw = raw.get("cross_slots", [])
    if not isinstance(slots_raw, list):
        raise BusinessError(
            E_CONFIG_INVALID,
            "cross_slots must be a list",
            stage="cross",
            details={"field": "cross_slots"},
        )
    cross_slots: list[CrossSlot] = []
    slot_ids: set[str] = set()
    combinations: dict[tuple[str, ...], str] = {}
    for index, item in enumerate(slots_raw):
        slot_raw = _require_mapping(
            item,
            f"cross_slots[{index}]",
            stage="cross",
            feature=None,
        )
        slot_id = slot_raw.get("slot_id")
        if not isinstance(slot_id, str) or not slot_id:
            raise BusinessError(
                E_CONFIG_INVALID,
                "cross slot_id must be a non-empty string",
                stage="cross",
                feature=None,
                details={"field": f"cross_slots[{index}].slot_id"},
            )
        if slot_id in slot_ids:
            raise BusinessError(
                E_SLOT_DUP,
                f"duplicate cross slot_id: {slot_id}",
                stage="cross",
                feature=slot_id,
                details={"field": f"cross_slots[{index}].slot_id"},
            )
        slot_ids.add(slot_id)
        columns_value = slot_raw.get("columns")
        if (
            not isinstance(columns_value, list)
            or len(columns_value) not in {2, 3}
            or any(not isinstance(column, str) or not column for column in columns_value)
        ):
            raise BusinessError(
                E_CONFIG_INVALID,
                f"cross slot {slot_id} columns must contain two or three names",
                stage="cross",
                feature=slot_id,
                details={"field": f"cross_slots[{index}].columns"},
            )
        columns = tuple(columns_value)
        if len(set(columns)) != len(columns):
            raise BusinessError(
                E_SLOT_DUP,
                f"cross slot {slot_id} repeats a column",
                stage="cross",
                feature=slot_id,
                details={"field": f"cross_slots[{index}].columns", "columns": list(columns)},
            )
        combination = tuple(sorted(columns))
        if combination in combinations:
            raise BusinessError(
                E_SLOT_DUP,
                f"cross slot {slot_id} duplicates combination from {combinations[combination]}",
                stage="cross",
                feature=slot_id,
                details={
                    "field": f"cross_slots[{index}].columns",
                    "duplicate_of": combinations[combination],
                    "columns": list(columns),
                },
            )
        combinations[combination] = slot_id
        representation = slot_raw.get("representation")
        if representation not in {"one_hot", "ordinal"}:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"unknown cross representation for slot {slot_id}: {representation!r}",
                stage="cross",
                feature=slot_id,
                details={
                    "field": f"cross_slots[{index}].representation",
                    "allowed": ["one_hot", "ordinal"],
                    "value": representation,
                },
            )
        column_prefix = slot_raw.get("column_prefix", "x")
        name_separator = slot_raw.get("name_separator", "__")
        value_separator = slot_raw.get("value_separator", "|")
        ordinal_suffix = slot_raw.get("ordinal_suffix", "ordinal")
        for field_name, field_value in (
            ("column_prefix", column_prefix),
            ("name_separator", name_separator),
            ("value_separator", value_separator),
            ("ordinal_suffix", ordinal_suffix),
        ):
            if not isinstance(field_value, str) or field_value == "":
                raise BusinessError(
                    E_CONFIG_INVALID,
                    f"{field_name} for slot {slot_id} must be a non-empty string",
                    stage="cross",
                    feature=slot_id,
                    details={"field": f"cross_slots[{index}].{field_name}"},
                )
        cross_slots.append(
            CrossSlot(
                slot_id=slot_id,
                columns=columns,
                representation=representation,
                column_prefix=column_prefix,
                name_separator=name_separator,
                value_separator=value_separator,
                ordinal_suffix=ordinal_suffix,
                raw=dict(slot_raw),
            )
        )

    selector_raw = _require_mapping(raw.get("selector"), "selector", stage="select", feature=None)
    selector_type = selector_raw.get("type")
    if selector_type not in {"variance", "mutual_info"}:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"unknown selector type: {selector_type!r}",
            stage="select",
            feature="selector.type",
            details={
                "field": "selector.type",
                "allowed": ["variance", "mutual_info"],
                "value": selector_type,
            },
        )
    mode = selector_raw.get("mode", "top_k")
    if mode not in {"top_k", "threshold"}:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"unknown selector mode: {mode!r}",
            stage="select",
            feature="selector.mode",
            details={
                "field": "selector.mode",
                "allowed": ["top_k", "threshold"],
                "value": mode,
            },
        )
    target_count_raw = selector_raw.get("target_count")
    target_count: int | None = None
    if mode == "top_k" or target_count_raw is not None:
        if target_count_raw is None:
            raise BusinessError(
                E_CONFIG_INVALID,
                "target_count is required for top_k selection",
                stage="select",
                feature="selector.target_count",
                details={"field": "selector.target_count"},
            )
        target_count = _require_int(
            target_count_raw,
            "selector.target_count",
            stage="select",
            feature="selector.target_count",
            minimum=1,
        )
    threshold: float | None = None
    if selector_type == "variance" or mode == "threshold":
        if "threshold" not in selector_raw:
            raise BusinessError(
                E_CONFIG_INVALID,
                "selector.threshold is required",
                stage="select",
                feature="selector.threshold",
                details={"field": "selector.threshold"},
            )
    if "threshold" in selector_raw:
        threshold = _require_number(
            selector_raw["threshold"],
            "selector.threshold",
            stage="select",
            feature="selector.threshold",
            minimum=0.0,
        )
    bins = _require_int(
        selector_raw.get("bins", 5),
        "selector.bins",
        stage="select",
        feature="selector.bins",
        minimum=2,
    )
    default_variance_threshold = _require_number(
        selector_raw.get("default_variance_threshold", 0.0),
        "selector.default_variance_threshold",
        stage="select",
        feature="selector.default_variance_threshold",
        minimum=0.0,
    )
    selector = SelectorSpec(
        type=selector_type,
        mode=mode,
        target_count=target_count,
        threshold=threshold,
        bins=bins,
        default_variance_threshold=default_variance_threshold,
        raw=dict(selector_raw),
    )

    return FeatureConfig(
        raw=dict(raw),
        config_version=config_version,
        statistics_version=statistics_version,
        selection_version=selection_version,
        seed=seed,
        id_column=id_column,
        label_column=label_column,
        category_features=tuple(category_features),
        numeric_features=tuple(numeric_features),
        cross_slots=tuple(cross_slots),
        selector=selector,
    )


def load_config(path: str | Path) -> FeatureConfig:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise BusinessError(
            E_CONFIG_INVALID,
            "config must be a JSON object",
            stage="config",
            details={"field": "$"},
        )
    return parse_config(raw)
