"""Deterministic feature assembly and byte-stable matrix serialization."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import E_DUP_REQUEST, E_FORMAT_INVALID, E_RESOURCE_LIMIT, fail
from .hashing import atomic_write_bytes, atomic_write_json, sha256_bytes, sha256_obj
from .models import FeatureSpec, Sample
from .specs import normalize_spec


MATRIX_MAGIC = b"CTRMAT1\x00"
MATRIX_DTYPE = "float64-le"
_MAX_ABS_STANDARDIZED = 8.0
_CROSS_HASH_BUCKETS = 64


@dataclass(frozen=True)
class AssembledRow:
    request_id: str
    timestamp_ms: int
    feature_names: tuple[str, ...]
    values: tuple[float, ...]
    details: tuple[dict[str, Any], ...]
    row_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "timestamp_ms": self.timestamp_ms,
            "feature_names": list(self.feature_names),
            "values": list(self.values),
            "details": list(self.details),
            "row_hash": self.row_hash,
        }


@dataclass(frozen=True)
class AssemblyResult:
    spec_version: int
    spec_checksum: str
    seed: int
    feature_names: tuple[str, ...]
    rows: tuple[AssembledRow, ...]
    matrix_bytes: bytes
    matrix_sha256: str
    summary: dict[str, Any]
    summary_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "spec_version": self.spec_version,
            "spec_checksum": self.spec_checksum,
            "seed": self.seed,
            "feature_names": list(self.feature_names),
            "matrix_sha256": self.matrix_sha256,
            "summary_sha256": self.summary_sha256,
            "summary": self.summary,
            "rows": [row.to_dict() for row in self.rows],
        }


def assembly_feature_names(spec: FeatureSpec) -> tuple[str, ...]:
    normalized = normalize_spec(spec)
    names: list[str] = []
    names.extend(f"numeric.{item.name}" for item in normalized.numeric)
    names.extend(f"categorical.{item.name}" for item in normalized.categorical)
    for item in normalized.cross:
        names.extend(f"cross.{item.name}.{slot}" for slot in item.slots)
    return tuple(sorted(names))


def assemble_features(
    samples: Iterable[Sample],
    spec: FeatureSpec,
    *,
    seed: int = 42,
    max_matrix_bytes: int | None = None,
) -> AssemblyResult:
    normalized = normalize_spec(spec)
    ordered = sorted(samples, key=lambda item: (item.request_id, item.timestamp_ms))
    seen: set[str] = set()
    for sample in ordered:
        if sample.request_id in seen:
            fail(
                E_DUP_REQUEST,
                f"duplicate request_id {sample.request_id!r}",
                field="request_id",
                request_id=sample.request_id,
            )
        seen.add(sample.request_id)
    feature_names = assembly_feature_names(normalized)
    estimated_bytes = len(ordered) * len(feature_names) * 8
    if max_matrix_bytes is not None and estimated_bytes > max_matrix_bytes:
        fail(
            E_RESOURCE_LIMIT,
            "assembled feature matrix exceeds the configured memory limit",
            resource="feature_matrix_bytes",
            estimated_bytes=estimated_bytes,
            limit_bytes=int(max_matrix_bytes),
            rows=len(ordered),
            columns=len(feature_names),
        )
    rows: list[AssembledRow] = []
    raw_values: list[tuple[float, ...]] = []
    for sample in ordered:
        row, values = _assemble_row(sample, normalized, feature_names, seed)
        rows.append(row)
        raw_values.append(values)
    matrix = encode_matrix(feature_names, raw_values)
    matrix_sha = sha256_bytes(matrix)
    summary = {
        "protocol": "deterministic_assembly_v1",
        "sort": ["request_id:asc", "timestamp_ms:asc", "request_id:asc"],
        "seed": int(seed),
        "spec_version": normalized.source_version,
        "spec_checksum": normalized.checksum,
        "feature_names": list(feature_names),
        "rows": [
            {
                "request_id": row.request_id,
                "timestamp_ms": row.timestamp_ms,
                "row_hash": row.row_hash,
            }
            for row in rows
        ],
        "matrix_sha256": matrix_sha,
        "matrix_dtype": MATRIX_DTYPE,
        "matrix_magic": MATRIX_MAGIC.decode("latin-1"),
        "shape": [len(rows), len(feature_names)],
    }
    summary_sha = sha256_obj(summary)
    return AssemblyResult(
        spec_version=normalized.source_version,
        spec_checksum=normalized.checksum,
        seed=int(seed),
        feature_names=feature_names,
        rows=tuple(rows),
        matrix_bytes=matrix,
        matrix_sha256=matrix_sha,
        summary=summary,
        summary_sha256=summary_sha,
    )


def _assemble_row(
    sample: Sample,
    spec: FeatureSpec,
    feature_names: tuple[str, ...],
    seed: int,
) -> tuple[AssembledRow, tuple[float, ...]]:
    values: dict[str, float] = {}
    details: list[dict[str, Any]] = []
    numeric_map = spec.numeric_map()
    categorical_map = spec.categorical_map()
    cross_map = spec.cross_map()
    for item in spec.numeric:
        feature = f"numeric.{item.name}"
        present = item.name in sample.numeric_feats
        if present:
            raw = sample.numeric_feats[item.name]
            strategy = "observed"
            imputed = False
        else:
            strategy = item.missing_strategy
            imputed = True
            if strategy == "error":
                fail(
                    E_FORMAT_INVALID,
                    f"missing numeric feature {item.name!r} with strategy 'error'",
                    field=f"numeric_feats.{item.name}",
                    request_id=sample.request_id,
                )
            if strategy == "mean":
                raw = item.mean
            elif strategy == "default":
                raw = item.default if item.default is not None else 0.0
            else:
                raw = 0.0
        encoded = _standardize(raw, item.mean, item.std)
        values[feature] = encoded
        details.append(
            {
                "feature": feature,
                "kind": "numeric",
                "source": "sample" if present else "missing_strategy",
                "missing_strategy": strategy,
                "imputed": imputed,
                "raw_value": raw,
                "encoded_value": encoded,
            }
        )
    for item in spec.categorical:
        feature = f"categorical.{item.name}"
        present = item.name in sample.categorical_feats
        if present:
            raw = sample.categorical_feats[item.name]
            strategy = "observed"
            imputed = False
        else:
            strategy = item.missing_strategy
            imputed = True
            if strategy == "error":
                fail(
                    E_FORMAT_INVALID,
                    f"missing categorical feature {item.name!r} with strategy 'error'",
                    field=f"categorical_feats.{item.name}",
                    request_id=sample.request_id,
                )
            raw = item.default if item.default is not None else "__MISSING__"
        bucket = _category_bucket(item.name, raw, int(item.buckets or 16), seed)
        encoded = bucket / max(1, int(item.buckets or 16) - 1)
        values[feature] = encoded
        details.append(
            {
                "feature": feature,
                "kind": "categorical",
                "source": "sample" if present else "missing_strategy",
                "missing_strategy": strategy,
                "imputed": imputed,
                "raw_value": raw,
                "bucket": bucket,
                "encoded_value": encoded,
            }
        )
    for item in spec.cross:
        left_value, left_present, left_raw = _cross_side(
            sample, numeric_map, categorical_map, item.left, seed
        )
        right_value, right_present, right_raw = _cross_side(
            sample, numeric_map, categorical_map, item.right, seed
        )
        present = left_present and right_present
        if not present:
            if item.missing_strategy == "error":
                fail(
                    E_FORMAT_INVALID,
                    f"missing side of cross feature {item.name!r} with strategy 'error'",
                    field=f"cross.{item.name}",
                    request_id=sample.request_id,
                )
            left_value = left_value if left_present else 0.0
            right_value = right_value if right_present else 0.0
        for slot in item.slots:
            feature = f"cross.{item.name}.{slot}"
            encoded = _cross_slot_value(
                slot,
                left_value,
                right_value,
                left_raw,
                right_raw,
                seed,
            )
            values[feature] = encoded
            details.append(
                {
                    "feature": feature,
                    "kind": "cross",
                    "source": "sample" if present else "missing_strategy",
                    "missing_strategy": "observed" if present else item.missing_strategy,
                    "imputed": not present,
                    "left": item.left,
                    "right": item.right,
                    "encoded_value": encoded,
                }
            )
    missing_names = [name for name in feature_names if name not in values]
    if missing_names:
        fail(
            E_FORMAT_INVALID,
            "internal assembly did not produce every declared feature slot",
            field="feature_names",
            missing=missing_names,
        )
    ordered_values = tuple(_clean_float(values[name]) for name in feature_names)
    detail_tuple = tuple(
        sorted(details, key=lambda detail: (detail["feature"], detail["kind"]))
    )
    row_hash = sha256_obj(
        {
            "request_id": sample.request_id,
            "timestamp_ms": sample.timestamp_ms,
            "feature_names": list(feature_names),
            "values": list(ordered_values),
        }
    )
    return (
        AssembledRow(
            request_id=sample.request_id,
            timestamp_ms=sample.timestamp_ms,
            feature_names=feature_names,
            values=ordered_values,
            details=detail_tuple,
            row_hash=row_hash,
        ),
        ordered_values,
    )


def _cross_side(
    sample: Sample,
    numeric_map: dict[str, Any],
    categorical_map: dict[str, Any],
    name: str,
    seed: int,
) -> tuple[float, bool, str | float | None]:
    if name in numeric_map:
        item = numeric_map[name]
        if name not in sample.numeric_feats:
            if item.missing_strategy == "error":
                fail(
                    E_FORMAT_INVALID,
                    f"missing numeric cross side {name!r} with strategy 'error'",
                    field=f"numeric_feats.{name}",
                    request_id=sample.request_id,
                )
            if item.missing_strategy == "mean":
                raw = item.mean
            elif item.missing_strategy == "default":
                raw = item.default if item.default is not None else 0.0
            else:
                raw = 0.0
            return _standardize(raw, item.mean, item.std), False, raw
        raw = sample.numeric_feats[name]
        return _standardize(raw, item.mean, item.std), True, raw
    if name in categorical_map:
        item = categorical_map[name]
        if name not in sample.categorical_feats:
            if item.missing_strategy == "error":
                fail(
                    E_FORMAT_INVALID,
                    f"missing categorical cross side {name!r} with strategy 'error'",
                    field=f"categorical_feats.{name}",
                    request_id=sample.request_id,
                )
            raw = item.default if item.default is not None else "__MISSING__"
            buckets = int(item.buckets or 16)
            bucket = _category_bucket(name, raw, buckets, seed)
            return bucket / max(1, buckets - 1), False, raw
        raw = sample.categorical_feats[name]
        buckets = int(item.buckets or 16)
        bucket = _category_bucket(name, raw, buckets, seed)
        return bucket / max(1, buckets - 1), True, raw
    fail(
        E_FORMAT_INVALID,
        f"cross references unknown feature {name!r}",
        field="cross",
    )


def _cross_slot_value(
    slot: str,
    left: float,
    right: float,
    left_raw: str | float | None,
    right_raw: str | float | None,
    seed: int = 0,
) -> float:
    if slot == "product":
        return _clean_float(left * right)
    if slot == "affinity":
        return _clean_float(1.0 / (1.0 + abs(left - right)))
    bucket = _category_bucket(
        "cross",
        f"{left_raw!r}|{right_raw!r}",
        _CROSS_HASH_BUCKETS,
        seed,
    )
    return bucket / (_CROSS_HASH_BUCKETS - 1)


def _standardize(raw: float, mean: float, std: float) -> float:
    value = (float(raw) - mean) / std
    return _clean_float(max(-_MAX_ABS_STANDARDIZED, min(_MAX_ABS_STANDARDIZED, value)))


def _category_bucket(name: str, value: str, buckets: int, seed: int) -> int:
    from .hashing import sha256_text

    digest = sha256_text(f"{seed}|{name}|{value}")
    return int(digest[:16], 16) % buckets


def _clean_float(value: float) -> float:
    if not math.isfinite(value):
        fail(
            E_FORMAT_INVALID,
            "assembled feature value is not finite",
            field="feature_value",
        )
    return 0.0 if value == 0.0 else float(value)


def encode_matrix(
    feature_names: tuple[str, ...], rows: Iterable[tuple[float, ...]]
) -> bytes:
    row_list = list(rows)
    header = (
        MATRIX_MAGIC
        + struct.pack("<I", len(feature_names))
        + struct.pack("<I", len(row_list))
    )
    payload = bytearray(header)
    for row in row_list:
        if len(row) != len(feature_names):
            fail(
                E_FORMAT_INVALID,
                "matrix row width does not match feature count",
                field="matrix",
            )
        payload.extend(struct.pack(f"<{len(row)}d", *row))
    return bytes(payload)


def decode_matrix(data: bytes) -> tuple[tuple[str, ...] | None, list[tuple[float, ...]]]:
    if not data.startswith(MATRIX_MAGIC) or len(data) < len(MATRIX_MAGIC) + 8:
        fail(E_FORMAT_INVALID, "invalid deterministic matrix header", field="matrix")
    offset = len(MATRIX_MAGIC)
    columns, rows = struct.unpack_from("<II", data, offset)
    offset += 8
    expected = offset + columns * rows * 8
    if len(data) != expected:
        fail(
            E_FORMAT_INVALID,
            "deterministic matrix length mismatch",
            field="matrix",
            expected_bytes=expected,
            actual_bytes=len(data),
        )
    result: list[tuple[float, ...]] = []
    for _ in range(rows):
        values = struct.unpack_from(f"<{columns}d", data, offset)
        offset += columns * 8
        result.append(values)
    return None, result


def write_assembly_artifacts(
    directory: str | Path,
    result: AssemblyResult,
    *,
    prefix: str = "assembly",
) -> dict[str, str]:
    root = Path(directory)
    matrix_path = root / f"{prefix}.matrix"
    summary_path = root / f"{prefix}.summary.json"
    atomic_write_bytes(matrix_path, result.matrix_bytes)
    atomic_write_json(summary_path, result.summary)
    return {
        "matrix_path": str(matrix_path),
        "summary_path": str(summary_path),
        "matrix_sha256": result.matrix_sha256,
        "summary_sha256": result.summary_sha256,
    }


__all__ = [
    "AssembledRow",
    "AssemblyResult",
    "assemble_features",
    "assembly_feature_names",
    "encode_matrix",
    "decode_matrix",
    "write_assembly_artifacts",
]

