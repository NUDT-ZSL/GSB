"""Input/config parsing plus strict sample validation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .canonical import file_sha256
from .errors import EngineError


@dataclass(frozen=True)
class Dataset:
    sample_ids: tuple[str, ...]
    features: np.ndarray
    groups: tuple[str, ...]
    injected: tuple[bool, ...]
    source_sha256: str

    @property
    def n_rows(self) -> int:
        return len(self.sample_ids)

    @property
    def n_features(self) -> int:
        return int(self.features.shape[1])

    def serializable(self) -> dict[str, Any]:
        return {
            "sample_ids": list(self.sample_ids),
            "features": self.features.tolist(),
            "groups": list(self.groups),
            "injected": list(self.injected),
        }


class _DuplicateKeyJSONError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate JSON key: {key}")
        self.key = key


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyJSONError(key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def parse_json_document(text: str, *, stage: str, source: str) -> Any:
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except _DuplicateKeyJSONError as exc:
        raise EngineError(
            "E_CONFIG_DUPLICATE_KEY",
            stage,
            f"{source} contains duplicate key {exc.key!r}",
            field=exc.key,
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise EngineError(
            "E_CONFIG_PARSE",
            stage,
            f"cannot parse {source}: {exc}",
            field=source,
        ) from exc


def load_json_document(path: str | Path, *, stage: str = "config") -> Any:
    source = str(path)
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise EngineError(
            "E_CONFIG_IO",
            stage,
            f"cannot read {source}: {exc}",
            field=source,
        ) from exc
    return parse_json_document(text, stage=stage, source=source)


def load_samples_jsonl(path: str | Path) -> list[dict[str, Any]]:
    source = str(path)
    records: list[dict[str, Any]] = []
    try:
        handle = open(path, "r", encoding="utf-8")
    except OSError as exc:
        raise EngineError(
            "E_INPUT_IO",
            "input",
            f"cannot read {source}: {exc}",
            field=source,
        ) from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_constant,
                )
            except _DuplicateKeyJSONError as exc:
                raise EngineError(
                    "E_CONFIG_DUPLICATE_KEY",
                    "input",
                    f"line {line_number} contains duplicate key {exc.key!r}",
                    field=exc.key,
                    details={"line": line_number},
                ) from exc
            except (json.JSONDecodeError, ValueError) as exc:
                raise EngineError(
                    "E_INPUT_PARSE",
                    "input",
                    f"invalid JSONL at line {line_number}: {exc}",
                    field="line",
                    details={"line": line_number},
                ) from exc
            if not isinstance(record, dict):
                raise EngineError(
                    "E_INPUT_SCHEMA",
                    "input",
                    f"line {line_number} must contain a JSON object",
                    field="record",
                    details={"line": line_number},
                )
            records.append(record)
    if not records:
        raise EngineError(
            "E_EMPTY_DATASET",
            "input",
            "sample file contains no records",
            field="sample_id",
        )
    return records


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_samples(
    records: Iterable[dict[str, Any]],
    *,
    source_sha256: str = "in-memory",
) -> Dataset:
    rows = list(records)
    if not rows:
        raise EngineError(
            "E_EMPTY_DATASET",
            "validation",
            "dataset is empty",
            field="sample_id",
        )

    seen: dict[str, int] = {}
    parsed: list[tuple[str, list[float], str, bool, int]] = []
    expected_dim: int | None = None

    for index, record in enumerate(rows):
        line = index + 1
        if not isinstance(record, dict):
            raise EngineError(
                "E_INPUT_SCHEMA",
                "validation",
                "sample must be an object",
                field="record",
                details={"line": line},
            )

        sample_id = record.get("sample_id")
        if not isinstance(sample_id, str) or not sample_id:
            raise EngineError(
                "E_INVALID_SAMPLE_ID",
                "validation",
                "sample_id must be a non-empty string",
                field="sample_id",
                details={"line": line},
            )
        if sample_id in seen:
            raise EngineError(
                "E_DUPLICATE_SAMPLE_ID",
                "validation",
                f"duplicate sample_id {sample_id!r}",
                field="sample_id",
                details={"line": line, "first_line": seen[sample_id], "sample_id": sample_id},
            )
        seen[sample_id] = line

        features = record.get("feature", record.get("features"))
        if not isinstance(features, list) or not features:
            raise EngineError(
                "E_INVALID_FEATURE_VECTOR",
                "validation",
                "feature/features must be a non-empty numeric list",
                field="feature",
                details={"line": line, "sample_id": sample_id},
            )
        if expected_dim is None:
            expected_dim = len(features)
        elif len(features) != expected_dim:
            raise EngineError(
                "E_FEATURE_DIM_MISMATCH",
                "validation",
                f"feature length {len(features)} differs from expected {expected_dim}",
                field="feature",
                details={
                    "line": line,
                    "sample_id": sample_id,
                    "expected": expected_dim,
                    "actual": len(features),
                },
            )

        numeric: list[float] = []
        for column, value in enumerate(features):
            if not _is_number(value) or not math.isfinite(float(value)):
                raise EngineError(
                    "E_INVALID_FEATURE_VALUE",
                    "validation",
                    "feature values must be finite numbers",
                    field=f"feature[{column}]",
                    details={"line": line, "sample_id": sample_id},
                )
            numeric.append(float(value))

        group = record.get("group", "")
        if not isinstance(group, str):
            raise EngineError(
                "E_INVALID_GROUP",
                "validation",
                "group must be a string",
                field="group",
                details={"line": line, "sample_id": sample_id},
            )

        marker = record.get("is_injected_anomaly")
        if not isinstance(marker, bool):
            raise EngineError(
                "E_INVALID_ANOMALY_MARKER",
                "validation",
                "is_injected_anomaly must be boolean",
                field="is_injected_anomaly",
                details={"line": line, "sample_id": sample_id},
            )

        parsed.append((sample_id, numeric, group, marker, line))

    parsed.sort(key=lambda item: item[0])
    sample_ids = tuple(item[0] for item in parsed)
    features = np.asarray([item[1] for item in parsed], dtype=np.float64)
    groups = tuple(item[2] for item in parsed)
    injected = tuple(item[3] for item in parsed)
    return Dataset(
        sample_ids=sample_ids,
        features=features,
        groups=groups,
        injected=injected,
        source_sha256=source_sha256,
    )


def load_dataset(path: str | Path) -> Dataset:
    source = str(path)
    records = load_samples_jsonl(path)
    return validate_samples(records, source_sha256=file_sha256(source))
