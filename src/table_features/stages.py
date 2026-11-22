"""Deterministic implementation of the four feature stages."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import quote

import numpy as np

from .cache import (
    CacheLookup,
    ContentAddressedCache,
    build_cache_key,
    matrix_from_base64,
    matrix_to_base64,
    matrix_to_bytes,
)
from .canonical import canonical_bytes, sha256_bytes, sha256_obj
from .config import CategoryFeature, FeatureConfig, NumericFeature
from .data import sample_content_digest
from .errors import (
    E_CACHE_CORRUPT,
    E_CONFIG_INVALID,
    E_SELECT_TARGET_INVALID,
    E_SLOT_DUP,
    E_SLOT_REF_INVALID,
    E_STANDARDIZE_VALUE_INVALID,
    BusinessError,
    Degradation,
    ErrorRecord,
)
from .injection import InjectionConfig


TARGET_FORMULA = (
    "(sum_y_other_folds + smoothing * global_mean_other_folds) / "
    "(count_other_folds + smoothing)"
)
VARIANCE_FORMULA = "mean((x - mean(x))^2); ddof=0"
MI_FORMULA = "sum p(x,y) * log(p(x,y) / (p(x) * p(y))); natural log"


@dataclass
class Diagnostics:
    errors: list[ErrorRecord] = field(default_factory=list)
    degradations: list[Degradation] = field(default_factory=list)
    cache_lookups: list[CacheLookup] = field(default_factory=list)

    def add_error(self, record: ErrorRecord) -> None:
        self.errors.append(record)

    def add_degradation(self, degradation: Degradation) -> None:
        self.degradations.append(degradation)

    def add_lookup(self, lookup: CacheLookup) -> None:
        self.cache_lookups.append(lookup)


@dataclass
class FeatureBlock:
    stage: str
    logical_feature: str
    columns: list[str]
    matrix: np.ndarray
    params: dict[str, Any]
    cache_lookup: CacheLookup | None

    def summary(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "feature": self.logical_feature,
            "columns": self.columns,
            "column_count": len(self.columns),
            "row_count": int(self.matrix.shape[0]),
            "memory_estimate_bytes": int(self.matrix.nbytes),
            "params": self.params,
        }


@dataclass
class StageBlocks:
    blocks: list[FeatureBlock]
    failed_features: list[str]
    errors: list[ErrorRecord]


@dataclass
class SelectionOutcome:
    selected_features: list[str]
    scores: dict[str, float]
    matrix_version: str
    method: str
    mode: str
    threshold: float | None
    target_count: int | None
    score_context: dict[str, Any]
    cache_lookup: CacheLookup
    degradation_category: str | None


def _corrupt_record(error: BusinessError, *, fallback_feature: str) -> ErrorRecord:
    record = error.record
    if record.code != E_CACHE_CORRUPT:
        return record
    return ErrorRecord(
        stage=record.stage,
        code=record.code,
        message=record.message,
        feature=record.feature or fallback_feature,
        row_id=record.row_id,
        details=record.details,
    )


def _cache_components(
    *,
    cfg: FeatureConfig,
    injection: InjectionConfig,
    projection_digest: str,
    slots_digest: str,
    slots_digest_scope: str,
    include_selection_version: bool = False,
) -> dict[str, Any]:
    components: dict[str, Any] = {
        "sample_content_digest": projection_digest,
        "config_version": cfg.config_version,
        "statistics_version": cfg.statistics_version,
        "slots_digest": slots_digest,
        "slots_digest_scope": slots_digest_scope,
        "seed": cfg.seed,
        "injection_digest": injection.digest,
    }
    if include_selection_version:
        components["selection_version"] = cfg.selection_version
    return components


def _load_matrix_payload(
    payload: dict[str, Any],
    *,
    rows: int,
    feature: str,
    stage: str,
) -> tuple[list[str], np.ndarray, dict[str, Any]]:
    columns = payload.get("columns")
    matrix_b64 = payload.get("matrix")
    params = payload.get("params")
    if (
        not isinstance(columns, list)
        or any(not isinstance(column, str) for column in columns)
        or not isinstance(matrix_b64, str)
        or not isinstance(params, dict)
    ):
        raise BusinessError(
            E_CACHE_CORRUPT,
            f"cached {stage} payload is malformed for feature {feature}",
            stage=stage,
            feature=feature,
        )
    try:
        matrix = matrix_from_base64(matrix_b64, expected_shape=(rows, len(columns)))
    except (ValueError, TypeError) as exc:
        raise BusinessError(
            E_CACHE_CORRUPT,
            f"cached {stage} matrix is malformed for feature {feature}: {exc}",
            stage=stage,
            feature=feature,
        ) from exc
    return columns, matrix, params


def _categorical_text(value: Any) -> str:
    return str(value)


def _encoded_category(value: Any) -> str:
    return quote(_categorical_text(value), safe="")


def _one_hot_column(feature: str, value: Any) -> str:
    return f"cat__{feature}__{_encoded_category(value)}"


def _labels(rows: Sequence[dict[str, Any]], label_column: str, *, stage: str) -> np.ndarray:
    values: list[float] = []
    for row in rows:
        if label_column not in row:
            raise BusinessError(
                E_CONFIG_INVALID,
                f"label column {label_column!r} is missing",
                stage=stage,
                feature=label_column,
                row_id=str(row.get("row_id")),
                details={"field": "label_column"},
            )
        raw = row[label_column]
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise BusinessError(
                E_CONFIG_INVALID,
                f"label value for {label_column!r} must be numeric",
                stage=stage,
                feature=label_column,
                row_id=str(row.get("row_id")),
                details={"value": raw},
            )
        value = float(raw)
        if not math.isfinite(value):
            raise BusinessError(
                E_CONFIG_INVALID,
                f"label value for {label_column!r} must be finite",
                stage=stage,
                feature=label_column,
                row_id=str(row.get("row_id")),
                details={"value": raw},
            )
        values.append(value)
    return np.asarray(values, dtype=np.float64)


def _target_encoding(
    rows: Sequence[dict[str, Any]],
    cfg: FeatureConfig,
    feature: CategoryFeature,
) -> tuple[list[str], np.ndarray, dict[str, Any]]:
    if feature.folds <= 0:
        raise BusinessError(
            E_CONFIG_INVALID,
            f"target-encoding folds for column {feature.name} must be positive",
            stage="encode",
            feature=feature.name,
            details={"field": "folds", "value": feature.folds},
        )
    n_rows = len(rows)
    ids = [str(row[cfg.id_column]) for row in rows]
    categories = np.asarray([_categorical_text(row[feature.name]) for row in rows])
    labels = _labels(rows, cfg.label_column, stage="encode")
    rng = np.random.default_rng(feature.seed)
    permutation = rng.permutation(n_rows)
    fold_for_row = np.empty(n_rows, dtype=np.int64)
    for position, row_index in enumerate(permutation):
        fold_for_row[int(row_index)] = position % feature.folds

    fold_sizes = [int(np.sum(fold_for_row == fold)) for fold in range(feature.folds)]
    encoded = np.empty(n_rows, dtype=np.float64)
    global_means: list[float] = []
    for row_index in range(n_rows):
        other_mask = fold_for_row != fold_for_row[row_index]
        other_labels = labels[other_mask]
        global_mean = float(np.mean(other_labels)) if other_labels.size else 0.0
        global_means.append(global_mean)
        category_mask = (categories == categories[row_index]) & other_mask
        count = int(np.sum(category_mask))
        if count == 0:
            encoded[row_index] = global_mean
        elif feature.smoothing == 0:
            encoded[row_index] = float(np.mean(labels[category_mask]))
        else:
            numerator = float(np.sum(labels[category_mask])) + feature.smoothing * global_mean
            encoded[row_index] = numerator / (count + feature.smoothing)

    column = f"cat__{feature.name}__target"
    params = {
        "encoding": "target",
        "column_rule": "cat__<feature>__target",
        "formula": TARGET_FORMULA,
        "smoothing": feature.smoothing,
        "folds": feature.folds,
        "seed": feature.seed,
        "fold_rule": "permutation(row_index, seed); fold=position_in_permutation % folds",
        "fold_sizes": fold_sizes,
        "fold_assignment": [
            {"row_id": ids[index], "fold": int(fold_for_row[index])} for index in range(n_rows)
        ],
        "global_mean_other_folds": global_means,
        "label_column": cfg.label_column,
    }
    return [column], encoded.reshape(-1, 1), params


def _one_hot_encoding(
    rows: Sequence[dict[str, Any]],
    feature: CategoryFeature,
) -> tuple[list[str], np.ndarray, dict[str, Any]]:
    values = [_categorical_text(row[feature.name]) for row in rows]
    vocabulary = sorted(set(values))
    index = {value: position for position, value in enumerate(vocabulary)}
    matrix = np.zeros((len(rows), len(vocabulary)), dtype=np.float64)
    for row_index, value in enumerate(values):
        matrix[row_index, index[value]] = 1.0
    columns = [_one_hot_column(feature.name, value) for value in vocabulary]
    params = {
        "encoding": "one-hot",
        "column_rule": "cat__<feature>__<urlquote(value)>",
        "vocabulary": vocabulary,
    }
    return columns, matrix, params


def encode_categories(
    rows: Sequence[dict[str, Any]],
    cfg: FeatureConfig,
    cache: ContentAddressedCache,
    injection: InjectionConfig,
    diagnostics: Diagnostics,
) -> StageBlocks:
    injection.raise_if_unclassified("encode")
    blocks: list[FeatureBlock] = []
    failed: list[str] = []
    stage_errors_before = len(diagnostics.errors)
    n_rows = len(rows)

    for feature in cfg.category_features:
        actions = injection.for_stage("encode", feature=feature.name)
        injected_error = next(
            (
                action
                for action in actions
                if action.kind in {"category_value_out_of_range", "target_folds_non_positive"}
            ),
            None,
        )
        if injected_error is not None:
            record = injected_error.to_error()
            diagnostics.add_error(record)
            diagnostics.add_degradation(
                Degradation(
                    stage="encode",
                    category="skip_failed_encoded_feature",
                    triggered_by=record.code,
                    feature=feature.name,
                )
            )
            failed.append(feature.name)
            continue

        if feature.name not in (rows[0] if rows else {}):
            record = ErrorRecord(
                stage="encode",
                code=E_CONFIG_INVALID,
                message=f"category column {feature.name!r} is missing from the sample",
                feature=feature.name,
                details={"field": "category_features.name"},
            )
            diagnostics.add_error(record)
            diagnostics.add_degradation(
                Degradation(
                    stage="encode",
                    category="skip_failed_encoded_feature",
                    triggered_by=record.code,
                    feature=feature.name,
                )
            )
            failed.append(feature.name)
            continue

        projection_columns = [feature.name]
        if feature.encoding == "target":
            projection_columns.append(cfg.label_column)
        projection_digest = sample_content_digest(
            rows,
            id_column=cfg.id_column,
            columns=projection_columns,
        )
        components = {
            **_cache_components(
                cfg=cfg,
                injection=injection,
                projection_digest=projection_digest,
                slots_digest=sha256_obj(
                    {"scope": "encode", "cross_slot_dependencies": []}
                ),
                slots_digest_scope="encode_has_no_cross_slot_dependency",
            ),
            "feature_config_digest": cfg.stage_feature_digest("encode", feature.name),
            "encoding": feature.encoding,
        }
        key_info = build_cache_key(stage="encode", feature=feature.name, components=components)
        payload: dict[str, Any] | None = None
        lookup: CacheLookup | None = None
        try:
            payload, lookup = cache.get(stage="encode", feature=feature.name, key_info=key_info)
        except BusinessError as exc:
            record = _corrupt_record(exc, fallback_feature=feature.name)
            diagnostics.add_error(record)
            lookup = CacheLookup(
                status="corrupt",
                stage="encode",
                feature=feature.name,
                cache_key=key_info["cache_key"],
                content_hash=None,
                components=key_info["components"],
            )

        if payload is not None and lookup is not None:
            try:
                columns, matrix, params = _load_matrix_payload(
                    payload,
                    rows=n_rows,
                    feature=feature.name,
                    stage="encode",
                )
            except BusinessError as exc:
                diagnostics.add_error(exc.record)
                lookup = CacheLookup(
                    status="corrupt",
                    stage="encode",
                    feature=feature.name,
                    cache_key=key_info["cache_key"],
                    content_hash=lookup.content_hash,
                    components=key_info["components"],
                )
            else:
                diagnostics.add_lookup(lookup)
                blocks.append(
                    FeatureBlock(
                        stage="encode",
                        logical_feature=feature.name,
                        columns=columns,
                        matrix=matrix,
                        params=params,
                        cache_lookup=lookup,
                    )
                )
                continue

        if feature.encoding == "one-hot":
            columns, matrix, params = _one_hot_encoding(rows, feature)
        else:
            columns, matrix, params = _target_encoding(rows, cfg, feature)
        stored = cache.put(
            stage="encode",
            feature=feature.name,
            key_info=key_info,
            payload={
                "kind": "encoded_feature",
                "columns": columns,
                "matrix": matrix_to_base64(matrix),
                "params": params,
            },
        )
        status = "miss" if lookup is None or lookup.status == "miss" else "corrupt_recomputed"
        reported = CacheLookup(
            status=status,
            stage="encode",
            feature=feature.name,
            cache_key=stored.cache_key,
            content_hash=stored.content_hash,
            components=stored.components,
        )
        diagnostics.add_lookup(reported)
        blocks.append(
            FeatureBlock(
                stage="encode",
                logical_feature=feature.name,
                columns=columns,
                matrix=matrix,
                params=params,
                cache_lookup=reported,
            )
        )

    return StageBlocks(
        blocks=blocks,
        failed_features=failed,
        errors=diagnostics.errors[stage_errors_before:],
    )


def standardize_numeric(
    rows: Sequence[dict[str, Any]],
    cfg: FeatureConfig,
    cache: ContentAddressedCache,
    injection: InjectionConfig,
    diagnostics: Diagnostics,
) -> StageBlocks:
    injection.raise_if_unclassified("standardize")
    blocks: list[FeatureBlock] = []
    failed: list[str] = []
    stage_errors_before = len(diagnostics.errors)
    n_rows = len(rows)

    for feature in cfg.numeric_features:
        actions = injection.for_stage("standardize", feature=feature.name)
        injected_error = next(
            (action for action in actions if action.kind in {"non_finite_numeric", "numeric_value_out_of_range"}),
            None,
        )
        if injected_error is not None:
            record = injected_error.to_error()
            diagnostics.add_error(record)
            diagnostics.add_degradation(
                Degradation(
                    stage="standardize",
                    category="skip_failed_standardized_feature",
                    triggered_by=record.code,
                    feature=feature.name,
                )
            )
            failed.append(feature.name)
            continue

        projection_digest = sample_content_digest(
            rows,
            id_column=cfg.id_column,
            columns=[feature.name],
        )
        components = {
            **_cache_components(
                cfg=cfg,
                injection=injection,
                projection_digest=projection_digest,
                slots_digest=sha256_obj(
                    {"scope": "standardize", "cross_slot_dependencies": []}
                ),
                slots_digest_scope="standardization_has_no_cross_slot_dependency",
            ),
            "feature_config_digest": cfg.stage_feature_digest("standardize", feature.name),
            "standardization": feature.standardization,
        }
        key_info = build_cache_key(
            stage="standardize",
            feature=feature.name,
            components=components,
        )
        payload: dict[str, Any] | None = None
        lookup: CacheLookup | None = None
        try:
            payload, lookup = cache.get(
                stage="standardize",
                feature=feature.name,
                key_info=key_info,
            )
        except BusinessError as exc:
            diagnostics.add_error(_corrupt_record(exc, fallback_feature=feature.name))
            lookup = CacheLookup(
                status="corrupt",
                stage="standardize",
                feature=feature.name,
                cache_key=key_info["cache_key"],
                content_hash=None,
                components=key_info["components"],
            )

        if payload is not None and lookup is not None:
            try:
                columns, matrix, params = _load_matrix_payload(
                    payload,
                    rows=n_rows,
                    feature=feature.name,
                    stage="standardize",
                )
            except BusinessError as exc:
                diagnostics.add_error(exc.record)
                lookup = CacheLookup(
                    status="corrupt",
                    stage="standardize",
                    feature=feature.name,
                    cache_key=key_info["cache_key"],
                    content_hash=lookup.content_hash,
                    components=key_info["components"],
                )
            else:
                diagnostics.add_lookup(lookup)
                blocks.append(
                    FeatureBlock(
                        stage="standardize",
                        logical_feature=feature.name,
                        columns=columns,
                        matrix=matrix,
                        params=params,
                        cache_lookup=lookup,
                    )
                )
                continue

        raw_values: list[float] = []
        value_error: ErrorRecord | None = None
        for row in rows:
            if feature.name not in row:
                value_error = ErrorRecord(
                    stage="standardize",
                    code=E_STANDARDIZE_VALUE_INVALID,
                    message=f"numeric column {feature.name!r} is missing",
                    feature=feature.name,
                    row_id=str(row.get(cfg.id_column)),
                    details={"field": feature.name},
                )
                break
            raw = row[feature.name]
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                value_error = ErrorRecord(
                    stage="standardize",
                    code=E_STANDARDIZE_VALUE_INVALID,
                    message=f"numeric column {feature.name!r} contains a non-numeric value",
                    feature=feature.name,
                    row_id=str(row.get(cfg.id_column)),
                    details={"value": raw},
                )
                break
            value = float(raw)
            if not math.isfinite(value):
                value_error = ErrorRecord(
                    stage="standardize",
                    code=E_STANDARDIZE_VALUE_INVALID,
                    message=f"numeric column {feature.name!r} contains a non-finite value",
                    feature=feature.name,
                    row_id=str(row.get(cfg.id_column)),
                    details={"value": raw},
                )
                break
            raw_values.append(value)

        if value_error is not None:
            diagnostics.add_error(value_error)
            diagnostics.add_degradation(
                Degradation(
                    stage="standardize",
                    category="skip_failed_standardized_feature",
                    triggered_by=value_error.code,
                    feature=feature.name,
                )
            )
            failed.append(feature.name)
            continue

        values = np.asarray(raw_values, dtype=np.float64)
        column = f"num__{feature.name}"
        if feature.standardization == "z-score":
            center = float(np.mean(values))
            spread = float(np.std(values, ddof=0))
            scale = spread if spread != 0.0 else 1.0
            transformed = (values - center) / scale
            params = {
                "standardization": "z-score",
                "formula": "(x - mean) / std; ddof=0",
                "mean": center,
                "std": spread,
                "scale": scale,
                "constant": spread == 0.0,
            }
        else:
            minimum = float(np.min(values))
            maximum = float(np.max(values))
            spread = maximum - minimum
            scale = spread if spread != 0.0 else 1.0
            transformed = (values - minimum) / scale
            params = {
                "standardization": "min-max",
                "formula": "(x - min) / (max - min), constant maps to 0",
                "min": minimum,
                "max": maximum,
                "range": spread,
                "scale": scale,
                "constant": spread == 0.0,
            }
        matrix = transformed.reshape(-1, 1)
        stored = cache.put(
            stage="standardize",
            feature=feature.name,
            key_info=key_info,
            payload={
                "kind": "standardized_feature",
                "columns": [column],
                "matrix": matrix_to_base64(matrix),
                "params": params,
            },
        )
        status = "miss" if lookup is None or lookup.status == "miss" else "corrupt_recomputed"
        reported = CacheLookup(
            status=status,
            stage="standardize",
            feature=feature.name,
            cache_key=stored.cache_key,
            content_hash=stored.content_hash,
            components=stored.components,
        )
        diagnostics.add_lookup(reported)
        blocks.append(
            FeatureBlock(
                stage="standardize",
                logical_feature=feature.name,
                columns=[column],
                matrix=matrix,
                params=params,
                cache_lookup=reported,
            )
        )

    return StageBlocks(
        blocks=blocks,
        failed_features=failed,
        errors=diagnostics.errors[stage_errors_before:],
    )


def _cross_values(rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> list[tuple[str, ...]]:
    return [tuple(_categorical_text(row[column]) for column in columns) for row in rows]


def _cross_columns_for_combination(
    slot: Any,
    combination: tuple[str, ...],
) -> str:
    encoded_values = slot.value_separator.join(quote(value, safe="") for value in combination)
    return (
        f"{slot.column_prefix}{slot.name_separator}{slot.slot_id}"
        f"{slot.name_separator}{encoded_values}"
    )


def expand_cross_slots(
    rows: Sequence[dict[str, Any]],
    cfg: FeatureConfig,
    cache: ContentAddressedCache,
    injection: InjectionConfig,
    diagnostics: Diagnostics,
    *,
    failed_encoded_features: Sequence[str],
) -> StageBlocks:
    injection.raise_if_unclassified("cross")
    n_rows = len(rows)
    encode_codes = [
        error.code
        for error in diagnostics.errors
        if error.stage == "encode" and error.code != E_CACHE_CORRUPT
    ]
    encode_failure_code = encode_codes[-1] if encode_codes else "E_ENCODE_STAGE_FAILURE"
    blocks: list[FeatureBlock] = []
    failed: list[str] = []
    stage_errors_before = len(diagnostics.errors)
    categories = {feature.name for feature in cfg.category_features}
    available_columns = set(rows[0]) if rows else set()

    for slot in cfg.cross_slots:
        blocked_by = sorted(set(slot.columns) & set(failed_encoded_features))
        if blocked_by:
            diagnostics.add_degradation(
                Degradation(
                    stage="cross",
                    category="skip_cross_slot_due_to_encode_failure",
                    triggered_by=encode_failure_code,
                    feature=slot.slot_id,
                    details={"failed_encoded_features": blocked_by},
                )
            )
            failed.append(slot.slot_id)
            continue
        actions = injection.for_stage("cross", feature=slot.slot_id)
        injected_error = next(
            (
                action
                for action in actions
                if action.kind in {"missing_slot_column", "duplicate_slot"}
            ),
            None,
        )
        if injected_error is not None:
            record = injected_error.to_error()
            diagnostics.add_error(record)
            diagnostics.add_degradation(
                Degradation(
                    stage="cross",
                    category="skip_invalid_cross_slot",
                    triggered_by=record.code,
                    feature=slot.slot_id,
                )
            )
            failed.append(slot.slot_id)
            continue

        missing = [column for column in slot.columns if column not in available_columns]
        non_category = [column for column in slot.columns if column in available_columns and column not in categories]
        if missing or non_category:
            record = ErrorRecord(
                stage="cross",
                code=E_SLOT_REF_INVALID,
                message=f"cross slot {slot.slot_id} references invalid columns",
                feature=slot.slot_id,
                details={
                    "missing_columns": missing,
                    "non_category_columns": non_category,
                    "slot_columns": list(slot.columns),
                },
            )
            diagnostics.add_error(record)
            diagnostics.add_degradation(
                Degradation(
                    stage="cross",
                    category="skip_invalid_cross_slot",
                    triggered_by=record.code,
                    feature=slot.slot_id,
                )
            )
            failed.append(slot.slot_id)
            continue

        projection_digest = sample_content_digest(
            rows,
            id_column=cfg.id_column,
            columns=list(slot.columns),
        )
        components = {
            **_cache_components(
                cfg=cfg,
                injection=injection,
                projection_digest=projection_digest,
                slots_digest=sha256_obj(
                    {"scope": "cross", "slot": slot.raw}
                ),
                slots_digest_scope="single_cross_slot_definition",
            ),
            "slot_config_digest": cfg.stage_feature_digest("cross", slot.slot_id),
            "representation": slot.representation,
        }
        key_info = build_cache_key(stage="cross", feature=slot.slot_id, components=components)
        payload: dict[str, Any] | None = None
        lookup: CacheLookup | None = None
        try:
            payload, lookup = cache.get(stage="cross", feature=slot.slot_id, key_info=key_info)
        except BusinessError as exc:
            diagnostics.add_error(_corrupt_record(exc, fallback_feature=slot.slot_id))
            lookup = CacheLookup(
                status="corrupt",
                stage="cross",
                feature=slot.slot_id,
                cache_key=key_info["cache_key"],
                content_hash=None,
                components=key_info["components"],
            )

        if payload is not None and lookup is not None:
            try:
                columns, matrix, params = _load_matrix_payload(
                    payload,
                    rows=n_rows,
                    feature=slot.slot_id,
                    stage="cross",
                )
            except BusinessError as exc:
                diagnostics.add_error(exc.record)
                lookup = CacheLookup(
                    status="corrupt",
                    stage="cross",
                    feature=slot.slot_id,
                    cache_key=key_info["cache_key"],
                    content_hash=lookup.content_hash,
                    components=key_info["components"],
                )
            else:
                diagnostics.add_lookup(lookup)
                blocks.append(
                    FeatureBlock(
                        stage="cross",
                        logical_feature=slot.slot_id,
                        columns=columns,
                        matrix=matrix,
                        params=params,
                        cache_lookup=lookup,
                    )
                )
                continue

        combinations = _cross_values(rows, slot.columns)
        observed = sorted(set(combinations))
        if slot.representation == "one_hot":
            index = {combination: position for position, combination in enumerate(observed)}
            matrix = np.zeros((n_rows, len(observed)), dtype=np.float64)
            for row_index, combination in enumerate(combinations):
                matrix[row_index, index[combination]] = 1.0
            columns = [
                _cross_columns_for_combination(slot, combination) for combination in observed
            ]
            column_rule = (
                "<column_prefix><name_separator><slot_id><name_separator>"
                "<urlquote(value_1)><value_separator>...<urlquote(value_n)>"
            )
            combination_codes = {
                slot.value_separator.join(combination): code
                for code, combination in enumerate(observed)
            }
        else:
            index = {combination: position for position, combination in enumerate(observed)}
            matrix = np.asarray(
                [[float(index[combination])] for combination in combinations],
                dtype=np.float64,
            )
            column = (
                f"{slot.column_prefix}{slot.name_separator}{slot.slot_id}"
                f"{slot.name_separator}{slot.ordinal_suffix}"
            )
            columns = [column]
            column_rule = (
                "<column_prefix><name_separator><slot_id><name_separator><ordinal_suffix>"
            )
            combination_codes = {
                slot.value_separator.join(combination): code
                for code, combination in enumerate(observed)
            }

        memory_estimate = int(n_rows * len(columns) * np.dtype(np.float64).itemsize)
        params = {
            "representation": slot.representation,
            "columns": columns,
            "column_rule": column_rule,
            "name_separator": slot.name_separator,
            "value_separator": slot.value_separator,
            "column_prefix": slot.column_prefix,
            "slot_columns": list(slot.columns),
            "observed_combinations": [list(combination) for combination in observed],
            "combination_codes": combination_codes,
            "column_count": len(columns),
            "memory_estimate_bytes": memory_estimate,
            "memory_formula": "rows * columns * 8 bytes (dense float64)",
        }
        stored = cache.put(
            stage="cross",
            feature=slot.slot_id,
            key_info=key_info,
            payload={
                "kind": "cross_feature_set",
                "columns": columns,
                "matrix": matrix_to_base64(matrix),
                "params": params,
            },
        )
        status = "miss" if lookup is None or lookup.status == "miss" else "corrupt_recomputed"
        reported = CacheLookup(
            status=status,
            stage="cross",
            feature=slot.slot_id,
            cache_key=stored.cache_key,
            content_hash=stored.content_hash,
            components=stored.components,
        )
        diagnostics.add_lookup(reported)
        blocks.append(
            FeatureBlock(
                stage="cross",
                logical_feature=slot.slot_id,
                columns=columns,
                matrix=matrix,
                params=params,
                cache_lookup=reported,
            )
        )

    return StageBlocks(
        blocks=blocks,
        failed_features=failed,
        errors=diagnostics.errors[stage_errors_before:],
    )


def matrix_version(matrix: np.ndarray, columns: Sequence[str]) -> str:
    document = canonical_bytes({"columns": list(columns)})
    return sha256_bytes(b"table-feature-matrix-v1\0" + document + b"\0" + matrix_to_bytes(matrix))


def _variance_scores(matrix: np.ndarray, columns: Sequence[str]) -> tuple[dict[str, float], dict[str, Any]]:
    values = np.var(matrix, axis=0, ddof=0) if matrix.shape[1] else np.asarray([], dtype=np.float64)
    scores = {column: float(values[index]) for index, column in enumerate(columns)}
    return scores, {"formula": VARIANCE_FORMULA, "ddof": 0}


def _digitize(values: np.ndarray, bins: int) -> tuple[np.ndarray, list[float], int]:
    if values.size == 0 or np.all(values == values[0]):
        return np.zeros(values.shape[0], dtype=np.int64), [], 1
    quantiles = np.quantile(values, np.linspace(0.0, 1.0, bins + 1), method="linear")
    internal_edges = np.unique(quantiles[1:-1])
    if internal_edges.size == 0:
        return np.zeros(values.shape[0], dtype=np.int64), [], 1
    codes = np.digitize(values, internal_edges, right=False).astype(np.int64)
    return codes, [float(edge) for edge in internal_edges], int(internal_edges.size + 1)


def _mutual_info_scores(
    matrix: np.ndarray,
    columns: Sequence[str],
    rows: Sequence[dict[str, Any]],
    cfg: FeatureConfig,
) -> tuple[dict[str, float], dict[str, Any]]:
    raw_labels = [row[cfg.label_column] for row in rows]
    classes = sorted({_categorical_text(value) for value in raw_labels})
    class_index = {value: index for index, value in enumerate(classes)}
    y = np.asarray([class_index[_categorical_text(value)] for value in raw_labels], dtype=np.int64)
    scores: dict[str, float] = {}
    bin_details: dict[str, Any] = {}
    n_rows = matrix.shape[0]
    for column_index, column in enumerate(columns):
        codes, edges, bin_count = _digitize(matrix[:, column_index], cfg.selector.bins)
        bin_details[column] = {
            "bin_edges": edges,
            "effective_bin_count": bin_count,
        }
        joint = np.zeros((bin_count, len(classes)), dtype=np.float64)
        np.add.at(joint, (codes, y), 1.0)
        pxy = joint / n_rows
        px = pxy.sum(axis=1)
        py = pxy.sum(axis=0)
        positive = joint > 0
        mi = 0.0
        for x_index, y_index in zip(*np.nonzero(positive), strict=True):
            probability = pxy[x_index, y_index]
            mi += probability * math.log(
                probability / (px[x_index] * py[y_index])
            )
        scores[column] = max(0.0, float(mi))
    context = {
        "formula": MI_FORMULA,
        "requested_bins": cfg.selector.bins,
        "binning": "linear quantile edges; np.digitize(right=False)",
        "label_classes": classes,
        "bins": bin_details,
    }
    return scores, context


def _record_select_injection(
    action: Any,
    diagnostics: Diagnostics,
) -> str:
    feature = action.feature
    if feature is None:
        if action.kind == "threshold_out_of_bounds":
            feature = "selector.threshold"
        elif action.kind == "bins_invalid":
            feature = "selector.bins"
    record = ErrorRecord(
        stage="select",
        code=action.code,
        message=action.to_error().message,
        feature=feature,
        row_id=action.row_id,
        details=action.to_error().details,
    )
    diagnostics.add_error(record)
    return record.code


def _rank_scores(
    scores: dict[str, float],
    *,
    eligible: list[str],
) -> list[str]:
    return sorted(eligible, key=lambda name: (-scores[name], name))


def _replay_error(value: Any, *, fallback_stage: str) -> ErrorRecord | None:
    if not isinstance(value, dict):
        return None
    stage = value.get("stage")
    code = value.get("code")
    message = value.get("message")
    if not isinstance(stage, str) or not isinstance(code, str) or not isinstance(message, str):
        return None
    return ErrorRecord(
        stage=stage or fallback_stage,
        code=code,
        message=message,
        feature=value.get("feature") if isinstance(value.get("feature"), str) else None,
        row_id=value.get("row_id") if isinstance(value.get("row_id"), str) else None,
        details=value.get("details") if isinstance(value.get("details"), dict) else {},
    )


def _replay_degradation(value: Any) -> Degradation | None:
    if not isinstance(value, dict):
        return None
    stage = value.get("stage")
    category = value.get("category")
    triggered_by = value.get("triggered_by")
    if (
        not isinstance(stage, str)
        or not isinstance(category, str)
        or not isinstance(triggered_by, str)
    ):
        return None
    return Degradation(
        stage=stage,
        category=category,
        triggered_by=triggered_by,
        feature=value.get("feature") if isinstance(value.get("feature"), str) else None,
        details=value.get("details") if isinstance(value.get("details"), dict) else {},
    )


def select_features(
    rows: Sequence[dict[str, Any]],
    cfg: FeatureConfig,
    blocks: Sequence[FeatureBlock],
    cache: ContentAddressedCache,
    injection: InjectionConfig,
    diagnostics: Diagnostics,
    *,
    upstream_codes: Sequence[str],
) -> SelectionOutcome:
    injection.raise_if_unclassified("select")
    if upstream_codes:
        diagnostics.add_degradation(
            Degradation(
                stage="select",
                category="use_available_feature_matrix",
                triggered_by=upstream_codes[0],
                details={"upstream_codes": list(dict.fromkeys(upstream_codes))},
            )
        )
    select_error_start = len(diagnostics.errors)
    select_degradation_start = len(diagnostics.degradations)

    n_rows = len(rows)
    matrices = [block.matrix for block in blocks if block.matrix.shape[1] > 0]
    matrix = (
        np.concatenate(matrices, axis=1)
        if matrices
        else np.zeros((n_rows, 0), dtype=np.float64)
    )
    columns: list[str] = []
    for block in blocks:
        columns.extend(block.columns)
    seen: set[str] = set()
    duplicate_columns: list[str] = []
    keep_indices: list[int] = []
    for index, column in enumerate(columns):
        if column in seen:
            duplicate_columns.append(column)
        else:
            seen.add(column)
            keep_indices.append(index)
    if duplicate_columns:
        diagnostics.add_error(
            ErrorRecord(
                stage="select",
                code=E_CONFIG_INVALID,
                message="encoded feature matrix contains duplicate column names",
                feature=duplicate_columns[0],
                details={"duplicate_columns": duplicate_columns},
            )
        )
        matrix = matrix[:, keep_indices]
        columns = [columns[index] for index in keep_indices]

    version = matrix_version(matrix, columns)
    select_actions = injection.for_stage(
        "select",
        kinds={"threshold_out_of_bounds", "bins_invalid"},
    )
    select_error_codes: list[str] = []
    for action in select_actions:
        select_error_codes.append(_record_select_injection(action, diagnostics))

    all_fields = [
        key for key in sorted(set().union(*(row.keys() for row in rows))) if key != cfg.id_column
    ]
    full_sample_digest = sample_content_digest(
        rows,
        id_column=cfg.id_column,
        columns=all_fields,
    )
    components = {
        **_cache_components(
            cfg=cfg,
            injection=injection,
            projection_digest=full_sample_digest,
            slots_digest=cfg.slots_digest,
            slots_digest_scope="all_cross_slots_affect_selection_matrix",
            include_selection_version=True,
        ),
        "matrix_version": version,
        "selector_config_digest": sha256_obj(cfg.selector.raw),
        "available_columns_digest": sha256_obj(columns),
    }
    key_info = build_cache_key(stage="select", feature="_selection", components=components)
    payload: dict[str, Any] | None = None
    lookup: CacheLookup | None = None

    if not select_error_codes:
        try:
            payload, lookup = cache.get(stage="select", feature="_selection", key_info=key_info)
        except BusinessError as exc:
            diagnostics.add_error(_corrupt_record(exc, fallback_feature="_selection"))
            lookup = CacheLookup(
                status="corrupt",
                stage="select",
                feature="_selection",
                cache_key=key_info["cache_key"],
                content_hash=None,
                components=key_info["components"],
            )

    if payload is not None and lookup is not None:
        cached_selected = payload.get("selected_features")
        cached_scores = payload.get("scores")
        cached_context = payload.get("score_context")
        if (
            not isinstance(cached_selected, list)
            or any(not isinstance(name, str) or name not in columns for name in cached_selected)
            or not isinstance(cached_scores, dict)
            or set(cached_scores) != set(columns)
            or not isinstance(cached_context, dict)
            or payload.get("matrix_version") != version
        ):
            diagnostics.add_error(
                ErrorRecord(
                    stage="select",
                    code=E_CACHE_CORRUPT,
                    message="cached selection payload does not match the current matrix",
                    feature="_selection",
                    details={"cache_key": key_info["cache_key"]},
                )
            )
            lookup = CacheLookup(
                status="corrupt",
                stage="select",
                feature="_selection",
                cache_key=key_info["cache_key"],
                content_hash=lookup.content_hash,
                components=key_info["components"],
            )
        else:
            cached_errors = payload.get("selection_errors", [])
            cached_degradations = payload.get("selection_degradations", [])
            if isinstance(cached_errors, list):
                for value in cached_errors:
                    record = _replay_error(value, fallback_stage="select")
                    if record is not None:
                        diagnostics.add_error(record)
            if isinstance(cached_degradations, list):
                for value in cached_degradations:
                    degradation = _replay_degradation(value)
                    if degradation is not None:
                        diagnostics.add_degradation(degradation)
            diagnostics.add_lookup(lookup)
            return SelectionOutcome(
                selected_features=list(cached_selected),
                scores={name: float(cached_scores[name]) for name in columns},
                matrix_version=version,
                method=str(payload.get("method")),
                mode=str(payload.get("mode")),
                threshold=(
                    None if payload.get("threshold") is None else float(payload["threshold"])
                ),
                target_count=(
                    None
                    if payload.get("target_count") is None
                    else int(payload["target_count"])
                ),
                score_context=cached_context,
                cache_lookup=lookup,
                degradation_category=payload.get("degradation_category"),
            )

    fallback_reason = select_error_codes[0] if select_error_codes else None
    if fallback_reason is not None:
        diagnostics.add_degradation(
            Degradation(
                stage="select",
                category="fallback_variance_default",
                triggered_by=fallback_reason,
                feature="selector",
                details={"default_variance_threshold": cfg.selector.default_variance_threshold},
            )
        )
        effective_method = "variance"
        effective_mode = "threshold"
        effective_threshold = cfg.selector.default_variance_threshold
        effective_target = None
        scores, score_context = _variance_scores(matrix, columns)
        score_context["fallback"] = True
    else:
        effective_method = cfg.selector.type
        effective_mode = cfg.selector.mode
        effective_threshold = (
            cfg.selector.default_variance_threshold
            if effective_method == "variance" and cfg.selector.threshold is None
            else cfg.selector.threshold
        )
        effective_target = cfg.selector.target_count
        if effective_method == "variance":
            scores, score_context = _variance_scores(matrix, columns)
        else:
            scores, score_context = _mutual_info_scores(matrix, columns, rows, cfg)

    threshold = 0.0 if effective_threshold is None else float(effective_threshold)
    if effective_method == "variance" or effective_mode == "threshold":
        eligible = [column for column in columns if scores[column] >= threshold]
    else:
        eligible = list(columns)

    degradation_category: str | None = None
    if effective_mode == "top_k":
        target_count = 1 if effective_target is None else int(effective_target)
        if target_count > len(eligible):
            record = ErrorRecord(
                stage="select",
                code=E_SELECT_TARGET_INVALID,
                message=(
                    f"target_count {target_count} exceeds the {len(eligible)} available features"
                ),
                feature="selector.target_count",
                details={
                    "target_count": target_count,
                    "available_feature_count": len(eligible),
                    "threshold": threshold,
                },
            )
            diagnostics.add_error(record)
            diagnostics.add_degradation(
                Degradation(
                    stage="select",
                    category="fallback_available_features",
                    triggered_by=record.code,
                    feature="selector.target_count",
                    details={"selected_count": len(eligible)},
                )
            )
            degradation_category = "fallback_available_features"
            selected = _rank_scores(scores, eligible=eligible)
        else:
            selected = _rank_scores(scores, eligible=eligible)[:target_count]
    else:
        selected = _rank_scores(scores, eligible=eligible)

    if not set(selected).issubset(columns):
        raise AssertionError("selection produced columns outside the encoded matrix")

    if fallback_reason is not None:
        degradation_category = degradation_category or "fallback_variance_default"

    selection_payload = {
        "kind": "selection_result",
        "matrix_version": version,
        "columns": columns,
        "selected_features": selected,
        "scores": {column: float(scores[column]) for column in columns},
        "method": effective_method,
        "mode": effective_mode,
        "threshold": None if effective_method == "mutual_info" and effective_mode != "threshold" else threshold,
        "target_count": effective_target,
        "score_context": score_context,
        "tie_break": "score_desc_then_feature_name_asc",
        "degradation_category": degradation_category,
        "requested_selector": cfg.selector.raw,
        "selection_errors": [
            error.to_dict()
            for error in diagnostics.errors[select_error_start:]
            if error.stage == "select"
        ],
        "selection_degradations": [
            degradation.to_dict()
            for degradation in diagnostics.degradations[select_degradation_start:]
            if degradation.stage == "select"
        ],
    }
    if select_error_codes:
        stored_content_hash = None
        stored_key = key_info["cache_key"]
        reported = CacheLookup(
            status="bypassed_injection",
            stage="select",
            feature="_selection",
            cache_key=stored_key,
            content_hash=stored_content_hash,
            components=key_info["components"],
        )
    else:
        stored = cache.put(
            stage="select",
            feature="_selection",
            key_info=key_info,
            payload=selection_payload,
        )
        status = "miss" if lookup is None or lookup.status == "miss" else "corrupt_recomputed"
        reported = CacheLookup(
            status=status,
            stage="select",
            feature="_selection",
            cache_key=stored.cache_key,
            content_hash=stored.content_hash,
            components=stored.components,
        )
    diagnostics.add_lookup(reported)

    for column in columns:
        score_components = {**components, "score_feature": column}
        score_key = build_cache_key(
            stage="selection_score",
            feature=column,
            components=score_components,
        )
        cache.put(
            stage="selection_score",
            feature=column,
            key_info=score_key,
            payload={
                "kind": "selection_score",
                "column": column,
                "score": float(scores[column]),
                "method": effective_method,
                "matrix_version": version,
                "score_context": score_context,
            },
        )

    return SelectionOutcome(
        selected_features=selected,
        scores={column: float(scores[column]) for column in columns},
        matrix_version=version,
        method=effective_method,
        mode=effective_mode,
        threshold=selection_payload["threshold"],
        target_count=effective_target,
        score_context=score_context,
        cache_lookup=reported,
        degradation_category=degradation_category,
    )

