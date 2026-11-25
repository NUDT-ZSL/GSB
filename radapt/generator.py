from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np

from . import GENERATOR_VERSION
from .errors import ErrorCode, RadaptError, ValidationError
from .model import BatchSpec, ColumnSpec, TableSpec


class HashRandom:
    """Small deterministic rejection sampler backed by SHA-256."""

    def __init__(self, *parts: Any) -> None:
        material = "|".join(str(part) for part in parts).encode("utf-8")
        self._key = hashlib.sha256(material).digest()
        self._counter = 0

    def randbelow(self, upper: int) -> int:
        if upper <= 0:
            raise ValueError("upper must be positive")
        if upper == 1:
            return 0
        bits = (upper.bit_length() + 7) // 8
        mask = (1 << (bits * 8)) - 1
        while True:
            digest = hashlib.sha256(self._key + self._counter.to_bytes(8, "big")).digest()
            self._counter += 1
            value = int.from_bytes(digest[:bits], "big") & mask
            if value < upper:
                return value


@dataclass(frozen=True)
class ColumnDigest:
    name: str
    type: str
    ndv: int
    dtype: str
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class BatchSummary:
    generator_version: str
    global_seed: int
    seq: int
    table: str
    window_id: str
    row_count: int
    digest: str
    columns: tuple[ColumnDigest, ...]
    skew_column: str | None = None
    skew_ratio: str | None = None
    hot_key_count: int | None = None
    actual_hot_ratio: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["columns"] = [item if isinstance(item, dict) else item.to_dict() for item in self.columns]
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BatchSummary":
        columns = tuple(ColumnDigest(**item) for item in raw.get("columns", []))
        data = dict(raw)
        data["columns"] = columns
        return cls(**data)


@dataclass
class BatchEnvelope:
    spec: BatchSpec
    columns: dict[str, np.ndarray]
    summary: BatchSummary


def _column_values(col: ColumnSpec) -> list[Any]:
    values: list[Any] = []
    for index in range(col.ndv):
        if col.type in {"int", "bigint", "smallint"}:
            values.append(10_000 + index)
        elif col.type == "timestamp":
            values.append(1_700_000_000_000 + index * 60_000)
        elif col.type == "decimal":
            values.append(100 + index * 137)
        elif col.type in {"double", "float"}:
            values.append(float(100 + index) + (index % 10) / 10.0)
        elif col.type == "boolean":
            values.append(bool(index % 2))
        else:
            values.append(f"{col.name}_{index:06d}")
    return values


def _array_for(col: ColumnSpec, values: list[Any]) -> np.ndarray:
    if col.type in {"int", "bigint", "smallint", "timestamp", "decimal"}:
        return np.asarray(values, dtype=np.int64)
    if col.type in {"double", "float"}:
        return np.asarray(values, dtype=np.float64)
    if col.type == "boolean":
        return np.asarray(values, dtype=np.bool_)
    max_length = max((len(str(value)) for value in values), default=1)
    return np.asarray(values, dtype=f"U{max(1, max_length)}")


def digest_array(col: ColumnSpec, array: np.ndarray) -> str:
    hasher = hashlib.sha256()
    hasher.update(col.name.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(col.type.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(str(array.dtype).encode("ascii"))
    hasher.update(b"\0")
    if col.type == "string" or array.dtype.kind in {"U", "O", "S"}:
        for value in array.tolist():
            encoded = str(value).encode("utf-8")
            hasher.update(len(encoded).to_bytes(4, "big"))
            hasher.update(encoded)
    else:
        contiguous = np.ascontiguousarray(array)
        hasher.update(contiguous.tobytes())
    return hasher.hexdigest()


def generate_batch(table: TableSpec, global_seed: int, spec: BatchSpec) -> BatchEnvelope:
    if spec.table != table.name:
        raise ValidationError(
            ErrorCode.BATCH_WINDOW_MISMATCH,
            f"batch {spec.seq} table {spec.table!r} does not match {table.name!r}",
        )
    columns: dict[str, np.ndarray] = {}
    column_digests: list[ColumnDigest] = []
    hot_count: int | None = None
    actual_ratio: Fraction | None = None

    for col in table.columns:
        rng = HashRandom(GENERATOR_VERSION, global_seed, table.name, spec.seq, col.name, col.type, col.ndv)
        pool = _column_values(col)
        values: list[Any] = []
        is_skew = table.skew_column == col.name and table.skew_ratio is not None
        if is_skew:
            ratio = table.skew_ratio or Fraction(0)
            hot_count = min(spec.row_count - 1, max(1, -(-ratio.numerator * spec.row_count // ratio.denominator)))
            actual_ratio = Fraction(hot_count, spec.row_count)
            for row_index in range(spec.row_count):
                if row_index < hot_count:
                    values.append(pool[0])
                else:
                    values.append(pool[rng.randbelow(col.ndv - 1) + 1])
        else:
            for _ in range(spec.row_count):
                values.append(pool[rng.randbelow(col.ndv)])
        array = _array_for(col, values)
        columns[col.name] = array
        digest = digest_array(col, array)
        column_digests.append(ColumnDigest(col.name, col.type, col.ndv, str(array.dtype), digest))

    canonical = {
        "generator_version": GENERATOR_VERSION,
        "global_seed": global_seed,
        "seq": spec.seq,
        "table": table.name,
        "window_id": spec.window_id,
        "row_count": spec.row_count,
        "columns": [item.to_dict() for item in column_digests],
        "skew_column": table.skew_column,
        "skew_ratio": None if table.skew_ratio is None else f"{table.skew_ratio.numerator}/{table.skew_ratio.denominator}",
        "hot_key_count": hot_count,
        "actual_hot_ratio": None if actual_ratio is None else f"{actual_ratio.numerator}/{actual_ratio.denominator}",
    }
    batch_digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    summary = BatchSummary(
        generator_version=GENERATOR_VERSION,
        global_seed=global_seed,
        seq=spec.seq,
        table=table.name,
        window_id=spec.window_id,
        row_count=spec.row_count,
        digest=batch_digest,
        columns=tuple(column_digests),
        skew_column=table.skew_column,
        skew_ratio=canonical["skew_ratio"],
        hot_key_count=hot_count,
        actual_hot_ratio=canonical["actual_hot_ratio"],
    )
    return BatchEnvelope(spec=spec, columns=columns, summary=summary)


def verify_envelope(envelope: BatchEnvelope, table: TableSpec) -> None:
    if envelope.summary.row_count != envelope.spec.row_count:
        raise RadaptError(
            ErrorCode.BATCH_DIGEST_MISMATCH,
            f"batch {envelope.spec.seq} row count does not match summary",
        )
    for col in table.columns:
        array = envelope.columns.get(col.name)
        if array is None:
            raise RadaptError(ErrorCode.BATCH_DIGEST_MISMATCH, f"batch {envelope.spec.seq} missing column {col.name!r}")
        expected = next(item.digest for item in envelope.summary.columns if item.name == col.name)
        actual = digest_array(col, array)
        if actual != expected:
            raise RadaptError(
                ErrorCode.BATCH_DIGEST_MISMATCH,
                f"batch {envelope.spec.seq} column {col.name!r} digest mismatch",
                {"expected": expected, "actual": actual},
            )


def envelope_to_json(envelope: BatchEnvelope) -> dict[str, Any]:
    return {
        "summary": envelope.summary.to_dict(),
        "columns": {name: array.tolist() for name, array in envelope.columns.items()},
    }


def envelope_from_json(raw: dict[str, Any], table: TableSpec) -> BatchEnvelope:
    summary = BatchSummary.from_dict(raw["summary"])
    columns: dict[str, np.ndarray] = {}
    for col in table.columns:
        values = raw["columns"][col.name]
        columns[col.name] = _array_for(col, values)
    spec = BatchSpec(
        seq=summary.seq,
        table=summary.table,
        window_id=summary.window_id,
        arrival_ms=0,
        row_count=summary.row_count,
    )
    envelope = BatchEnvelope(spec=spec, columns=columns, summary=summary)
    verify_envelope(envelope, table)
    return envelope


def write_batch_file(path: str | Path, envelope: BatchEnvelope) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(envelope_to_json(envelope), handle, ensure_ascii=False, indent=2, sort_keys=True)


def read_batch_file(path: str | Path, table: TableSpec, spec: BatchSpec) -> BatchEnvelope:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    envelope = envelope_from_json(raw, table)
    if envelope.summary.seq != spec.seq or envelope.summary.window_id != spec.window_id:
        raise RadaptError(ErrorCode.BATCH_DIGEST_MISMATCH, f"batch file {path} identity does not match arrival script")
    envelope.spec = spec  # dataclass is not frozen? BatchEnvelope not frozen, okay.
    return envelope
