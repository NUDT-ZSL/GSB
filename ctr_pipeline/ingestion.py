from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    atomic_write_bytes,
    canonical_json_bytes,
    checksum,
    sha256_bytes,
    stable_bucket,
)
from .errors import (
    E_CONFIG_INVALID,
    E_DUP_SAMPLE,
    PipelineError,
)
from .models import (
    CrossSlot,
    ExpandedDataset,
    ExpandedSample,
    FeatureDictionary,
    FeatureSpec,
    SourceSample,
)


def _config_error(
    message: str,
    *,
    field: str,
    line_number: int | None = None,
    **extra: Any,
) -> PipelineError:
    details: dict[str, Any] = {"field": field, **extra}
    if line_number is not None:
        details["line_number"] = line_number
    return PipelineError(E_CONFIG_INVALID, "ingest", message, details)


def _iter_json_lines(path: str | Path) -> Iterable[tuple[int, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield line_number, json.loads(line)
            except json.JSONDecodeError as exc:
                raise _config_error(
                    f"invalid JSON: {exc.msg}",
                    field="json",
                    line_number=line_number,
                ) from exc


def load_feature_dictionary(
    path: str | Path,
    *,
    version: str = "dict-v1",
) -> FeatureDictionary:
    if not isinstance(version, str) or not version:
        raise _config_error("dictionary version must be a non-empty string", field="dictionary_version")

    entries: dict[int, FeatureSpec] = {}
    for line_number, row in _iter_json_lines(path):
        if not isinstance(row, dict):
            raise _config_error("feature dictionary row must be an object", field="row", line_number=line_number)
        for required in ("feature_index", "dimension", "type", "cardinality"):
            if required not in row:
                raise _config_error(
                    f"missing field {required}",
                    field=required,
                    line_number=line_number,
                )
        feature_index = row["feature_index"]
        dimension = row["dimension"]
        feature_type = row["type"]
        cardinality = row["cardinality"]
        if isinstance(feature_index, bool) or not isinstance(feature_index, int) or feature_index < 0:
            raise _config_error(
                "feature_index must be a non-negative integer",
                field="feature_index",
                line_number=line_number,
            )
        if isinstance(dimension, bool) or not isinstance(dimension, int) or dimension <= 0:
            raise _config_error(
                "dictionary dimension must be a positive integer",
                field="dimension",
                feature_index=feature_index,
                line_number=line_number,
            )
        if feature_type not in {"user", "item"}:
            raise _config_error(
                "feature type must be user or item",
                field="type",
                feature_index=feature_index,
                line_number=line_number,
            )
        if isinstance(cardinality, bool) or not isinstance(cardinality, int) or cardinality <= 0:
            raise _config_error(
                "cardinality must be a positive integer",
                field="cardinality",
                feature_index=feature_index,
                line_number=line_number,
            )
        row_version = row.get("dictionary_version")
        if row_version is not None and row_version != version:
            raise _config_error(
                "feature dictionary version mismatch",
                field="dictionary_version",
                feature_index=feature_index,
                expected=version,
                actual=row_version,
                line_number=line_number,
            )
        if feature_index in entries:
            raise _config_error(
                "duplicate feature_index",
                field="feature_index",
                feature_index=feature_index,
                line_number=line_number,
            )
        entries[feature_index] = FeatureSpec(
            feature_index=feature_index,
            dimension=dimension,
            feature_type=feature_type,
            cardinality=cardinality,
        )

    if not entries:
        raise _config_error("feature dictionary is empty", field="feature_index")
    ordered = [entries[index].to_dict() for index in sorted(entries)]
    return FeatureDictionary(
        entries=entries,
        version=version,
        checksum=checksum(ordered),
    )


def _as_sequence(value: Any, *, field: str, line_number: int) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise _config_error(
            f"{field} must be a list",
            field=field,
            line_number=line_number,
        )
    return list(value)


def _parse_feature_block(
    row: dict[str, Any],
    *,
    prefix: str,
    expected_type: str,
    dictionary: FeatureDictionary,
    line_number: int,
) -> tuple[tuple[int, ...], tuple[tuple[float, ...], ...]]:
    indices_key = f"{prefix}_indices"
    values_key = f"{prefix}_values"
    indices = _as_sequence(row.get(indices_key), field=indices_key, line_number=line_number)
    values = _as_sequence(row.get(values_key), field=values_key, line_number=line_number)
    if len(indices) != len(values):
        raise _config_error(
            f"{indices_key} and {values_key} length mismatch",
            field=values_key,
            index_count=len(indices),
            value_count=len(values),
            line_number=line_number,
        )
    if len(set(indices)) != len(indices):
        raise _config_error(
            f"{indices_key} contains duplicates",
            field=indices_key,
            line_number=line_number,
        )

    parsed_indices: list[int] = []
    parsed_values: list[tuple[float, ...]] = []
    for position, (feature_index, raw_values) in enumerate(zip(indices, values)):
        if isinstance(feature_index, bool) or not isinstance(feature_index, int):
            raise _config_error(
                "feature index must be an integer",
                field=indices_key,
                position=position,
                line_number=line_number,
            )
        spec = dictionary.entries.get(feature_index)
        if spec is None:
            raise _config_error(
                "feature index is absent from dictionary",
                field=indices_key,
                feature_index=feature_index,
                position=position,
                line_number=line_number,
            )
        if spec.feature_type != expected_type:
            raise _config_error(
                "feature index has the wrong feature type",
                field=indices_key,
                feature_index=feature_index,
                expected_type=expected_type,
                actual_type=spec.feature_type,
                position=position,
                line_number=line_number,
            )
        vector = _as_sequence(raw_values, field=values_key, line_number=line_number)
        if len(vector) != spec.dimension:
            raise _config_error(
                "feature value dimension is invalid",
                field=values_key,
                feature_index=feature_index,
                expected_dimension=spec.dimension,
                actual_dimension=len(vector),
                position=position,
                line_number=line_number,
            )
        numeric_vector: list[float] = []
        for value_position, value in enumerate(vector):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise _config_error(
                    "feature value must be numeric",
                    field=values_key,
                    feature_index=feature_index,
                    position=position,
                    value_position=value_position,
                    line_number=line_number,
                )
            numeric = float(value)
            if not math.isfinite(numeric):
                raise _config_error(
                    "feature value must be finite",
                    field=values_key,
                    feature_index=feature_index,
                    position=position,
                    value_position=value_position,
                    line_number=line_number,
                )
            numeric_vector.append(numeric)
        parsed_indices.append(feature_index)
        parsed_values.append(tuple(numeric_vector))
    return tuple(parsed_indices), tuple(parsed_values)


def _parse_cross_slots(
    raw: Any,
    *,
    user_indices: tuple[int, ...],
    item_indices: tuple[int, ...],
    dictionary: FeatureDictionary,
    max_cross_slots: int,
    line_number: int,
) -> tuple[CrossSlot, ...]:
    rows = _as_sequence(raw, field="cross_slots", line_number=line_number)
    slots: list[CrossSlot] = []
    seen_slots: set[int] = set()
    seen_combinations: set[tuple[int, int]] = set()
    user_set = set(user_indices)
    item_set = set(item_indices)
    for position, item in enumerate(rows):
        if not isinstance(item, dict):
            raise _config_error(
                "cross slot must be an object",
                field="cross_slots",
                position=position,
                line_number=line_number,
            )
        missing = [key for key in ("slot", "left", "right") if key not in item]
        if missing:
            raise _config_error(
                f"cross slot missing field {missing[0]}",
                field=f"cross_slots.{missing[0]}",
                position=position,
                line_number=line_number,
            )
        slot = item["slot"]
        left = item["left"]
        right = item["right"]
        if (
            isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 0
            or slot >= max_cross_slots
        ):
            raise _config_error(
                "cross slot index is invalid",
                field="cross_slots.slot",
                slot=slot,
                max_cross_slots=max_cross_slots,
                position=position,
                line_number=line_number,
            )
        if slot in seen_slots:
            raise _config_error(
                "duplicate cross slot",
                field="cross_slots.slot",
                slot=slot,
                position=position,
                line_number=line_number,
            )
        if left not in user_set:
            raise _config_error(
                "cross slot left feature is absent from user features",
                field="cross_slots.left",
                left=left,
                position=position,
                line_number=line_number,
            )
        if right not in item_set:
            raise _config_error(
                "cross slot right feature is absent from item features",
                field="cross_slots.right",
                right=right,
                position=position,
                line_number=line_number,
            )
        left_spec = dictionary.entries[left]
        right_spec = dictionary.entries[right]
        if left_spec.dimension != right_spec.dimension:
            raise _config_error(
                "cross slot feature dimensions differ",
                field="cross_slots",
                slot=slot,
                left_dimension=left_spec.dimension,
                right_dimension=right_spec.dimension,
                position=position,
                line_number=line_number,
            )
        combination = (left, right)
        if combination in seen_combinations:
            raise _config_error(
                "duplicate cross feature combination",
                field="cross_slots",
                left=left,
                right=right,
                position=position,
                line_number=line_number,
            )
        seen_slots.add(slot)
        seen_combinations.add(combination)
        slots.append(CrossSlot(slot=slot, left=left, right=right))
    slots.sort(key=lambda value: value.slot)
    return tuple(slots)


def load_source_samples(
    path: str | Path,
    dictionary: FeatureDictionary,
    *,
    max_cross_slots: int = 8,
) -> tuple[SourceSample, ...]:
    if isinstance(max_cross_slots, bool) or not isinstance(max_cross_slots, int) or max_cross_slots <= 0:
        raise _config_error(
            "max_cross_slots must be a positive integer",
            field="max_cross_slots",
        )
    samples: list[SourceSample] = []
    first_seen: dict[str, int] = {}
    for line_number, row in _iter_json_lines(path):
        if not isinstance(row, dict):
            raise _config_error("sample row must be an object", field="row", line_number=line_number)
        sample_id = row.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise _config_error(
                "sample_id must be a non-empty string",
                field="sample_id",
                line_number=line_number,
            )
        if sample_id in first_seen:
            raise PipelineError(
                E_DUP_SAMPLE,
                "ingest",
                "duplicate sample_id",
                {
                    "field": "sample_id",
                    "sample_id": sample_id,
                    "first_line_number": first_seen[sample_id],
                    "duplicate_line_number": line_number,
                },
            )
        first_seen[sample_id] = line_number

        label = row.get("label")
        if isinstance(label, bool) or not isinstance(label, int) or label not in (0, 1):
            raise _config_error(
                "label must be exactly 0 or 1",
                field="label",
                sample_id=sample_id,
                actual=label,
                line_number=line_number,
            )

        user_indices, user_values = _parse_feature_block(
            row,
            prefix="user",
            expected_type="user",
            dictionary=dictionary,
            line_number=line_number,
        )
        item_indices, item_values = _parse_feature_block(
            row,
            prefix="item",
            expected_type="item",
            dictionary=dictionary,
            line_number=line_number,
        )
        cross_slots = _parse_cross_slots(
            row.get("cross_slots", []),
            user_indices=user_indices,
            item_indices=item_indices,
            dictionary=dictionary,
            max_cross_slots=max_cross_slots,
            line_number=line_number,
        )
        samples.append(
            SourceSample(
                sample_id=sample_id,
                user_indices=user_indices,
                user_values=user_values,
                item_indices=item_indices,
                item_values=item_values,
                cross_slots=cross_slots,
                label=label,
                line_number=line_number,
            )
        )
    samples.sort(key=lambda sample: sample.sample_id)
    return tuple(samples)


def _add_bucket(accumulator: dict[int, float], bucket: int, value: float) -> None:
    accumulator[bucket] = accumulator.get(bucket, 0.0) + value


def expand_sample(
    sample: SourceSample,
    dictionary: FeatureDictionary,
    *,
    feature_dimension: int,
) -> ExpandedSample:
    if feature_dimension <= 0:
        raise _config_error(
            "feature_dimension must be a positive integer",
            field="feature_dimension",
            sample_id=sample.sample_id,
        )
    accumulator: dict[int, float] = {}

    user_values = dict(zip(sample.user_indices, sample.user_values))
    for feature_index in sorted(sample.user_indices):
        for part, value in enumerate(user_values[feature_index]):
            bucket = stable_bucket(f"user:{feature_index}:{part}", feature_dimension)
            _add_bucket(accumulator, bucket, value)

    item_values = dict(zip(sample.item_indices, sample.item_values))
    for feature_index in sorted(sample.item_indices):
        for part, value in enumerate(item_values[feature_index]):
            bucket = stable_bucket(f"item:{feature_index}:{part}", feature_dimension)
            _add_bucket(accumulator, bucket, value)

    for cross in sample.cross_slots:
        left_vector = user_values[cross.left]
        right_vector = item_values[cross.right]
        cross_value = 0.0
        for left_value, right_value in zip(left_vector, right_vector):
            cross_value += left_value * right_value
        bucket = stable_bucket(
            f"cross:{cross.slot}:{cross.left}:{cross.right}",
            feature_dimension,
        )
        _add_bucket(accumulator, bucket, cross_value)

    indices = tuple(sorted(index for index, value in accumulator.items() if value != 0.0))
    values = tuple(accumulator[index] for index in indices)
    return ExpandedSample(
        sample_id=sample.sample_id,
        label=sample.label,
        feature_dimension=feature_dimension,
        indices=indices,
        values=values,
    )


def canonical_expanded_bytes(samples: Iterable[ExpandedSample]) -> bytes:
    ordered = sorted(samples, key=lambda sample: sample.sample_id)
    return b"".join(canonical_json_bytes(sample.to_row()) + b"\n" for sample in ordered)


def expand_dataset(
    samples: Iterable[SourceSample],
    dictionary: FeatureDictionary,
    *,
    feature_dimension: int,
) -> ExpandedDataset:
    ordered = sorted(samples, key=lambda sample: sample.sample_id)
    expanded = tuple(
        expand_sample(sample, dictionary, feature_dimension=feature_dimension)
        for sample in ordered
    )
    data = canonical_expanded_bytes(expanded)
    return ExpandedDataset(
        samples=expanded,
        feature_dimension=feature_dimension,
        dictionary_version=dictionary.version,
        dictionary_checksum=dictionary.checksum,
        checksum=sha256_bytes(data),
    )


def write_expanded_dataset(path: str | Path, dataset: ExpandedDataset) -> str:
    data = canonical_expanded_bytes(dataset.samples)
    atomic_write_bytes(path, data)
    return sha256_bytes(data)


def load_expanded_dataset(
    path: str | Path,
    *,
    feature_dimension: int,
    dictionary_version: str,
    dictionary_checksum: str,
) -> ExpandedDataset:
    samples: list[ExpandedSample] = []
    for line_number, row in _iter_json_lines(path):
        if not isinstance(row, dict):
            raise _config_error(
                "expanded row must be an object",
                field="row",
                line_number=line_number,
            )
        sample_id = row.get("sample_id")
        label = row.get("label")
        indices = row.get("indices")
        values = row.get("values")
        if not isinstance(sample_id, str) or not sample_id:
            raise _config_error("invalid sample_id", field="sample_id", line_number=line_number)
        if isinstance(label, bool) or label not in (0, 1):
            raise _config_error("invalid label", field="label", line_number=line_number)
        if not isinstance(indices, list) or not isinstance(values, list) or len(indices) != len(values):
            raise _config_error("invalid sparse vector", field="indices", line_number=line_number)
        if indices != sorted(indices) or len(set(indices)) != len(indices):
            raise _config_error(
                "expanded indices must be unique and sorted",
                field="indices",
                line_number=line_number,
            )
        if any(not isinstance(index, int) or index < 0 or index >= feature_dimension for index in indices):
            raise _config_error(
                "expanded index out of range",
                field="indices",
                line_number=line_number,
            )
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in values
        ):
            raise _config_error(
                "expanded value is invalid",
                field="values",
                line_number=line_number,
            )
        samples.append(
            ExpandedSample(
                sample_id=sample_id,
                label=int(label),
                feature_dimension=feature_dimension,
                indices=tuple(int(index) for index in indices),
                values=tuple(float(value) for value in values),
            )
        )
    samples.sort(key=lambda sample: sample.sample_id)
    data = canonical_expanded_bytes(samples)
    return ExpandedDataset(
        samples=tuple(samples),
        feature_dimension=feature_dimension,
        dictionary_version=dictionary_version,
        dictionary_checksum=dictionary_checksum,
        checksum=sha256_bytes(data),
    )
