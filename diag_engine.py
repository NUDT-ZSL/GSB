#!/usr/bin/env python3
"""Local, deterministic operator diagnostics engine.

The implementation intentionally uses only the Python standard library and
NumPy.  All persistent state is local JSON, NumPy arrays, and SQLite.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import json
import math
import os
import signal
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple

import numpy as np


SEED = 20250501
SCHEMA_VERSION = "lin-diag-v1"

NODE_INPUT = "input_validation_partition"
NODE_BLOCK = "block_op_execution"
NODE_REDUCE = "cross_block_reduction"
NODE_DEQUANT = "dequant_output"
NODES: Tuple[str, str, str, str] = (
    NODE_INPUT,
    NODE_BLOCK,
    NODE_REDUCE,
    NODE_DEQUANT,
)
STAGE_ORDER: Tuple[str, str, str, str] = (
    "input_quantization",
    "block_op_execution",
    "cross_block_reduction",
    "dequant_output",
)


class DiagError(RuntimeError):
    """Structured, stable error exposed by the diagnostic engine."""

    def __init__(self, code: str, message: str, details: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}

    def __str__(self) -> str:
        if not self.details:
            return f"{self.code}: {self.message}"
        return f"{self.code}: {self.message} ({canonical_json(self.details)})"


class ForcedTermination(BaseException):
    """Test/CLI hook used to simulate an uncatchable abrupt process death."""


def _normalize_json(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, np.ndarray):
        return _normalize_json(value.tolist())
    if isinstance(value, np.generic):
        return _normalize_json(value.item())
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): _normalize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_json(v) for v in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite float is not JSON-safe: {value!r}")
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"cannot canonicalize {type(value)!r}")


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize_json(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_json(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write a complete file via fsync plus an atomic rename."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_bytes(value) + b"\n")


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def array_content_hash(array: np.ndarray) -> str:
    """Hash float64 values, shape, and logical dtype rather than file bytes."""

    arr = np.ascontiguousarray(np.asarray(array, dtype=np.float64))
    h = hashlib.sha256()
    h.update(b"float64|")
    h.update(canonical_bytes(list(arr.shape)))
    h.update(b"|")
    h.update(arr.tobytes(order="C"))
    return h.hexdigest()


def normalize_pair(value: Any, field_name: str, default: Tuple[int, int]) -> Tuple[int, int]:
    if value is None:
        return default
    if isinstance(value, bool):
        raise DiagError("E_PARAM_INVALID", f"{field_name} must be an integer or pair", {"field": field_name})
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (not math.isfinite(value) or value != int(value)):
            raise DiagError("E_PARAM_INVALID", f"{field_name} must be integral", {"field": field_name, "value": value})
        return int(value), int(value)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return int(value[0]), int(value[1])
        except (TypeError, ValueError):
            pass
    raise DiagError("E_PARAM_INVALID", f"{field_name} must be a scalar or two-element pair", {"field": field_name, "value": value})


def normalize_float_pair(value: Any, field_name: str) -> Tuple[float, float]:
    if isinstance(value, (int, float)):
        pair = (float(value), float(value))
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        pair = (float(value[0]), float(value[1]))
    else:
        raise DiagError("E_PARAM_INVALID", f"{field_name} must be a scalar or two-element pair", {"field": field_name, "value": value})
    if not all(math.isfinite(v) and v > 0 for v in pair):
        raise DiagError("E_PARAM_INVALID", f"{field_name} must contain finite positive values", {"field": field_name, "value": pair})
    return pair


def normalize_shape(value: Any, field_name: str) -> Tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise DiagError("E_SHAPE_MISMATCH", f"{field_name} must be a non-empty shape", {"field": field_name, "value": value})
    shape: List[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise DiagError("E_SHAPE_MISMATCH", f"{field_name} has an invalid dimension", {"field": field_name, "value": value})
        shape.append(int(item))
    return tuple(shape)


def normalize_tolerance(value: Any, default: float = 0.5) -> Dict[str, float]:
    if value is None:
        return {stage: float(default) for stage in STAGE_ORDER}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        tol = float(value)
        if not math.isfinite(tol) or tol < 0:
            raise DiagError("E_PARAM_INVALID", "tolerance must be finite and non-negative", {"tolerance": value})
        return {stage: tol for stage in STAGE_ORDER}
    if isinstance(value, Mapping):
        result = {stage: float(default) for stage in STAGE_ORDER}
        for key, item in value.items():
            if key not in STAGE_ORDER:
                raise DiagError("E_PARAM_INVALID", "unknown tolerance stage", {"stage": key})
            tol = float(item)
            if not math.isfinite(tol) or tol < 0:
                raise DiagError("E_PARAM_INVALID", "tolerance must be finite and non-negative", {"stage": key, "tolerance": item})
            result[str(key)] = tol
        return result
    raise DiagError("E_PARAM_INVALID", "tolerance must be a number or stage map", {"tolerance": value})


def index_position(flat_index: int, shape: Sequence[int]) -> List[int]:
    shape_list = [int(x) for x in shape]
    if not shape_list:
        return [int(flat_index)]
    coords: List[int] = []
    remainder = int(flat_index)
    for dim in reversed(shape_list):
        coords.append(remainder % dim)
        remainder //= dim
    return list(reversed(coords))


@dataclass(frozen=True)
class Job:
    job_id: str
    op_kind: str
    input_shape: Tuple[int, ...]
    kernel_shape: Optional[Tuple[int, ...]]
    scale: Optional[Tuple[float, float]]
    stride: Tuple[int, int]
    padding: Tuple[int, int]
    interp_mode: str
    quant_scale: float
    quant_zero_point: int
    block_size: int
    logical_clock_ms: int
    revision: int
    config_key: str
    input_path: str
    kernel_path: Optional[str]
    tolerance: Any
    tolerance_map: Dict[str, float] = field(compare=False, hash=False)

    def semantic_dict(self) -> Dict[str, Any]:
        return {
            "job_id": self.job_id,
            "op_kind": self.op_kind,
            "input_shape": list(self.input_shape),
            "kernel_shape": list(self.kernel_shape) if self.kernel_shape is not None else None,
            "scale": list(self.scale) if self.scale is not None else None,
            "stride": list(self.stride),
            "padding": list(self.padding),
            "interp_mode": self.interp_mode,
            "quant_scale": self.quant_scale,
            "quant_zero_point": self.quant_zero_point,
            "block_size": self.block_size,
            "logical_clock_ms": self.logical_clock_ms,
            "revision": self.revision,
            "config_key": self.config_key,
            "tolerance": _normalize_json(self.tolerance),
        }

    def semantic_without_revision(self) -> Dict[str, Any]:
        value = self.semantic_dict()
        value.pop("revision", None)
        return value


def parse_job(raw: Mapping[str, Any], line_no: int = 0) -> Job:
    job_id = str(raw.get("job_id", "")).strip()
    if not job_id:
        raise DiagError("E_PARAM_INVALID", "job_id is required", {"line": line_no})
    op_kind = str(raw.get("op_kind", "")).strip().lower()
    if op_kind not in ("conv2d", "interpolate"):
        raise DiagError("E_PARAM_INVALID", "op_kind must be conv2d or interpolate", {"job_id": job_id, "op_kind": op_kind})

    input_shape = normalize_shape(raw.get("input_shape"), "input_shape")
    if len(input_shape) != 4:
        raise DiagError("E_SHAPE_MISMATCH", "input_shape must be NHWC (4 dimensions)", {"job_id": job_id, "input_shape": list(input_shape)})

    if op_kind == "conv2d":
        kernel_shape = normalize_shape(raw.get("kernel_shape"), "kernel_shape")
        if len(kernel_shape) != 4:
            raise DiagError("E_SHAPE_MISMATCH", "kernel_shape must be [out_channels, kh, kw, in_channels]", {"job_id": job_id})
        scale = None
        interp_mode = None
        kernel_path = str(raw.get("kernel_path", f"arrays/{job_id}_kernel.npy"))
    else:
        kernel_shape = None
        scale = normalize_float_pair(raw.get("scale", raw.get("interp_scale")), "scale")
        interp_mode = str(raw.get("interp_mode", "bilinear")).strip().lower()
        if interp_mode not in ("nearest", "bilinear"):
            raise DiagError("E_PARAM_INVALID", "interp_mode must be nearest or bilinear", {"job_id": job_id, "interp_mode": interp_mode})
        kernel_path = None

    stride = normalize_pair(raw.get("stride"), "stride", (1, 1))
    padding = normalize_pair(raw.get("padding"), "padding", (0, 0))
    if stride[0] <= 0 or stride[1] <= 0:
        raise DiagError("E_PARAM_INVALID", "stride must be positive", {"job_id": job_id, "stride": stride})
    if padding[0] < 0 or padding[1] < 0:
        raise DiagError("E_PARAM_INVALID", "padding must be non-negative", {"job_id": job_id, "padding": padding})

    try:
        quant_scale = float(raw.get("quant_scale"))
    except (TypeError, ValueError):
        raise DiagError("E_PARAM_INVALID", "quant_scale must be numeric", {"job_id": job_id})
    if not math.isfinite(quant_scale) or quant_scale <= 0:
        raise DiagError("E_PARAM_INVALID", "quant_scale must be finite and positive", {"job_id": job_id, "quant_scale": raw.get("quant_scale")})
    try:
        quant_zero_point = int(raw.get("quant_zero_point", 0))
        if isinstance(raw.get("quant_zero_point"), bool):
            raise ValueError
    except (TypeError, ValueError):
        raise DiagError("E_PARAM_INVALID", "quant_zero_point must be an integer", {"job_id": job_id, "quant_zero_point": raw.get("quant_zero_point")})
    try:
        block_size = int(raw.get("block_size"))
        if isinstance(raw.get("block_size"), bool):
            raise ValueError
    except (TypeError, ValueError):
        raise DiagError("E_PARAM_INVALID", "block_size must be an integer", {"job_id": job_id})
    if block_size <= 0:
        raise DiagError("E_PARAM_INVALID", "block_size must be positive", {"job_id": job_id, "block_size": block_size})
    try:
        logical_clock_ms = int(raw.get("logical_clock_ms", 0))
        revision = int(raw.get("revision", 0))
        if isinstance(raw.get("logical_clock_ms"), bool) or isinstance(raw.get("revision"), bool):
            raise ValueError
    except (TypeError, ValueError):
        raise DiagError("E_PARAM_INVALID", "logical_clock_ms and revision must be integers", {"job_id": job_id})
    if logical_clock_ms < 0 or revision < 0:
        raise DiagError("E_PARAM_INVALID", "logical_clock_ms and revision must be non-negative", {"job_id": job_id})

    tolerance = raw.get("tolerance", raw.get("tolerances"))
    tolerance_map = normalize_tolerance(tolerance)
    config_key = str(raw.get("config_key", job_id))
    input_path = str(raw.get("input_path", f"arrays/{job_id}_input.npy"))

    return Job(
        job_id=job_id,
        op_kind=op_kind,
        input_shape=input_shape,
        kernel_shape=kernel_shape,
        scale=scale,
        stride=stride,
        padding=padding,
        interp_mode=interp_mode or "",
        quant_scale=quant_scale,
        quant_zero_point=quant_zero_point,
        block_size=block_size,
        logical_clock_ms=logical_clock_ms,
        revision=revision,
        config_key=config_key,
        input_path=input_path,
        kernel_path=kernel_path,
        tolerance=tolerance,
        tolerance_map=tolerance_map,
    )


def load_manifest(path: Path) -> Tuple[List[Job], str]:
    jobs: List[Job] = []
    seen: set[str] = set()
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DiagError("E_PARAM_INVALID", "invalid JSONL", {"line": line_no, "error": str(exc)})
            if not isinstance(raw, Mapping):
                raise DiagError("E_PARAM_INVALID", "manifest line must be an object", {"line": line_no})
            job = parse_job(raw, line_no)
            if job.job_id in seen:
                raise DiagError("E_DUP_JOB", "duplicate job_id", {"job_id": job.job_id, "line": line_no})
            seen.add(job.job_id)
            jobs.append(job)
    if not jobs:
        raise DiagError("E_PARAM_INVALID", "manifest contains no jobs", {"path": str(path)})
    semantic_hash = sha256_json([job.semantic_dict() for job in jobs])
    return jobs, semantic_hash


def validate_revisions(jobs: Sequence[Job], previous: Mapping[str, int]) -> None:
    for job in jobs:
        old = previous.get(job.job_id)
        if old is not None and job.revision < int(old):
            raise DiagError(
                "E_REVISION_ROLLBACK",
                "job revision moved backwards",
                {"job_id": job.job_id, "previous_revision": int(old), "new_revision": job.revision},
            )


class SampleStore:
    def __init__(self, root: Path):
        self.root = root

    def resolve(self, relative_or_absolute: str) -> Path:
        path = Path(relative_or_absolute)
        if path.is_absolute():
            return path
        return (self.root / path).resolve()

    def load_input(self, job: Job) -> np.ndarray:
        path = self.resolve(job.input_path)
        if not path.is_file():
            raise DiagError("E_SHAPE_MISMATCH", "input array is missing", {"job_id": job.job_id, "path": str(path)})
        array = np.load(path, allow_pickle=False)
        array = np.asarray(array, dtype=np.float64)
        if tuple(array.shape) != tuple(job.input_shape):
            raise DiagError(
                "E_SHAPE_MISMATCH",
                "input array dimensions do not match input_shape",
                {"job_id": job.job_id, "declared": list(job.input_shape), "actual": list(array.shape), "path": str(path)},
            )
        if not np.all(np.isfinite(array)):
            raise DiagError("E_PARAM_INVALID", "input array contains non-finite values", {"job_id": job.job_id})
        return array

    def load_kernel(self, job: Job) -> Optional[np.ndarray]:
        if job.op_kind != "conv2d":
            return None
        assert job.kernel_path is not None
        path = self.resolve(job.kernel_path)
        if not path.is_file():
            raise DiagError("E_SHAPE_MISMATCH", "kernel array is missing", {"job_id": job.job_id, "path": str(path)})
        array = np.asarray(np.load(path, allow_pickle=False), dtype=np.float64)
        if tuple(array.shape) != tuple(job.kernel_shape):
            raise DiagError(
                "E_SHAPE_MISMATCH",
                "kernel array dimensions do not match kernel_shape",
                {"job_id": job.job_id, "declared": list(job.kernel_shape or ()), "actual": list(array.shape), "path": str(path)},
            )
        if not np.all(np.isfinite(array)):
            raise DiagError("E_PARAM_INVALID", "kernel array contains non-finite values", {"job_id": job.job_id})
        return array

    def validate(self, jobs: Sequence[Job]) -> None:
        for job in jobs:
            input_arr = self.load_input(job)
            kernel = self.load_kernel(job)
            if job.op_kind == "conv2d" and kernel is not None:
                if input_arr.shape[-1] != kernel.shape[-1]:
                    raise DiagError(
                        "E_SHAPE_MISMATCH",
                        "conv2d input channels must equal kernel in_channels",
                        {
                            "job_id": job.job_id,
                            "input_shape": list(input_arr.shape),
                            "kernel_shape": list(kernel.shape),
                        },
                    )


@dataclass(frozen=True)
class QuantConfigRecord:
    config_key: str
    effective_abs_ms: int
    generation: int
    quant_scale: float
    quant_zero_point: int
    block_size: int
    revision: int
    tombstone: bool = False
    provenance: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    def semantic_dict(self) -> Dict[str, Any]:
        return {
            "config_key": self.config_key,
            "effective_abs_ms": self.effective_abs_ms,
            "generation": self.generation,
            "quant_scale": self.quant_scale,
            "quant_zero_point": self.quant_zero_point,
            "block_size": self.block_size,
            "revision": self.revision,
            "tombstone": self.tombstone,
        }

    def active_dict(self) -> Dict[str, Any]:
        value = self.semantic_dict()
        value.pop("tombstone", None)
        return value


def _record_from_fields(
    config_key: str,
    effective_abs_ms: int,
    generation: int,
    quant_scale: Any,
    quant_zero_point: Any,
    block_size: Any,
    revision: Any,
    tombstone: Any = False,
    provenance: Optional[Mapping[str, Any]] = None,
) -> QuantConfigRecord:
    try:
        scale = float(quant_scale)
        zero_point = int(quant_zero_point)
        config_block_size = int(block_size)
        config_revision = int(revision)
        config_generation = int(generation)
        absolute_ms = int(effective_abs_ms)
    except (TypeError, ValueError, OverflowError):
        raise DiagError("E_PARAM_INVALID", "config record contains invalid scalar fields", {"config_key": config_key})
    if not math.isfinite(scale) or scale <= 0:
        raise DiagError("E_PARAM_INVALID", "config quant_scale must be finite and positive", {"config_key": config_key})
    if config_block_size <= 0 or config_revision < 0 or config_generation < 0:
        raise DiagError("E_PARAM_INVALID", "config values must be non-negative and block_size positive", {"config_key": config_key})
    return QuantConfigRecord(
        config_key=config_key,
        effective_abs_ms=absolute_ms,
        generation=config_generation,
        quant_scale=scale,
        quant_zero_point=zero_point,
        block_size=config_block_size,
        revision=config_revision,
        tombstone=bool(tombstone),
        provenance=dict(provenance or {}),
    )


def _validate_v1_document(document: Mapping[str, Any]) -> None:
    if document.get("schema") not in ("quant-config-v1", "quant-config-v1.0"):
        raise DiagError("E_SEMANTIC_DRIFT", "source is not a v1 quant config snapshot", {"schema": document.get("schema")})
    if not isinstance(document.get("records"), list):
        raise DiagError("E_SEMANTIC_DRIFT", "v1 snapshot must contain a flat records list", {})


def read_v1_records(path: Path, base_epoch_ms: int = 0) -> List[QuantConfigRecord]:
    document = read_json(path)
    if not isinstance(document, Mapping):
        raise DiagError("E_PARAM_INVALID", "v1 snapshot must be a JSON object", {"path": str(path)})
    _validate_v1_document(document)
    records: List[QuantConfigRecord] = []
    keys: set[str] = set()
    for item in document["records"]:
        if not isinstance(item, Mapping):
            raise DiagError("E_SEMANTIC_DRIFT", "v1 record must be an object", {})
        key = str(item.get("config_key", "")).strip()
        if not key:
            raise DiagError("E_SEMANTIC_DRIFT", "v1 record has no config_key", {})
        if key in keys:
            raise DiagError("E_PARAM_INVALID", "duplicate config_key in v1 snapshot", {"config_key": key})
        keys.add(key)
        try:
            relative_seconds = float(item["relative_effective_seconds"])
        except (KeyError, TypeError, ValueError):
            raise DiagError("E_SEMANTIC_DRIFT", "v1 record has no usable relative_effective_seconds", {"config_key": key})
        if not math.isfinite(relative_seconds):
            raise DiagError("E_SEMANTIC_DRIFT", "v1 relative_effective_seconds must be finite", {"config_key": key})
        effective_abs_ms = int(round(int(base_epoch_ms) + relative_seconds * 1000.0))
        generation = item.get("config_generation", item.get("generation", 1))
        provenance = {
            "effective_abs_ms": {
                "source": "v1.relative_effective_seconds + caller.base_epoch_ms",
                "relative_effective_seconds": relative_seconds,
                "base_epoch_ms": int(base_epoch_ms),
            },
            "generation": {
                "source": "v1.config_generation" if "config_generation" in item or "generation" in item else "schema_default",
                "value": generation,
            },
            "tombstone": {"source": "v1 schema defaults to false", "value": False},
        }
        records.append(
            _record_from_fields(
                key,
                effective_abs_ms,
                generation,
                item.get("quant_scale"),
                item.get("quant_zero_point"),
                item.get("block_size"),
                item.get("revision", 0),
                False,
                provenance,
            )
        )
    return records


def write_v1_records(
    path: Path,
    records: Sequence[QuantConfigRecord],
    base_epoch_ms: int = 0,
    relative_epoch_seconds: float = 0.0,
) -> None:
    rows: List[Dict[str, Any]] = []
    for record in records:
        if record.tombstone:
            raise DiagError(
                "E_SEMANTIC_DRIFT",
                "v1 cannot represent a tombstone and the explicit downgrade rule did not drop it",
                {"config_key": record.config_key},
            )
        rows.append(
            {
                "config_key": record.config_key,
                "relative_effective_seconds": (record.effective_abs_ms - int(base_epoch_ms)) / 1000.0,
                "config_generation": record.generation,
                "quant_scale": record.quant_scale,
                "quant_zero_point": record.quant_zero_point,
                "block_size": record.block_size,
                "revision": record.revision,
            }
        )
    document = {
        "schema": "quant-config-v1",
        "relative_epoch_seconds": float(relative_epoch_seconds),
        "records": rows,
    }
    atomic_write_json(path, document)


def _fragment_filename(config_key: str) -> str:
    return f"{sha256_bytes(config_key.encode('utf-8'))[:32]}.json"


def write_v2_records(v2_dir: Path, records: Sequence[QuantConfigRecord]) -> None:
    """Write fragment JSON files and atomically replace the SQLite metadata DB."""

    v2_dir = Path(v2_dir)
    fragments_dir = v2_dir / "fragments"
    fragments_dir.mkdir(parents=True, exist_ok=True)
    db_path = v2_dir / "quant_config_v2.sqlite3"
    keys: set[str] = set()
    fragment_rows: List[Tuple[str, int, int, int, int, str, str]] = []

    for record in records:
        if record.config_key in keys:
            raise DiagError("E_PARAM_INVALID", "duplicate config_key in v2 snapshot", {"config_key": record.config_key})
        keys.add(record.config_key)
        fragment_path = fragments_dir / _fragment_filename(record.config_key)
        fragment = {
            "schema": "quant-config-v2-fragment",
            "config_key": record.config_key,
            "quant_scale": record.quant_scale,
            "quant_zero_point": record.quant_zero_point,
            "block_size": record.block_size,
            "revision": record.revision,
            "provenance": record.provenance,
        }
        atomic_write_json(fragment_path, fragment)
        fragment_rows.append(
            (
                record.config_key,
                int(record.effective_abs_ms),
                int(record.generation),
                1 if record.tombstone else 0,
                int(record.revision),
                fragment_path.relative_to(v2_dir).as_posix(),
                sha256_file(fragment_path),
            )
        )

    old_hashes = {path.name for path in fragments_dir.glob("*.json")}
    expected_hashes = {Path(row[5]).name for row in fragment_rows}
    db_tmp = v2_dir / f".quant_config_v2.sqlite3.{os.getpid()}.tmp"
    with contextlib.suppress(FileNotFoundError):
        db_tmp.unlink()
    conn = sqlite3.connect(str(db_tmp))
    try:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            """
            CREATE TABLE config_meta (
                config_key TEXT PRIMARY KEY,
                effective_abs_ms INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                tombstone INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                fragment_path TEXT NOT NULL,
                fragment_sha256 TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE TABLE snapshot_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany(
            "INSERT INTO config_meta(config_key, effective_abs_ms, generation, tombstone, revision, fragment_path, fragment_sha256) VALUES (?,?,?,?,?,?,?)",
            fragment_rows,
        )
        conn.execute("INSERT INTO snapshot_meta(key, value) VALUES (?, ?)", ("schema", "quant-config-v2"))
        conn.commit()
        conn.close()
        os.replace(db_tmp, db_path)
    finally:
        with contextlib.suppress(Exception):
            conn.close()
        with contextlib.suppress(FileNotFoundError):
            db_tmp.unlink()

    for old_name in old_hashes - expected_hashes:
        with contextlib.suppress(FileNotFoundError):
            (fragments_dir / old_name).unlink()


def read_v2_records(v2_dir: Path) -> List[QuantConfigRecord]:
    v2_dir = Path(v2_dir)
    db_path = v2_dir / "quant_config_v2.sqlite3"
    if not db_path.is_file():
        raise DiagError("E_PARAM_INVALID", "v2 metadata SQLite database is missing", {"path": str(db_path)})
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT config_key, effective_abs_ms, generation, tombstone, revision, fragment_path, fragment_sha256 FROM config_meta ORDER BY config_key"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise DiagError("E_PARAM_INVALID", "v2 metadata database is invalid", {"error": str(exc)})
    finally:
        conn.close()

    records: List[QuantConfigRecord] = []
    for row in rows:
        fragment_path = v2_dir / row["fragment_path"]
        if not fragment_path.is_file():
            raise DiagError("E_SEMANTIC_DRIFT", "v2 fragment is missing", {"config_key": row["config_key"], "path": str(fragment_path)})
        if sha256_file(fragment_path) != row["fragment_sha256"]:
            raise DiagError("E_SEMANTIC_DRIFT", "v2 fragment checksum mismatch", {"config_key": row["config_key"]})
        fragment = read_json(fragment_path)
        if not isinstance(fragment, Mapping) or fragment.get("config_key") != row["config_key"]:
            raise DiagError("E_SEMANTIC_DRIFT", "v2 fragment identity does not match metadata", {"config_key": row["config_key"]})
        records.append(
            _record_from_fields(
                row["config_key"],
                row["effective_abs_ms"],
                row["generation"],
                fragment.get("quant_scale"),
                fragment.get("quant_zero_point"),
                fragment.get("block_size"),
                fragment.get("revision", row["revision"]),
                bool(row["tombstone"]),
                fragment.get("provenance", {}),
            )
        )
    return records


def _active_semantic(records: Iterable[QuantConfigRecord]) -> List[Dict[str, Any]]:
    return sorted(
        [record.active_dict() for record in records if not record.tombstone],
        key=lambda item: item["config_key"],
    )


def assert_semantic_equivalence(
    source_records: Sequence[QuantConfigRecord],
    target_records: Sequence[QuantConfigRecord],
    context: Mapping[str, Any],
) -> None:
    source = _active_semantic(source_records)
    target = _active_semantic(target_records)
    if source != target:
        raise DiagError(
            "E_SEMANTIC_DRIFT",
            "quant config migration changed active semantic content",
            {"context": dict(context), "source_active": source, "target_active": target},
        )


def migrate_v1_to_v2(
    v1_path: Path,
    v2_dir: Path,
    base_epoch_ms: int = 0,
    report_path: Optional[Path] = None,
) -> Dict[str, Any]:
    source_records = read_v1_records(v1_path, base_epoch_ms=base_epoch_ms)
    write_v2_records(v2_dir, source_records)
    target_records = read_v2_records(v2_dir)
    assert_semantic_equivalence(
        source_records,
        target_records,
        {"migration": "v1->v2", "v1_path": str(v1_path), "v2_dir": str(v2_dir), "base_epoch_ms": int(base_epoch_ms)},
    )
    report = {
        "schema": "quant-config-migration-report-v1",
        "migration": "v1->v2",
        "source": str(v1_path),
        "target": str(v2_dir),
        "base_epoch_ms": int(base_epoch_ms),
        "record_count": len(source_records),
        "records": [
            {
                "config_key": record.config_key,
                "filled_fields": record.provenance,
            }
            for record in source_records
        ],
    }
    if report_path is not None:
        atomic_write_json(report_path, report)
    return report


def migrate_v2_to_v1(
    v2_dir: Path,
    v1_path: Path,
    base_epoch_ms: int = 0,
    report_path: Optional[Path] = None,
) -> Dict[str, Any]:
    source_records = read_v2_records(v2_dir)
    active_records = [record for record in source_records if not record.tombstone]
    discarded: List[Dict[str, Any]] = []
    for record in source_records:
        if record.tombstone:
            discarded.append(
                {
                    "config_key": record.config_key,
                    "effective_abs_ms": record.effective_abs_ms,
                    "generation": record.generation,
                    "reason": "v1 schema cannot express tombstone; explicit downgrade rule drops inactive tombstone",
                }
            )
    write_v1_records(v1_path, active_records, base_epoch_ms=base_epoch_ms, relative_epoch_seconds=0.0)
    parsed_v1 = read_v1_records(v1_path, base_epoch_ms=base_epoch_ms)
    assert_semantic_equivalence(
        active_records,
        parsed_v1,
        {"migration": "v2->v1", "v2_dir": str(v2_dir), "v1_path": str(v1_path), "base_epoch_ms": int(base_epoch_ms)},
    )
    report = {
        "schema": "quant-config-migration-report-v1",
        "migration": "v2->v1",
        "source": str(v2_dir),
        "target": str(v1_path),
        "base_epoch_ms": int(base_epoch_ms),
        "record_count": len(active_records),
        "discarded_tombstones": discarded,
        "discard_rule": "Tombstoned records are inactive and are omitted from v1; their discard is recorded explicitly.",
    }
    if report_path is not None:
        atomic_write_json(report_path, report)
    return report


class ConfigView:
    def __init__(self, records: Sequence[QuantConfigRecord], source: str):
        self.records: Dict[str, QuantConfigRecord] = {record.config_key: record for record in records}
        self.source = source
        self.semantic_hash = sha256_json(_active_semantic(records))

    @classmethod
    def from_v1(cls, path: Path, base_epoch_ms: int = 0) -> "ConfigView":
        return cls(read_v1_records(path, base_epoch_ms=base_epoch_ms), f"v1:{path}")

    @classmethod
    def from_v2(cls, v2_dir: Path) -> "ConfigView":
        return cls(read_v2_records(v2_dir), f"v2:{v2_dir}")

    def record_for_job(self, job: Job) -> QuantConfigRecord:
        record = self.records.get(job.config_key)
        if record is None:
            raise DiagError(
                "E_PARAM_INVALID",
                "job references a missing configuration",
                {"job_id": job.job_id, "config_key": job.config_key},
            )
        if record.tombstone:
            raise DiagError(
                "E_PARAM_INVALID",
                "job references a tombstoned configuration",
                {"job_id": job.job_id, "config_key": job.config_key},
            )
        if record.quant_scale != job.quant_scale or record.quant_zero_point != job.quant_zero_point:
            raise DiagError(
                "E_PARAM_INVALID",
                "job quantization parameters disagree with the active configuration",
                {
                    "job_id": job.job_id,
                    "config_key": job.config_key,
                    "job_quant_scale": job.quant_scale,
                    "config_quant_scale": record.quant_scale,
                    "job_quant_zero_point": job.quant_zero_point,
                    "config_quant_zero_point": record.quant_zero_point,
                },
            )
        if record.block_size != job.block_size:
            raise DiagError(
                "E_PARAM_INVALID",
                "job block_size disagrees with the active configuration",
                {"job_id": job.job_id, "config_key": job.config_key, "job_block_size": job.block_size, "config_block_size": record.block_size},
            )
        return record

    def validate_jobs(self, jobs: Sequence[Job]) -> None:
        for job in jobs:
            self.record_for_job(job)


def synthetic_config_view(jobs: Sequence[Job]) -> ConfigView:
    records: List[QuantConfigRecord] = []
    for job in jobs:
        if job.config_key in {r.config_key for r in records}:
            continue
        records.append(
            QuantConfigRecord(
                config_key=job.config_key,
                effective_abs_ms=job.logical_clock_ms,
                generation=1,
                quant_scale=job.quant_scale,
                quant_zero_point=job.quant_zero_point,
                block_size=job.block_size,
                revision=job.revision,
            )
        )
    return ConfigView(records, "synthetic")


class StageLock:
    """Non-blocking advisory lock for config snapshots and incremental commits."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: Optional[int] = None

    def __enter__(self) -> "StageLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            raise DiagError("E_STAGE_BUSY", "another stage/config commit is active", {"lock_path": str(self.path)})
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def cache_public_key(
    node: str,
    job: Job,
    config_generation: int,
    input_hash: str,
    kernel_hash: str,
    upstream_product_hash: str,
) -> str:
    material = {
        "schema": "content-cache-key-v1",
        "node": node,
        "input_content_hash": input_hash,
        "kernel_content_hash": kernel_hash,
        "op_kind": job.op_kind,
        "quant_scale": job.quant_scale,
        "quant_zero_point": job.quant_zero_point,
        "quant_config_generation": int(config_generation),
        "block_size": job.block_size,
        "stride": list(job.stride),
        "padding": list(job.padding),
        "interp_mode": job.interp_mode,
        "interp_scale": list(job.scale) if job.scale is not None else None,
        "logical_clock_ms": job.logical_clock_ms,
        "revision": job.revision,
        "config_key": job.config_key,
        "upstream_product_hash": upstream_product_hash,
    }
    return sha256_json(material)


class CacheStore:
    """Content-addressed four-node snapshot cache with an atomic index pointer."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.index: Dict[str, Dict[str, Any]] = {}
        self._load_index()

    def _load_index(self) -> None:
        if not self.index_path.is_file():
            self.index = {}
            return
        try:
            value = read_json(self.index_path)
        except (json.JSONDecodeError, OSError):
            self.index = {}
            return
        if not isinstance(value, Mapping) or value.get("schema") != "content-cache-index-v1":
            self.index = {}
            return
        entries = value.get("entries")
        self.index = dict(entries) if isinstance(entries, Mapping) else {}

    def restore_index(self, snapshot: Mapping[str, Any]) -> None:
        self.index = dict(snapshot)
        self.save_index()

    def save_index(self) -> None:
        atomic_write_json(
            self.index_path,
            {"schema": "content-cache-index-v1", "entries": self.index},
        )

    def entry_path(self, cache_key: str) -> Path:
        return self.entries_dir / f"{sha256_bytes(cache_key.encode('utf-8'))}.json"

    def peek(self, cache_key: str) -> Optional[Dict[str, Any]]:
        meta = self.index.get(cache_key)
        if not isinstance(meta, Mapping):
            return None
        path = self.entry_path(cache_key)
        if not path.is_file():
            return None
        try:
            entry = read_json(path)
        except (json.JSONDecodeError, OSError):
            return None
        if entry.get("cache_key") != cache_key:
            return None
        payload = entry.get("payload")
        product_hash = sha256_json(payload)
        if product_hash != entry.get("product_hash") or product_hash != meta.get("product_hash"):
            return None
        if entry.get("node") != meta.get("node"):
            return None
        return entry

    def count(self) -> int:
        return len(self.index)

    def stage_entry(
        self,
        node: str,
        cache_key: str,
        job_id: str,
        payload: Mapping[str, Any],
        run_id: str,
        logical_clock_ms: int,
        max_entries: int,
        reserved_entries: int = 0,
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        product_hash = sha256_json(payload)
        if cache_key not in self.index and self.count() + int(reserved_entries) >= max_entries:
            raise DiagError(
                "E_RESOURCE_LIMIT",
                "cache entry limit would be exceeded",
                {
                    "limit": max_entries,
                    "current": self.count(),
                    "reserved": int(reserved_entries),
                    "node": node,
                    "job_id": job_id,
                },
            )
        entry = {
            "schema": "content-cache-entry-v1",
            "node": node,
            "cache_key": cache_key,
            "product_hash": product_hash,
            "payload": _normalize_json(payload),
            "metadata": {
                "job_id": job_id,
                "created_run_id": run_id,
                "created_logical_clock_ms": int(logical_clock_ms),
            },
        }
        path = self.entry_path(cache_key)
        atomic_write_json(path, entry)
        index_meta = {
            "node": node,
            "job_id": job_id,
            "product_hash": product_hash,
            "path": path.relative_to(self.root).as_posix(),
            "created_run_id": run_id,
            "created_logical_clock_ms": int(logical_clock_ms),
        }
        return entry, index_meta

    def commit_staged(self, staged: Mapping[str, Dict[str, Any]]) -> None:
        changed = False
        for key, meta in staged.items():
            if key not in self.index:
                self.index[key] = dict(meta)
                changed = True
        if changed:
            self.save_index()

    def cleanup_orphans(self) -> List[str]:
        expected = {self.entry_path(key).name for key in self.index}
        removed: List[str] = []
        for path in self.entries_dir.glob("*.json"):
            if path.name not in expected:
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()
                    removed.append(path.name)
        return removed


def _make_checkpoint_checksum(payload: Mapping[str, Any]) -> str:
    value = dict(payload)
    value.pop("content_checksum", None)
    return sha256_json(value)


def verify_checkpoint(document: Mapping[str, Any]) -> bool:
    if document.get("schema") != "diagnostic-checkpoint-v1":
        return False
    expected = document.get("content_checksum")
    return isinstance(expected, str) and expected == _make_checkpoint_checksum(document)


class CheckpointStore:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_seen_version = self._max_version_on_disk()

    def _max_version_on_disk(self) -> int:
        highest = 0
        for path in self.directory.glob("checkpoint_*.json"):
            try:
                highest = max(highest, int(path.stem.split("_")[-1]))
            except ValueError:
                continue
        return highest

    def load_latest(self) -> Optional[Dict[str, Any]]:
        candidates = sorted(self.directory.glob("checkpoint_*.json"), reverse=True)
        for path in candidates:
            try:
                document = read_json(path)
                version = int(document.get("version", -1))
                if version > self.max_seen_version:
                    self.max_seen_version = version
            except (json.JSONDecodeError, OSError, TypeError, ValueError):
                continue
            if verify_checkpoint(document):
                return document
        return None

    def commit(
        self,
        run_id: str,
        status: str,
        completed_jobs: Sequence[str],
        job_revisions: Mapping[str, int],
        ledger_bytes: int,
        ledger_sha256: str,
        cache_index: Mapping[str, Any],
        manifest_hash: str,
        config_hash: str,
        logical_clock_ms: int,
        details: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        self.max_seen_version += 1
        payload = {
            "schema": "diagnostic-checkpoint-v1",
            "run_id": run_id,
            "version": self.max_seen_version,
            "status": status,
            "completed_jobs": sorted(str(job_id) for job_id in completed_jobs),
            "job_revisions": {str(k): int(v) for k, v in job_revisions.items()},
            "ledger_bytes": int(ledger_bytes),
            "ledger_sha256": str(ledger_sha256),
            "cache_index": _normalize_json(cache_index),
            "manifest_hash": manifest_hash,
            "config_hash": config_hash,
            "logical_clock_ms": int(logical_clock_ms),
            "details": _normalize_json(details or {}),
        }
        payload["content_checksum"] = _make_checkpoint_checksum(payload)
        path = self.directory / f"checkpoint_{self.max_seen_version:08d}.json"
        atomic_write_json(path, payload)
        return payload


class LedgerStore:
    """Append-only ledger with checkpoint-driven truncation on recovery."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def append(self, record: Mapping[str, Any]) -> Tuple[int, str]:
        with self.path.open("ab") as fh:
            fh.write(canonical_bytes(record) + b"\n")
            fh.flush()
            os.fsync(fh.fileno())
        size = self.path.stat().st_size
        return size, sha256_file(self.path)

    def reset(self) -> None:
        atomic_write_bytes(self.path, b"")

    def truncate_and_verify(self, expected_bytes: int, expected_sha256: str) -> None:
        actual_bytes = self.path.stat().st_size if self.path.is_file() else 0
        if actual_bytes < expected_bytes:
            raise DiagError(
                "E_RECOVERY_FAILED",
                "ledger is shorter than the checkpointed committed prefix",
                {"expected_bytes": expected_bytes, "actual_bytes": actual_bytes},
            )
        if actual_bytes > expected_bytes:
            with self.path.open("r+b") as fh:
                fh.truncate(expected_bytes)
                fh.flush()
                os.fsync(fh.fileno())
        if sha256_file(self.path) != expected_sha256:
            raise DiagError("E_RECOVERY_FAILED", "ledger checksum mismatch after recovery truncation", {})

    def load_records(self) -> List[Dict[str, Any]]:
        records: List[Dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise DiagError("E_RECOVERY_FAILED", "invalid ledger JSONL", {"line": line_no, "error": str(exc)})
                if not isinstance(record, Mapping):
                    raise DiagError("E_RECOVERY_FAILED", "ledger line must be an object", {"line": line_no})
                records.append(dict(record))
        return records


def load_state_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return read_json(path)
    except (json.JSONDecodeError, OSError):
        return default


def save_state_json(path: Path, value: Any) -> None:
    atomic_write_json(path, value)


def _quantize_array(array: np.ndarray, scale: float, zero_point: int) -> Tuple[np.ndarray, np.ndarray]:
    q = np.rint(np.asarray(array, dtype=np.float64) / scale + float(zero_point))
    q = np.clip(q, -32768, 32767).astype(np.int16)
    dequantized = (q.astype(np.float64) - float(zero_point)) * scale
    return q, dequantized


def _conv_output_shape(job: Job) -> Tuple[int, ...]:
    assert job.kernel_shape is not None
    n, h, w, _ = job.input_shape
    _, kh, kw, _ = job.kernel_shape
    ph, pw = job.padding
    sh, sw = job.stride
    out_h = (h + 2 * ph - kh) // sh + 1
    out_w = (w + 2 * pw - kw) // sw + 1
    if out_h <= 0 or out_w <= 0:
        raise DiagError(
            "E_SHAPE_MISMATCH",
            "conv2d geometry produces an empty output",
            {
                "job_id": job.job_id,
                "input_shape": list(job.input_shape),
                "kernel_shape": list(job.kernel_shape),
                "stride": list(job.stride),
                "padding": list(job.padding),
            },
        )
    return n, out_h, out_w, job.kernel_shape[0]


def _interpolate_output_shape(job: Job) -> Tuple[int, ...]:
    assert job.scale is not None
    n, h, w, c = job.input_shape
    out_h = max(1, int(math.floor(h * job.scale[0])))
    out_w = max(1, int(math.floor(w * job.scale[1])))
    return n, out_h, out_w, c


def build_partition_plan(job: Job, input_array: np.ndarray, kernel: Optional[np.ndarray]) -> Dict[str, Any]:
    if job.op_kind == "conv2d":
        assert job.kernel_shape is not None and kernel is not None
        output_shape = _conv_output_shape(job)
        n, h, w, c = job.input_shape
        out_c, kh, kw, kc = job.kernel_shape
        ph, pw = job.padding
        sh, sw = job.stride
        outputs: List[Dict[str, Any]] = []
        for bn in range(n):
            for oy in range(output_shape[1]):
                for ox in range(output_shape[2]):
                    for oc in range(out_c):
                        terms: List[List[int]] = []
                        for ky in range(kh):
                            iy = oy * sh - ph + ky
                            for kx in range(kw):
                                ix = ox * sw - pw + kx
                                for ic in range(c):
                                    if 0 <= iy < h and 0 <= ix < w:
                                        input_index = ((bn * h + iy) * w + ix) * c + ic
                                    else:
                                        input_index = -1
                                    kernel_index = ((oc * kh + ky) * kw + kx) * c + ic
                                    terms.append([input_index, kernel_index])
                        output_index = ((bn * output_shape[1] + oy) * output_shape[2] + ox) * out_c + oc
                        outputs.append({"output_index": output_index, "terms": terms})
        op_aux = {"kernel_shape": list(job.kernel_shape), "padding": list(job.padding), "stride": list(job.stride)}
    elif job.op_kind == "interpolate":
        assert job.scale is not None
        output_shape = _interpolate_output_shape(job)
        n, h, w, c = job.input_shape
        sh, sw = job.scale
        outputs = []
        for bn in range(n):
            for oy in range(output_shape[1]):
                for ox in range(output_shape[2]):
                    if job.interp_mode == "nearest":
                        sy = min(h - 1, max(0, int(math.floor((oy + 0.5) / sh))))
                        sx = min(w - 1, max(0, int(math.floor((ox + 0.5) / sw))))
                        spatial = [(sy, sx, 1.0)]
                    else:
                        src_y = min(float(h - 1), max(0.0, (oy + 0.5) / sh - 0.5))
                        src_x = min(float(w - 1), max(0.0, (ox + 0.5) / sw - 0.5))
                        y0, x0 = int(math.floor(src_y)), int(math.floor(src_x))
                        y1, x1 = min(h - 1, y0 + 1), min(w - 1, x0 + 1)
                        wy1, wx1 = src_y - y0, src_x - x0
                        wy0, wx0 = 1.0 - wy1, 1.0 - wx1
                        spatial = [
                            (y0, x0, wy0 * wx0),
                            (y0, x1, wy0 * wx1),
                            (y1, x0, wy1 * wx0),
                            (y1, x1, wy1 * wx1),
                        ]
                    for oc in range(c):
                        terms = [
                            [((bn * h + syi) * w + sxi) * c + oc, weight]
                            for syi, sxi, weight in spatial
                        ]
                        output_index = ((bn * output_shape[1] + oy) * output_shape[2] + ox) * c + oc
                        outputs.append({"output_index": output_index, "terms": terms})
        op_aux = {"scale": list(job.scale), "interp_mode": job.interp_mode}
    else:
        raise DiagError("E_PARAM_INVALID", "unsupported operator", {"job_id": job.job_id, "op_kind": job.op_kind})

    blocks = [int(math.ceil(len(item["terms"]) / job.block_size)) for item in outputs]
    return {
        "schema": "partition-plan-v1",
        "op_kind": job.op_kind,
        "input_shape": list(job.input_shape),
        "kernel_shape": list(job.kernel_shape) if job.kernel_shape is not None else None,
        "output_shape": list(output_shape),
        "block_size": job.block_size,
        "blocks_per_output": blocks,
        "outputs": outputs,
        "operator_aux": op_aux,
    }


def execute_block_op(
    job: Job,
    plan: Mapping[str, Any],
    input_array: np.ndarray,
    kernel: Optional[np.ndarray],
) -> Dict[str, Any]:
    input_flat = np.asarray(input_array, dtype=np.float64).reshape(-1)
    kernel_flat = np.asarray(kernel, dtype=np.float64).reshape(-1) if kernel is not None else np.empty(0)
    q_input, deq_input = _quantize_array(input_array, job.quant_scale, job.quant_zero_point)
    q_input_flat = q_input.reshape(-1)
    q_kernel, deq_kernel = _quantize_array(kernel, job.quant_scale, job.quant_zero_point) if kernel is not None else (
        np.empty(0, dtype=np.int16),
        np.empty(0, dtype=np.float64),
    )
    q_kernel_flat = q_kernel.reshape(-1)
    block_size = int(plan["block_size"])
    zero_point = job.quant_zero_point

    float_partials: List[List[float]] = []
    fixed_partials_int: List[List[int]] = []
    interp_weight_ref: List[float] = []
    interp_weight_dequant: List[float] = []

    for output in plan["outputs"]:
        terms = output["terms"]
        fp: List[float] = []
        fi: List[int] = []
        for start in range(0, len(terms), block_size):
            chunk = terms[start : start + block_size]
            partial_float = 0.0
            partial_int = 0
            for term in chunk:
                input_index = int(term[0])
                if job.op_kind == "conv2d":
                    kernel_index = int(term[1])
                    weight = float(kernel_flat[kernel_index])
                    if input_index >= 0:
                        value = float(input_flat[input_index])
                        centered_x = int(q_input_flat[input_index]) - zero_point
                    else:
                        value = 0.0
                        centered_x = 0
                    centered_w = int(q_kernel_flat[kernel_index]) - zero_point
                else:
                    weight = float(term[1])
                    value = float(input_flat[input_index])
                    centered_x = int(q_input_flat[input_index]) - zero_point
                    q_weight = int(np.clip(np.rint(weight / job.quant_scale + zero_point), -32768, 32767))
                    deq_weight = (q_weight - zero_point) * job.quant_scale
                    centered_w = q_weight - zero_point
                    interp_weight_ref.append(weight)
                    interp_weight_dequant.append(deq_weight)
                partial_float += value * weight
                partial_int += centered_x * centered_w
            fp.append(partial_float)
            fi.append(int(partial_int))
        float_partials.append(fp)
        fixed_partials_int.append(fi)

    return {
        "input_float": input_flat.tolist(),
        "input_q": q_input_flat.astype(np.int64).tolist(),
        "input_dequant": deq_input.reshape(-1).tolist(),
        "kernel_float": kernel_flat.tolist(),
        "kernel_q": q_kernel_flat.astype(np.int64).tolist(),
        "kernel_dequant": deq_kernel.reshape(-1).tolist(),
        "interp_weight_ref": interp_weight_ref,
        "interp_weight_dequant": interp_weight_dequant,
        "float_partials": float_partials,
        "fixed_partials_int": fixed_partials_int,
        "scale_factor": job.quant_scale * job.quant_scale,
        "quantization": {
            "dtype": "int16",
            "accumulator": "int64",
            "weight_quantization": "same quant_scale and quant_zero_point as input",
            "rounding": "numpy.rint (round-to-nearest-even)",
        },
    }


def execute_cross_block_reduction(plan: Mapping[str, Any], block_payload: Mapping[str, Any]) -> Dict[str, Any]:
    float_reduced: List[float] = []
    fixed_accum: List[int] = []
    float_partials = block_payload["float_partials"]
    fixed_partials = block_payload["fixed_partials_int"]
    if len(float_partials) != len(plan["outputs"]) or len(fixed_partials) != len(plan["outputs"]):
        raise DiagError("E_SELF_CHECK_FAILED", "block payload output count does not match the partition plan", {})
    for output_blocks_float, output_blocks_int in zip(float_partials, fixed_partials):
        total_float = 0.0
        for value in output_blocks_float:
            total_float += float(value)
        total_int = 0
        for value in output_blocks_int:
            total_int += int(value)
        float_reduced.append(total_float)
        fixed_accum.append(int(total_int))
    return {"float_reduced": float_reduced, "fixed_accum_int": fixed_accum}


def execute_dequant_output(block_payload: Mapping[str, Any], reduced_payload: Mapping[str, Any]) -> Dict[str, Any]:
    scale_factor = float(block_payload["scale_factor"])
    float_output = [float(value) for value in reduced_payload["float_reduced"]]
    fixed_output = [int(value) * scale_factor for value in reduced_payload["fixed_accum_int"]]
    return {
        "float_output": float_output,
        "fixed_output": fixed_output,
        "scale_factor": scale_factor,
    }


def _flat_input_positions(shape: Sequence[int], tensor_name: str) -> List[List[Any]]:
    count = int(np.prod(shape))
    return [[tensor_name] + index_position(i, shape) for i in range(count)]


def _output_positions(output_shape: Sequence[int]) -> List[List[int]]:
    return [index_position(i, output_shape) for i in range(int(np.prod(output_shape)))]


def build_stage_vectors(
    job: Job,
    plan: Mapping[str, Any],
    block_payload: Mapping[str, Any],
    reduced_payload: Mapping[str, Any],
    output_payload: Mapping[str, Any],
) -> Dict[str, Dict[str, Any]]:
    output_shape = plan["output_shape"]

    input_ref = [float(v) for v in block_payload["input_float"]]
    input_got = [float(v) for v in block_payload["input_dequant"]]
    input_positions = _flat_input_positions(job.input_shape, "input")
    if job.op_kind == "conv2d":
        kernel_ref = [float(v) for v in block_payload["kernel_float"]]
        kernel_got = [float(v) for v in block_payload["kernel_dequant"]]
        input_ref.extend(kernel_ref)
        input_got.extend(kernel_got)
        assert job.kernel_shape is not None
        input_positions.extend(_flat_input_positions(job.kernel_shape, "kernel"))
    else:
        weights_ref = [float(v) for v in block_payload["interp_weight_ref"]]
        weights_got = [float(v) for v in block_payload["interp_weight_dequant"]]
        input_ref.extend(weights_ref)
        input_got.extend(weights_got)
        input_positions.extend([["interp_weight", i] for i in range(len(weights_ref))])

    block_ref: List[float] = []
    block_got: List[float] = []
    block_positions: List[List[Any]] = []
    scale_factor = float(block_payload["scale_factor"])
    for output_index, (floats, ints) in enumerate(
        zip(block_payload["float_partials"], block_payload["fixed_partials_int"])
    ):
        for block_index, (float_value, int_value) in enumerate(zip(floats, ints)):
            block_ref.append(float(float_value))
            block_got.append(float(int_value) * scale_factor)
            block_positions.append(["block", output_index, block_index])

    reduce_positions = _output_positions(output_shape)
    reduce_ref = [float(v) for v in reduced_payload["float_reduced"]]
    reduce_got = [float(v) * scale_factor for v in reduced_payload["fixed_accum_int"]]
    dequant_ref = [float(v) for v in output_payload["float_output"]]
    dequant_got = [float(v) for v in output_payload["fixed_output"]]

    stages: Dict[str, Dict[str, Any]] = {}
    for name, reference, candidate, positions in (
        ("input_quantization", input_ref, input_got, input_positions),
        ("block_op_execution", block_ref, block_got, block_positions),
        ("cross_block_reduction", reduce_ref, reduce_got, reduce_positions),
        ("dequant_output", dequant_ref, dequant_got, reduce_positions),
    ):
        if len(reference) != len(candidate):
            raise DiagError("E_SELF_CHECK_FAILED", "stage reference/candidate lengths differ", {"stage": name})
        stages[name] = {
            "reference": reference,
            "candidate": candidate,
            "positions": positions,
            "checksum": sha256_json({"reference": reference, "candidate": candidate}),
        }
    return stages


def compute_stage_stats(reference: Sequence[float], candidate: Sequence[float], positions: Sequence[Any]) -> Dict[str, Any]:
    ref = np.asarray(reference, dtype=np.float64)
    got = np.asarray(candidate, dtype=np.float64)
    if ref.shape != got.shape:
        raise DiagError("E_SELF_CHECK_FAILED", "stage vectors have different lengths", {"reference": int(ref.size), "candidate": int(got.size)})
    abs_error = np.abs(got - ref)
    rel_error = abs_error / np.maximum(np.abs(ref), 1e-12)
    if ref.size == 0:
        return {
            "max_abs": 0.0,
            "mean_abs": 0.0,
            "max_rel": 0.0,
            "position": [],
            "reference_value": 0.0,
            "candidate_value": 0.0,
            "sample_count": 0,
        }
    max_abs_index = int(np.argmax(abs_error))
    max_rel_index = int(np.argmax(rel_error))
    return {
        "max_abs": float(abs_error[max_abs_index]),
        "mean_abs": float(np.mean(abs_error)),
        "max_rel": float(rel_error[max_rel_index]),
        "position": positions[max_abs_index],
        "reference_value": float(ref[max_abs_index]),
        "candidate_value": float(got[max_abs_index]),
        "sample_count": int(ref.size),
    }


def build_diagnostics(job: Job, stages: Mapping[str, Mapping[str, Any]], default_tolerance: float = 0.5) -> Dict[str, Any]:
    stage_stats: Dict[str, Dict[str, Any]] = {}
    previous_max: Optional[float] = None
    for name in STAGE_ORDER:
        stage = stages[name]
        stats = compute_stage_stats(stage["reference"], stage["candidate"], stage["positions"])
        if previous_max is None:
            amplification: Optional[float] = 1.0
        elif previous_max <= 1e-15:
            amplification = None
        else:
            amplification = float(stats["max_abs"] / previous_max)
        stats["amplification_ratio"] = amplification
        stats["amplification_vs"] = None if previous_max is None else "previous_stage"
        stage_stats[name] = stats
        previous_max = float(stats["max_abs"])

    dominant = max(STAGE_ORDER, key=lambda stage_name: stage_stats[stage_name]["max_abs"])
    violation: Optional[Dict[str, Any]] = None
    for name in STAGE_ORDER:
        tolerance = float(job.tolerance_map.get(name, default_tolerance))
        stats = stage_stats[name]
        if stats["max_abs"] > tolerance:
            violation = {
                "job_id": job.job_id,
                "stage": name,
                "layer": name,
                "position": stats["position"],
                "max_abs": stats["max_abs"],
                "max_rel": stats["max_rel"],
                "tolerance": tolerance,
            }
            break
    return {
        "job_id": job.job_id,
        "stages": stage_stats,
        "dominant_error_source": dominant,
        "dominant_stage": dominant,
        "passed": violation is None,
        "violation": violation,
        "tolerance": {name: float(job.tolerance_map.get(name, default_tolerance)) for name in STAGE_ORDER},
    }


def build_ledger_record(
    job: Job,
    run_id: str,
    config_generation: int,
    cache_chain: Sequence[Mapping[str, Any]],
    stages: Mapping[str, Mapping[str, Any]],
    diagnostics: Mapping[str, Any],
) -> Dict[str, Any]:
    record = {
        "schema": "diagnostic-error-ledger-v1",
        "job_id": job.job_id,
        "run_id": run_id,
        "job": job.semantic_dict(),
        "config_generation": int(config_generation),
        "cache_chain": [dict(item) for item in cache_chain],
        "stages": {
            name: {
                "reference": [float(v) for v in stages[name]["reference"]],
                "candidate": [float(v) for v in stages[name]["candidate"]],
                "positions": list(stages[name]["positions"]),
                "checksum": stages[name]["checksum"],
            }
            for name in STAGE_ORDER
        },
        "diagnostics": diagnostics,
        "status": "committed",
    }
    record["record_checksum"] = sha256_json({k: v for k, v in record.items() if k != "record_checksum"})
    return record


def _close_enough(left: float, right: float, rel: float = 1e-12, abs_tol: float = 1e-12) -> bool:
    return abs(float(left) - float(right)) <= max(abs_tol, rel * max(abs(left), abs(right), 1.0))


def replay_ledger(path: Path) -> Dict[str, Any]:
    """Recompute every ledger statistic from self-contained stage vectors."""

    ledger = LedgerStore(path)
    records = ledger.load_records()
    results: List[Dict[str, Any]] = []
    all_ok = True
    for record in records:
        checksum = record.get("record_checksum")
        if checksum != sha256_json({k: v for k, v in record.items() if k != "record_checksum"}):
            raise DiagError("E_SELF_CHECK_FAILED", "ledger record checksum mismatch", {"job_id": record.get("job_id")})
        stored_diag = record.get("diagnostics", {})
        stages_data = record.get("stages", {})
        errors: List[str] = []
        recomputed_stats: Dict[str, Any] = {}
        for name in STAGE_ORDER:
            stage = stages_data.get(name)
            if not isinstance(stage, Mapping):
                errors.append(f"missing stage {name}")
                continue
            ref = np.asarray(stage.get("reference", []), dtype=np.float64)
            got = np.asarray(stage.get("candidate", []), dtype=np.float64)
            if sha256_json({"reference": ref.tolist(), "candidate": got.tolist()}) != stage.get("checksum"):
                errors.append(f"stage checksum mismatch: {name}")
            stats = compute_stage_stats(ref.tolist(), got.tolist(), stage.get("positions", []))
            stored = stored_diag.get("stages", {}).get(name, {})
            for field_name in ("max_abs", "mean_abs", "max_rel"):
                if field_name not in stored or not _close_enough(stats[field_name], stored[field_name]):
                    errors.append(f"stage statistic mismatch: {name}.{field_name}")
            if stats["position"] != stored.get("position"):
                errors.append(f"stage position mismatch: {name}")
            recomputed_stats[name] = stats
        if recomputed_stats:
            dominant = max(STAGE_ORDER, key=lambda stage_name: recomputed_stats[stage_name]["max_abs"])
            if dominant != stored_diag.get("dominant_error_source"):
                errors.append("dominant error source mismatch")
            violation = None
            for name in STAGE_ORDER:
                tolerance = float(stored_diag.get("tolerance", {}).get(name, 0.0))
                if recomputed_stats[name]["max_abs"] > tolerance:
                    violation = {
                        "job_id": record.get("job_id"),
                        "stage": name,
                        "layer": name,
                        "position": recomputed_stats[name]["position"],
                        "max_abs": recomputed_stats[name]["max_abs"],
                        "max_rel": recomputed_stats[name]["max_rel"],
                        "tolerance": tolerance,
                    }
                    break
            if (violation is None) != bool(stored_diag.get("passed")):
                errors.append("tolerance verdict mismatch")
        all_ok = all_ok and not errors
        results.append(
            {
                "job_id": record.get("job_id"),
                "ok": not errors,
                "errors": errors,
                "dominant_error_source": stored_diag.get("dominant_error_source"),
                "passed": bool(stored_diag.get("passed")),
            }
        )
    return {"schema": "ledger-replay-report-v1", "ok": all_ok, "jobs": results, "job_count": len(results)}


@dataclass
class EngineOptions:
    max_resident_bytes: int = 64 * 1024 * 1024
    max_jobs_per_batch: int = 32
    max_cache_entries: int = 1024
    max_parallel_recompute: int = 4
    default_tolerance: float = 0.5

    def validate(self) -> None:
        if self.max_resident_bytes <= 0:
            raise DiagError("E_PARAM_INVALID", "max_resident_bytes must be positive", {})
        if self.max_jobs_per_batch <= 0:
            raise DiagError("E_PARAM_INVALID", "max_jobs_per_batch must be positive", {})
        if self.max_cache_entries <= 0:
            raise DiagError("E_PARAM_INVALID", "max_cache_entries must be positive", {})
        if self.max_parallel_recompute <= 0:
            raise DiagError("E_PARAM_INVALID", "max_parallel_recompute must be positive", {})
        if self.default_tolerance < 0 or not math.isfinite(self.default_tolerance):
            raise DiagError("E_PARAM_INVALID", "default_tolerance must be finite and non-negative", {})


@dataclass
class RunResult:
    run_id: str
    status: str
    completed_jobs: List[str]
    checkpoint_skipped_jobs: List[str]
    recomputed: List[Dict[str, Any]]
    reused: List[Dict[str, Any]]
    plan: Dict[str, Any]
    checkpoint_version: int
    checkpoint_path: str
    ledger_path: str
    degraded: List[Dict[str, Any]]
    resume_source: Optional[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "diagnostic-run-result-v1",
            "run_id": self.run_id,
            "status": self.status,
            "completed_jobs": self.completed_jobs,
            "checkpoint_skipped_jobs": self.checkpoint_skipped_jobs,
            "recompute_count": len(self.recomputed),
            "reuse_count": len(self.reused),
            "recomputed": self.recomputed,
            "reused": self.reused,
            "plan": self.plan,
            "checkpoint_version": self.checkpoint_version,
            "checkpoint_path": self.checkpoint_path,
            "ledger_path": self.ledger_path,
            "degraded": self.degraded,
            "resume_source": self.resume_source,
        }


class ManualClock:
    def __init__(self, now_ms: int = 0):
        self._now_ms = int(now_ms)

    def now_ms(self) -> int:
        return self._now_ms

    def set(self, now_ms: int) -> None:
        self._now_ms = int(now_ms)

    def advance(self, delta_ms: int) -> int:
        self._now_ms += int(delta_ms)
        return self._now_ms


class SystemClock:
    def now_ms(self) -> int:
        return int(time.time() * 1000)


def _job_layout_signature(job: Job) -> Dict[str, Any]:
    return {
        "op_kind": job.op_kind,
        "input_shape": list(job.input_shape),
        "kernel_shape": list(job.kernel_shape) if job.kernel_shape is not None else None,
        "scale": list(job.scale) if job.scale is not None else None,
        "stride": list(job.stride),
        "padding": list(job.padding),
        "interp_mode": job.interp_mode,
        "block_size": job.block_size,
    }


def _load_baseline(root: Path) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    jobs_doc = load_state_json(root / "state" / "jobs_baseline.json", {"jobs": []})
    config_doc = load_state_json(root / "state" / "config_baseline.json", {"records": []})
    jobs = {
        str(item["job_id"]): dict(item)
        for item in jobs_doc.get("jobs", [])
        if isinstance(item, Mapping) and "job_id" in item
    }
    configs = {
        str(item["config_key"]): dict(item)
        for item in config_doc.get("records", [])
        if isinstance(item, Mapping) and "config_key" in item
    }
    return jobs, configs


def _estimate_job_bytes(job: Job) -> int:
    input_elements = int(np.prod(job.input_shape))
    kernel_elements = int(np.prod(job.kernel_shape)) if job.kernel_shape is not None else 0
    if job.op_kind == "conv2d":
        output_elements = int(np.prod(_conv_output_shape(job)))
    else:
        output_elements = int(np.prod(_interpolate_output_shape(job)))
    padded_elements = int(
        np.prod(
            (
                job.input_shape[0],
                job.input_shape[1] + 2 * job.padding[0],
                job.input_shape[2] + 2 * job.padding[1],
                job.input_shape[3],
            )
        )
    )
    element_count = input_elements + kernel_elements + output_elements + padded_elements
    return int(element_count * 8 * 3 + output_elements * 8 * 2)


def build_invalidation_plan(
    jobs: Sequence[Job],
    config_view: ConfigView,
    baseline_jobs: Mapping[str, Mapping[str, Any]],
    baseline_configs: Mapping[str, Mapping[str, Any]],
    cache: CacheStore,
    sample_store: SampleStore,
    manifest_hash: str,
    max_parallel_recompute: int,
    completed_override: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    current_by_id = {job.job_id: job for job in jobs}
    completed = set(completed_override or [])
    affected_jobs: List[str] = []
    unaffected_jobs: List[str] = []
    reasons_by_job: Dict[str, List[str]] = {}
    removed_jobs = sorted(set(baseline_jobs) - set(current_by_id))

    for job in jobs:
        reasons: List[str] = []
        old = baseline_jobs.get(job.job_id)
        record = config_view.record_for_job(job)
        if old is None:
            reasons.append("new_job")
        else:
            if int(old.get("revision", 0)) != job.revision:
                reasons.append("revision")
            old_layout = {
                "op_kind": old.get("op_kind"),
                "input_shape": old.get("input_shape"),
                "kernel_shape": old.get("kernel_shape"),
                "scale": old.get("scale"),
                "stride": old.get("stride"),
                "padding": old.get("padding"),
                "interp_mode": old.get("interp_mode"),
                "block_size": old.get("block_size"),
            }
            if old_layout != _job_layout_signature(job):
                if old_layout.get("block_size") != job.block_size or old_layout.get("interp_mode") != job.interp_mode:
                    reasons.append("layout")
                else:
                    reasons.append("job_definition")
            old_without_revision = dict(old)
            old_without_revision.pop("revision", None)
            current_without_revision = job.semantic_dict()
            current_without_revision.pop("revision", None)
            if old_without_revision != current_without_revision and not reasons:
                reasons.append("job_definition")
        old_config = baseline_configs.get(job.config_key)
        if old_config is not None and int(old_config.get("generation", -1)) != record.generation:
            reasons.append("config_generation")
        if old_config is not None and (
            old_config.get("quant_scale") != record.quant_scale
            or old_config.get("quant_zero_point") != record.quant_zero_point
        ):
            reasons.append("quant_config")
        if job.job_id in completed:
            reasons = reasons or ["checkpoint_resume"]
        if reasons:
            affected_jobs.append(job.job_id)
            reasons_by_job[job.job_id] = sorted(set(reasons))
        else:
            unaffected_jobs.append(job.job_id)

    generation_changed_keys = {
        key
        for key, old_record in baseline_configs.items()
        if key in config_view.records
        and int(old_record.get("generation", -1)) != config_view.records[key].generation
    }
    for job in jobs:
        if job.config_key in generation_changed_keys:
            if "config_generation" not in reasons_by_job.setdefault(job.job_id, []):
                reasons_by_job[job.job_id].append("config_generation")
            if job.job_id not in affected_jobs:
                affected_jobs.append(job.job_id)
            if job.job_id in unaffected_jobs:
                unaffected_jobs.remove(job.job_id)

    input_hashes: Dict[str, str] = {}
    kernel_hashes: Dict[str, str] = {}
    for job in jobs:
        input_array = sample_store.load_input(job)
        kernel = sample_store.load_kernel(job)
        input_hashes[job.job_id] = array_content_hash(input_array)
        kernel_hashes[job.job_id] = array_content_hash(kernel) if kernel is not None else ""

    reuse: List[Dict[str, Any]] = []
    recompute: List[Dict[str, Any]] = []
    unaffected_reuse: List[Dict[str, Any]] = []
    affected_operations: List[Dict[str, Any]] = []

    def plan_job_chain(job: Job) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        record = config_view.record_for_job(job)
        upstream = sha256_json(
            {"input_content_hash": input_hashes[job.job_id], "kernel_content_hash": kernel_hashes[job.job_id]}
        )
        local_reuse: List[Dict[str, Any]] = []
        local_recompute: List[Dict[str, Any]] = []
        chain_open = True
        for node in NODES:
            key = cache_public_key(
                node,
                job,
                record.generation,
                input_hashes[job.job_id],
                kernel_hashes[job.job_id],
                upstream,
            )
            entry = cache.peek(key) if chain_open else None
            if chain_open and entry is not None:
                local_reuse.append({"job_id": job.job_id, "node": node, "cache_key": key, "product_hash": entry["product_hash"]})
                upstream = entry["product_hash"]
            else:
                local_recompute.append(
                    {
                        "job_id": job.job_id,
                        "node": node,
                        "cache_key": key if chain_open else None,
                        "reason": "changed_key" if chain_open else "downstream_of_recompute",
                    }
                )
                chain_open = False
                upstream = ""
        return local_reuse, local_recompute

    unaffected_candidate_reuse: Dict[str, List[Dict[str, Any]]] = {}
    for job in jobs:
        if job.job_id in reasons_by_job:
            continue
        local_reuse, local_recompute = plan_job_chain(job)
        if local_recompute:
            reasons_by_job[job.job_id] = ["cache_miss"]
            affected_jobs.append(job.job_id)
            if job.job_id in unaffected_jobs:
                unaffected_jobs.remove(job.job_id)
        else:
            unaffected_candidate_reuse[job.job_id] = local_reuse

    for job in jobs:
        if job.job_id not in reasons_by_job:
            continue
        local_reuse, local_recompute = plan_job_chain(job)
        reuse.extend(local_reuse)
        recompute.extend(local_recompute)

    affected_set = {job.job_id for job in jobs if job.job_id in reasons_by_job}
    affected_operations = [{"job_id": job_id, "node": node} for job_id in sorted(affected_set) for node in NODES]
    affected_reuse = [item for item in reuse if item["job_id"] in affected_set]
    affected_recompute = [item for item in recompute if item["job_id"] in affected_set]
    unaffected_reuse = [
        item
        for job_id in unaffected_jobs
        for item in unaffected_candidate_reuse.get(job_id, [])
    ]

    reuse_pairs = {(item["job_id"], item["node"]) for item in affected_reuse}
    recompute_pairs = {(item["job_id"], item["node"]) for item in affected_recompute}
    expected_pairs = {(item["job_id"], item["node"]) for item in affected_operations}
    if reuse_pairs & recompute_pairs:
        raise DiagError("E_SELF_CHECK_FAILED", "invalidation plan reuse/recompute sets overlap", {})
    missing = expected_pairs - (reuse_pairs | recompute_pairs)
    extra = (reuse_pairs | recompute_pairs) - expected_pairs
    if missing or extra:
        raise DiagError(
            "E_SELF_CHECK_FAILED",
            "invalidation plan does not cover affected operations exactly",
            {"missing": sorted(missing), "extra": sorted(extra)},
        )

    degraded: List[Dict[str, Any]] = []
    recompute_jobs = sorted({item["job_id"] for item in affected_recompute})
    if len(recompute_jobs) > max_parallel_recompute:
        degraded.append(
            {
                "category": "PARALLEL_TO_SERIAL",
                "reason": "parallel recompute request exceeds configured limit",
                "requested_jobs": len(recompute_jobs),
                "max_parallel_recompute": int(max_parallel_recompute),
            }
        )

    scenario_groups: Dict[str, List[str]] = {}
    for job_id, job_reasons in reasons_by_job.items():
        for reason in job_reasons:
            scenario_groups.setdefault(reason, []).append(job_id)
    for group in scenario_groups.values():
        group.sort()

    return {
        "schema": "invalidation-plan-v1",
        "run_manifest_hash": manifest_hash,
        "affected_jobs": sorted(affected_set),
        "unaffected_jobs": sorted(set(unaffected_jobs)),
        "removed_jobs": removed_jobs,
        "reasons_by_job": {k: sorted(v) for k, v in sorted(reasons_by_job.items())},
        "scenario_groups": {k: sorted(v) for k, v in sorted(scenario_groups.items())},
        "affected_operations": affected_operations,
        "recompute": affected_recompute,
        "reuse": affected_reuse,
        "unaffected_reuse": unaffected_reuse,
        "degraded": degraded,
        "cache_missing_jobs": sorted(
            [job_id for job_id, reasons in reasons_by_job.items() if "cache_miss" in reasons]
        ),
        "config_generations": {
            job.config_key: config_view.record_for_job(job).generation for job in jobs
        },
    }


def _semantic_config_state(records: Sequence[QuantConfigRecord]) -> List[Dict[str, Any]]:
    return sorted(
        [
            {
                **record.active_dict(),
                "provenance": record.provenance,
            }
            for record in records
            if not record.tombstone
        ],
        key=lambda item: item["config_key"],
    )


def assert_no_config_rollback(
    config_view: ConfigView,
    baseline_configs: Mapping[str, Mapping[str, Any]],
) -> None:
    for config_key, old_record in baseline_configs.items():
        current = config_view.records.get(config_key)
        if current is not None and current.generation < int(old_record.get("generation", 0)):
            raise DiagError(
                "E_SNAPSHOT_ROLLBACK",
                "applying a configuration snapshot older than the committed generation is rejected",
                {
                    "config_key": config_key,
                    "committed_generation": int(old_record.get("generation", 0)),
                    "attempted_generation": current.generation,
                },
            )


def update_v2_generation(
    v2_dir: Path,
    config_key: str,
    new_generation: Optional[int] = None,
    effective_abs_ms: Optional[int] = None,
    lock_path: Optional[Path] = None,
) -> QuantConfigRecord:
    def mutate() -> QuantConfigRecord:
        records = read_v2_records(v2_dir)
        current = next((record for record in records if record.config_key == config_key), None)
        if current is None:
            raise DiagError("E_PARAM_INVALID", "configuration does not exist", {"config_key": config_key})
        target_generation = current.generation + 1 if new_generation is None else int(new_generation)
        if target_generation < current.generation:
            raise DiagError(
                "E_SNAPSHOT_ROLLBACK",
                "configuration generation rollback is rejected",
                {
                    "config_key": config_key,
                    "current_generation": current.generation,
                    "requested_generation": target_generation,
                },
            )
        target_effective = current.effective_abs_ms if effective_abs_ms is None else int(effective_abs_ms)
        replacement = QuantConfigRecord(
            config_key=current.config_key,
            effective_abs_ms=target_effective,
            generation=target_generation,
            quant_scale=current.quant_scale,
            quant_zero_point=current.quant_zero_point,
            block_size=current.block_size,
            revision=current.revision,
            tombstone=current.tombstone,
            provenance=current.provenance,
        )
        updated = [replacement if record.config_key == config_key else record for record in records]
        write_v2_records(v2_dir, updated)
        return replacement

    if lock_path is None:
        return mutate()
    with StageLock(lock_path):
        return mutate()


class Engine:
    def __init__(
        self,
        root: Path,
        options: Optional[EngineOptions] = None,
        clock: Optional[Any] = None,
        base_epoch_ms: int = 0,
        run_self_check: bool = True,
    ):
        self.root = Path(root).resolve()
        self.options = options or EngineOptions()
        self.options.validate()
        self.clock = clock or SystemClock()
        self.base_epoch_ms = int(base_epoch_ms)
        self.run_self_check = bool(run_self_check)
        self.cache = CacheStore(self.root / "cache")
        self.sample_store = SampleStore(self.root)
        self.lock_path = self.root / "state" / "stage.lock"
        self.state_dir = self.root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._graceful_requested = False
        self._old_sigterm: Any = None

    def _install_signal_handlers(self) -> None:
        if not hasattr(signal, "SIGTERM"):
            return
        try:
            self._old_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, self._handle_graceful_signal)
            signal.signal(signal.SIGINT, self._handle_graceful_signal)
        except ValueError:
            self._old_sigterm = None

    def _restore_signal_handlers(self) -> None:
        if self._old_sigterm is None:
            return
        with contextlib.suppress(ValueError):
            signal.signal(signal.SIGTERM, self._old_sigterm)
            signal.signal(signal.SIGINT, signal.default_int_handler)
        self._old_sigterm = None

    def _handle_graceful_signal(self, signum: int, frame: Any) -> None:
        self._graceful_requested = True

    def _load_config(self, config_path: Optional[Path], config_kind: str = "auto") -> ConfigView:
        if config_path is None:
            return synthetic_config_view([])
        path = Path(config_path)
        if config_kind == "v2" or (config_kind == "auto" and path.is_dir()):
            return ConfigView.from_v2(path)
        if config_kind == "v1":
            return ConfigView.from_v1(path, base_epoch_ms=self.base_epoch_ms)
        if path.is_file():
            try:
                document = read_json(path)
            except json.JSONDecodeError as exc:
                raise DiagError("E_PARAM_INVALID", "config is not valid JSON", {"path": str(path), "error": str(exc)})
            if isinstance(document, Mapping) and document.get("schema") in ("quant-config-v1", "quant-config-v1.0"):
                return ConfigView.from_v1(path, base_epoch_ms=self.base_epoch_ms)
        raise DiagError("E_PARAM_INVALID", "unable to infer config snapshot kind", {"path": str(path), "config_kind": config_kind})

    def _run_id(self, manifest_hash: str, config_view: ConfigView) -> str:
        return sha256_json(
            {
                "schema": "diagnostic-run-v1",
                "manifest_hash": manifest_hash,
                "config_semantic_hash": config_view.semantic_hash,
            }
        )

    def _previous_revisions(self) -> Dict[str, int]:
        value = load_state_json(self.state_dir / "job_revisions.json", {"revisions": {}})
        revisions = value.get("revisions", {}) if isinstance(value, Mapping) else {}
        return {str(k): int(v) for k, v in revisions.items()} if isinstance(revisions, Mapping) else {}

    def _merge_revisions(self, revisions: Mapping[str, int], jobs: Sequence[Job]) -> Dict[str, int]:
        merged = {str(k): int(v) for k, v in revisions.items()}
        for job in jobs:
            merged[job.job_id] = max(int(merged.get(job.job_id, 0)), int(job.revision))
        return merged

    def _save_revisions(self, revisions: Mapping[str, int]) -> None:
        save_state_json(self.state_dir / "job_revisions.json", {"schema": "job-revisions-v1", "revisions": dict(revisions)})

    def _save_baseline(self, jobs: Sequence[Job], config_view: ConfigView) -> None:
        save_state_json(
            self.state_dir / "jobs_baseline.json",
            {"schema": "jobs-baseline-v1", "jobs": [job.semantic_dict() for job in jobs]},
        )
        save_state_json(
            self.state_dir / "config_baseline.json",
            {"schema": "config-baseline-v1", "records": _semantic_config_state(config_view.records.values())},
        )

    def _stage_pipeline(
        self,
        job: Job,
        config_record: QuantConfigRecord,
        run_id: str,
        stage_hook: Optional[Callable[[Dict[str, Any]], None]],
    ) -> Dict[str, Any]:
        input_array = self.sample_store.load_input(job)
        kernel = self.sample_store.load_kernel(job)
        input_hash = array_content_hash(input_array)
        kernel_hash = array_content_hash(kernel) if kernel is not None else ""
        upstream = sha256_json({"input_content_hash": input_hash, "kernel_content_hash": kernel_hash})

        staged: Dict[str, Dict[str, Any]] = {}
        chain: List[Dict[str, Any]] = []
        payloads: Dict[str, Dict[str, Any]] = {}
        actions: List[Dict[str, Any]] = []
        source_payload: Optional[Dict[str, Any]] = None

        for node in NODES:
            key = cache_public_key(node, job, config_record.generation, input_hash, kernel_hash, upstream)
            entry = self.cache.peek(key)
            if entry is not None:
                action = "reused"
                payload = entry["payload"]
                product_hash = entry["product_hash"]
            else:
                action = "recomputed"
                if node == NODE_INPUT:
                    plan = build_partition_plan(job, input_array, kernel)
                    payload = {
                        "plan": plan,
                        "input_content_hash": input_hash,
                        "kernel_content_hash": kernel_hash,
                        "revision": job.revision,
                        "job_id": job.job_id,
                    }
                elif node == NODE_BLOCK:
                    assert source_payload is not None
                    payload = execute_block_op(job, source_payload["plan"], input_array, kernel)
                elif node == NODE_REDUCE:
                    payload = execute_cross_block_reduction(payloads[NODE_INPUT]["plan"], payloads[NODE_BLOCK])
                elif node == NODE_DEQUANT:
                    payload = execute_dequant_output(payloads[NODE_BLOCK], payloads[NODE_REDUCE])
                else:
                    raise DiagError("E_SELF_CHECK_FAILED", "unknown cache node", {"node": node})
                entry, index_meta = self.cache.stage_entry(
                    node,
                    key,
                    job.job_id,
                    payload,
                    run_id,
                    job.logical_clock_ms,
                    self.options.max_cache_entries,
                    reserved_entries=len(staged),
                )
                staged[key] = index_meta
                product_hash = entry["product_hash"]
            payloads[node] = payload
            source_payload = payload
            upstream = product_hash
            action_record = {
                "job_id": job.job_id,
                "node": node,
                "action": action,
                "cache_key": key,
                "product_hash": product_hash,
            }
            actions.append(action_record)
            chain.append({"node": node, "cache_key": key, "product_hash": product_hash, "action": action})
            if stage_hook is not None:
                stage_hook(action_record)

        plan = payloads[NODE_INPUT]["plan"]
        stages = build_stage_vectors(job, plan, payloads[NODE_BLOCK], payloads[NODE_REDUCE], payloads[NODE_DEQUANT])
        diagnostics = build_diagnostics(job, stages, self.options.default_tolerance)
        if not diagnostics["passed"]:
            raise DiagError("E_TOLERANCE_EXCEEDED", "operator quantization error exceeds tolerance", diagnostics["violation"] or {})
        ledger_record = build_ledger_record(job, run_id, config_record.generation, chain, stages, diagnostics)
        return {
            "job": job,
            "config_record": config_record,
            "staged": staged,
            "chain": chain,
            "actions": actions,
            "ledger_record": ledger_record,
            "diagnostics": diagnostics,
        }

    def run(
        self,
        manifest_path: Path,
        config_path: Optional[Path] = None,
        config_kind: str = "auto",
        stage_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        graceful_after_jobs: Optional[int] = None,
    ) -> RunResult:
        self.options.validate()
        manifest_path = Path(manifest_path)
        jobs, manifest_hash = load_manifest(manifest_path)
        if len(jobs) > self.options.max_jobs_per_batch:
            raise DiagError(
                "E_RESOURCE_LIMIT",
                "job batch exceeds max_jobs_per_batch",
                {"job_count": len(jobs), "limit": self.options.max_jobs_per_batch},
            )
        for job in jobs:
            estimated = _estimate_job_bytes(job)
            if estimated > self.options.max_resident_bytes:
                raise DiagError(
                    "E_RESOURCE_LIMIT",
                    "estimated resident matrix memory exceeds limit",
                    {"job_id": job.job_id, "estimated_bytes": estimated, "limit": self.options.max_resident_bytes},
                )
        self.sample_store.validate(jobs)
        previous_revisions = self._previous_revisions()
        validate_revisions(jobs, previous_revisions)

        with StageLock(self.lock_path):
            config_view = self._load_config(config_path, config_kind)
            if config_path is None:
                config_view = synthetic_config_view(jobs)
            config_view.validate_jobs(jobs)
            run_id = self._run_id(manifest_hash, config_view)
            checkpoint_store = CheckpointStore(self.root / "checkpoints" / run_id)
            ledger = LedgerStore(self.root / "ledger" / f"{run_id}.jsonl")
            checkpoint = checkpoint_store.load_latest()
            completed: set[str] = set()
            resume_source: Optional[str] = None
            checkpoint_skipped: List[str] = []
            ledger_bytes = 0
            ledger_sha = sha256_bytes(b"")
            if checkpoint is not None:
                if checkpoint.get("manifest_hash") != manifest_hash or checkpoint.get("config_hash") != config_view.semantic_hash:
                    raise DiagError("E_RECOVERY_FAILED", "checkpoint identity does not match the current run", {})
                cache_snapshot = checkpoint.get("cache_index", {})
                if not isinstance(cache_snapshot, Mapping):
                    raise DiagError("E_RECOVERY_FAILED", "checkpoint cache index is malformed", {})
                self.cache.restore_index(cache_snapshot)
                self.cache.cleanup_orphans()
                ledger.truncate_and_verify(int(checkpoint.get("ledger_bytes", 0)), str(checkpoint.get("ledger_sha256", "")))
                completed = {str(item) for item in checkpoint.get("completed_jobs", [])}
                ledger_bytes = int(checkpoint.get("ledger_bytes", 0))
                ledger_sha = str(checkpoint.get("ledger_sha256", ""))
                resume_source = f"checkpoint:{checkpoint_store.directory / ('checkpoint_%08d.json' % int(checkpoint['version']))}"
                previous_revisions = {
                    **previous_revisions,
                    **{str(k): int(v) for k, v in checkpoint.get("job_revisions", {}).items()},
                }
                validate_revisions(jobs, previous_revisions)
                ledger_records = ledger.load_records()
                seen_ledger_jobs: set[str] = set()
                for record in ledger_records:
                    job_id = str(record.get("job_id"))
                    if job_id in seen_ledger_jobs:
                        raise DiagError("E_RECOVERY_FAILED", "ledger contains duplicate committed job records", {"job_id": job_id})
                    seen_ledger_jobs.add(job_id)
                if not completed.issubset(seen_ledger_jobs):
                    raise DiagError("E_RECOVERY_FAILED", "checkpoint completed jobs are missing from the ledger", {})
            else:
                ledger.reset()
                self.cache.cleanup_orphans()
                previous_revisions = self._merge_revisions(previous_revisions, jobs)

            baseline_jobs, baseline_configs = _load_baseline(self.root)
            assert_no_config_rollback(config_view, baseline_configs)
            plan = build_invalidation_plan(
                jobs,
                config_view,
                baseline_jobs,
                baseline_configs,
                self.cache,
                self.sample_store,
                manifest_hash,
                self.options.max_parallel_recompute,
            )
            if checkpoint is not None and all(job.job_id in completed for job in jobs) and checkpoint.get("status") == "completed":
                checkpoint_skipped = [job.job_id for job in jobs if job.job_id in completed]
                self._save_baseline(jobs, config_view)
                plan["actual_actions"] = []
                plan["checkpoint_skipped_jobs"] = checkpoint_skipped
                save_state_json(self.root / "plans" / f"{run_id}.json", plan)
                result = RunResult(
                    run_id=run_id,
                    status="completed",
                    completed_jobs=sorted(completed),
                    checkpoint_skipped_jobs=checkpoint_skipped,
                    recomputed=[],
                    reused=[],
                    plan=plan,
                    checkpoint_version=int(checkpoint["version"]),
                    checkpoint_path=str(checkpoint_store.directory / f"checkpoint_{int(checkpoint['version']):08d}.json"),
                    ledger_path=str(ledger.path),
                    degraded=plan.get("degraded", []),
                    resume_source=resume_source,
                )
                if self.run_self_check:
                    self.self_check(run_id, plan)
                return result

            self._graceful_requested = False
            self._install_signal_handlers()
            actual_actions: List[Dict[str, Any]] = []
            graceful_limit = int(graceful_after_jobs) if graceful_after_jobs is not None else None
            newly_committed = 0
            status = "running"
            checkpoint_version = int(checkpoint["version"]) if checkpoint else 0
            checkpoint_path = (
                str(checkpoint_store.directory / f"checkpoint_{checkpoint_version:08d}.json") if checkpoint else ""
            )
            revisions = self._merge_revisions(previous_revisions, jobs)
            try:
                for job in jobs:
                    if job.job_id in completed:
                        checkpoint_skipped.append(job.job_id)
                        continue
                    pipeline = self._stage_pipeline(job, config_view.record_for_job(job), run_id, stage_hook)
                    self.cache.commit_staged(pipeline["staged"])
                    ledger_bytes, ledger_sha = ledger.append(pipeline["ledger_record"])
                    completed.add(job.job_id)
                    newly_committed += 1
                    actual_actions.extend(pipeline["actions"])
                    revisions = self._merge_revisions(revisions, [job])
                    checkpoint = checkpoint_store.commit(
                        run_id=run_id,
                        status="running",
                        completed_jobs=sorted(completed),
                        job_revisions=revisions,
                        ledger_bytes=ledger_bytes,
                        ledger_sha256=ledger_sha,
                        cache_index=self.cache.index,
                        manifest_hash=manifest_hash,
                        config_hash=config_view.semantic_hash,
                        logical_clock_ms=int(self.clock.now_ms()),
                        details={"last_job_id": job.job_id},
                    )
                    checkpoint_version = int(checkpoint["version"])
                    checkpoint_path = str(checkpoint_store.directory / f"checkpoint_{checkpoint_version:08d}.json")
                    self._save_revisions(revisions)
                    if self._graceful_requested or (graceful_limit is not None and newly_committed >= graceful_limit):
                        status = "graceful_stop"
                        break
                if status == "running":
                    status = "completed"
                    checkpoint = checkpoint_store.commit(
                        run_id=run_id,
                        status=status,
                        completed_jobs=sorted(completed),
                        job_revisions=revisions,
                        ledger_bytes=ledger_bytes,
                        ledger_sha256=ledger_sha,
                        cache_index=self.cache.index,
                        manifest_hash=manifest_hash,
                        config_hash=config_view.semantic_hash,
                        logical_clock_ms=int(self.clock.now_ms()),
                        details={},
                    )
                    checkpoint_version = int(checkpoint["version"])
                    checkpoint_path = str(checkpoint_store.directory / f"checkpoint_{checkpoint_version:08d}.json")
                elif graceful_limit is not None or self._graceful_requested:
                    checkpoint = checkpoint_store.commit(
                        run_id=run_id,
                        status="graceful_stop",
                        completed_jobs=sorted(completed),
                        job_revisions=revisions,
                        ledger_bytes=ledger_bytes,
                        ledger_sha256=ledger_sha,
                        cache_index=self.cache.index,
                        manifest_hash=manifest_hash,
                        config_hash=config_view.semantic_hash,
                        logical_clock_ms=int(self.clock.now_ms()),
                        details={"reason": "graceful_signal" if self._graceful_requested else "graceful_job_limit"},
                    )
                    checkpoint_version = int(checkpoint["version"])
                    checkpoint_path = str(checkpoint_store.directory / f"checkpoint_{checkpoint_version:08d}.json")
            finally:
                self._restore_signal_handlers()

            plan["actual_actions"] = actual_actions
            plan["checkpoint_skipped_jobs"] = checkpoint_skipped
            save_state_json(self.root / "plans" / f"{run_id}.json", plan)
            if status == "completed":
                self._save_baseline(jobs, config_view)
            if self._graceful_requested:
                status = "graceful_stop"
            result = RunResult(
                run_id=run_id,
                status=status,
                completed_jobs=sorted(completed),
                checkpoint_skipped_jobs=checkpoint_skipped,
                recomputed=[action for action in actual_actions if action["action"] == "recomputed"],
                reused=[action for action in actual_actions if action["action"] == "reused"],
                plan=plan,
                checkpoint_version=checkpoint_version,
                checkpoint_path=checkpoint_path,
                ledger_path=str(ledger.path),
                degraded=plan.get("degraded", []),
                resume_source=resume_source,
            )
            if status == "completed" and self.run_self_check:
                self.self_check(run_id, plan)
            return result

    def self_check(self, run_id: str, plan: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Cross-check cache node coverage, key restorability, plan partition,
        and final output independently rebuilt from cached products.
        """

        checkpoint_store = CheckpointStore(self.root / "checkpoints" / run_id)
        checkpoint = checkpoint_store.load_latest()
        if checkpoint is None or not verify_checkpoint(checkpoint):
            raise DiagError("E_SELF_CHECK_FAILED", "self-check requires a valid committed checkpoint", {"run_id": run_id})
        ledger_path = self.root / "ledger" / f"{run_id}.jsonl"
        ledger_records = LedgerStore(ledger_path).load_records()
        job_records = {str(record.get("job_id")): record for record in ledger_records}
        expected_jobs = {str(item) for item in checkpoint.get("completed_jobs", [])}
        if set(job_records) != expected_jobs:
            raise DiagError(
                "E_SELF_CHECK_FAILED",
                "ledger job set differs from checkpoint completed job set",
                {"ledger_jobs": sorted(job_records), "checkpoint_jobs": sorted(expected_jobs)},
            )

        node_jobs: Dict[str, set[str]] = {node: set() for node in NODES}
        restorable = 0
        payloads_by_job: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for job_id, record in job_records.items():
            chain = record.get("cache_chain", [])
            if not isinstance(chain, list) or len(chain) != len(NODES):
                raise DiagError("E_SELF_CHECK_FAILED", "ledger cache chain is incomplete", {"job_id": job_id})
            payloads: Dict[str, Dict[str, Any]] = {}
            for item in chain:
                if not isinstance(item, Mapping):
                    raise DiagError("E_SELF_CHECK_FAILED", "ledger cache chain item is malformed", {"job_id": job_id})
                node = str(item.get("node"))
                if node not in NODES:
                    raise DiagError("E_SELF_CHECK_FAILED", "unknown node in ledger cache chain", {"job_id": job_id, "node": node})
                key = str(item.get("cache_key"))
                entry = self.cache.peek(key)
                if entry is None:
                    raise DiagError(
                        "E_SELF_CHECK_FAILED",
                        "cache key cannot restore a product",
                        {"job_id": job_id, "node": node, "cache_key": key},
                    )
                if entry.get("node") != node or entry.get("product_hash") != item.get("product_hash"):
                    raise DiagError(
                        "E_SELF_CHECK_FAILED",
                        "cache entry identity/hash differs from ledger chain",
                        {"job_id": job_id, "node": node},
                    )
                node_jobs[node].add(job_id)
                payloads[node] = entry["payload"]
                restorable += 1
            payloads_by_job[job_id] = payloads

        job_sets = {node: set(values) for node, values in node_jobs.items()}
        if any(job_set != expected_jobs for job_set in job_sets.values()):
            raise DiagError(
                "E_SELF_CHECK_FAILED",
                "cache node job sets are inconsistent",
                {"expected": sorted(expected_jobs), "node_jobs": {k: sorted(v) for k, v in job_sets.items()}},
            )

        if plan is None:
            plan_path = self.root / "plans" / f"{run_id}.json"
            if not plan_path.is_file():
                raise DiagError("E_SELF_CHECK_FAILED", "invalidation plan is missing", {"run_id": run_id})
            plan = read_json(plan_path)
        expected_operations = {
            (str(item["job_id"]), str(item["node"]))
            for item in plan.get("affected_operations", [])
        }
        reuse_operations = {
            (str(item["job_id"]), str(item["node"]))
            for item in plan.get("reuse", [])
        }
        recompute_operations = {
            (str(item["job_id"]), str(item["node"]))
            for item in plan.get("recompute", [])
        }
        if reuse_operations & recompute_operations:
            raise DiagError("E_SELF_CHECK_FAILED", "plan reuse/recompute sets overlap", {})
        if reuse_operations | recompute_operations != expected_operations:
            raise DiagError(
                "E_SELF_CHECK_FAILED",
                "plan reuse/recompute union does not cover affected operations",
                {
                    "missing": sorted(expected_operations - (reuse_operations | recompute_operations)),
                    "extra": sorted((reuse_operations | recompute_operations) - expected_operations),
                },
            )

        final_mismatches: List[Dict[str, Any]] = []
        for job_id, payloads in payloads_by_job.items():
            block_payload = payloads[NODE_BLOCK]
            reduce_payload = payloads[NODE_REDUCE]
            output_payload = payloads[NODE_DEQUANT]
            scale_factor = float(block_payload["scale_factor"])
            rebuilt_reduce: List[int] = []
            rebuilt_float: List[float] = []
            for floats, ints in zip(block_payload["float_partials"], block_payload["fixed_partials_int"]):
                total_float = 0.0
                for value in floats:
                    total_float += float(value)
                total_int = 0
                for value in ints:
                    total_int += int(value)
                rebuilt_float.append(total_float)
                rebuilt_reduce.append(int(total_int))
            if rebuilt_reduce != [int(v) for v in reduce_payload["fixed_accum_int"]]:
                final_mismatches.append({"job_id": job_id, "reason": "fixed accumulation mismatch"})
            if any(not _close_enough(a, b) for a, b in zip(rebuilt_float, reduce_payload["float_reduced"])):
                final_mismatches.append({"job_id": job_id, "reason": "float accumulation mismatch"})
            rebuilt_fixed_output = [int(v) * scale_factor for v in rebuilt_reduce]
            if any(not _close_enough(a, b) for a, b in zip(rebuilt_fixed_output, output_payload["fixed_output"])):
                final_mismatches.append({"job_id": job_id, "reason": "fixed dequant output mismatch"})
            if any(not _close_enough(a, b) for a, b in zip(rebuilt_float, output_payload["float_output"])):
                final_mismatches.append({"job_id": job_id, "reason": "float output mismatch"})
            stage = job_records[job_id].get("stages", {}).get("dequant_output", {})
            if any(not _close_enough(a, b) for a, b in zip(output_payload["float_output"], stage.get("reference", []))):
                final_mismatches.append({"job_id": job_id, "reason": "ledger float stage does not match cache output"})
            if any(not _close_enough(a, b) for a, b in zip(output_payload["fixed_output"], stage.get("candidate", []))):
                final_mismatches.append({"job_id": job_id, "reason": "ledger fixed stage does not match cache output"})
        if final_mismatches:
            raise DiagError("E_SELF_CHECK_FAILED", "cached details cannot independently rebuild final output", {"mismatches": final_mismatches})

        report = {
            "schema": "diagnostic-self-check-report-v1",
            "run_id": run_id,
            "ok": True,
            "job_count": len(expected_jobs),
            "node_job_sets_consistent": True,
            "cache_keys_restorable": restorable,
            "plan_partition_ok": True,
            "final_output_rebuilt": len(expected_jobs),
            "affected_operations": len(expected_operations),
            "reuse_operations": len(reuse_operations),
            "recompute_operations": len(recompute_operations),
        }
        save_state_json(self.root / "reports" / f"selfcheck_{run_id}.json", report)
        return report


def _read_stage_hook(force_kill_after_stages: Optional[int]) -> Optional[Callable[[Dict[str, Any]], None]]:
    if not force_kill_after_stages or force_kill_after_stages <= 0:
        return None
    counter = {"value": 0}

    def hook(action: Dict[str, Any]) -> None:
        counter["value"] += 1
        if counter["value"] >= force_kill_after_stages:
            print(
                canonical_json(
                    {
                        "event": "forced_termination",
                        "stage_count": counter["value"],
                        "job_id": action.get("job_id"),
                        "node": action.get("node"),
                        "checkpoint_commit_skipped": True,
                    }
                ),
                flush=True,
            )
            os._exit(137)

    return hook


def _build_cli_engine(args: argparse.Namespace) -> Engine:
    options = EngineOptions(
        max_resident_bytes=args.max_resident_bytes,
        max_jobs_per_batch=args.max_jobs_per_batch,
        max_cache_entries=args.max_cache_entries,
        max_parallel_recompute=args.max_parallel_recompute,
        default_tolerance=args.tolerance,
    )
    return Engine(
        Path(args.root),
        options=options,
        clock=ManualClock(args.logical_clock_ms),
        base_epoch_ms=args.base_epoch_ms,
        run_self_check=not args.no_self_check,
    )


def _print_json(value: Any, pretty: bool = True) -> None:
    if pretty:
        print(json.dumps(_normalize_json(value), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(canonical_json(value))


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local deterministic operator diagnostics engine")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=".", help="workspace root")
    common.add_argument("--logical-clock-ms", type=int, default=0)
    common.add_argument("--base-epoch-ms", type=int, default=0)
    common.add_argument("--max-resident-bytes", type=int, default=64 * 1024 * 1024)
    common.add_argument("--max-jobs-per-batch", type=int, default=32)
    common.add_argument("--max-cache-entries", type=int, default=1024)
    common.add_argument("--max-parallel-recompute", type=int, default=4)
    common.add_argument("--tolerance", type=float, default=0.5)
    common.add_argument("--no-self-check", action="store_true")
    common.add_argument("--compact", action="store_true")

    run = sub.add_parser("run", parents=[common], help="execute or resume a manifest")
    run.add_argument("--manifest", required=True)
    run.add_argument("--config", default=None)
    run.add_argument("--config-kind", choices=("auto", "v1", "v2"), default="auto")
    run.add_argument("--graceful-after-jobs", type=int, default=None)
    run.add_argument("--force-kill-after-stages", type=int, default=None)

    replay = sub.add_parser("replay", help="independently replay a ledger")
    replay.add_argument("--ledger", required=True)
    replay.add_argument("--compact", action="store_true")

    check = sub.add_parser("selfcheck", parents=[common], help="run cross-module consistency checks")
    check.add_argument("--run-id", required=True)

    migrate = sub.add_parser("migrate", help="migrate v1<->v2 config snapshots")
    migrate.add_argument("--direction", choices=("v1-to-v2", "v2-to-v1"), required=True)
    migrate.add_argument("--source", required=True)
    migrate.add_argument("--destination", required=True)
    migrate.add_argument("--base-epoch-ms", type=int, default=0)
    migrate.add_argument("--report", default=None)
    migrate.add_argument("--lock-path", default=None)
    migrate.add_argument("--compact", action="store_true")

    bump = sub.add_parser("bump-generation", help="atomically raise a v2 config generation")
    bump.add_argument("--v2-dir", required=True)
    bump.add_argument("--config-key", required=True)
    bump.add_argument("--new-generation", type=int, default=None)
    bump.add_argument("--lock-path", default=None)
    bump.add_argument("--compact", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            engine = _build_cli_engine(args)
            hook = _read_stage_hook(args.force_kill_after_stages)
            result = engine.run(
                Path(args.manifest),
                Path(args.config) if args.config else None,
                config_kind=args.config_kind,
                stage_hook=hook,
                graceful_after_jobs=args.graceful_after_jobs,
            )
            _print_json(result.to_dict(), pretty=not args.compact)
            return 0
        if args.command == "replay":
            report = replay_ledger(Path(args.ledger))
            _print_json(report, pretty=not args.compact)
            return 0 if report["ok"] else 2
        if args.command == "selfcheck":
            engine = _build_cli_engine(args)
            report = engine.self_check(args.run_id)
            _print_json(report, pretty=not args.compact)
            return 0
        if args.command == "migrate":
            source = Path(args.source)
            destination = Path(args.destination)
            report_path = Path(args.report) if args.report else None
            lock_path = Path(args.lock_path) if args.lock_path else None

            def migrate_action() -> Dict[str, Any]:
                if args.direction == "v1-to-v2":
                    return migrate_v1_to_v2(source, destination, args.base_epoch_ms, report_path)
                return migrate_v2_to_v1(source, destination, args.base_epoch_ms, report_path)

            if lock_path is None:
                report = migrate_action()
            else:
                with StageLock(lock_path):
                    report = migrate_action()
            _print_json(report, pretty=not args.compact)
            return 0
        if args.command == "bump-generation":
            record = update_v2_generation(
                Path(args.v2_dir),
                args.config_key,
                new_generation=args.new_generation,
                lock_path=Path(args.lock_path) if args.lock_path else None,
            )
            _print_json(record.semantic_dict(), pretty=not args.compact)
            return 0
        parser.error("unknown command")
        return 2
    except DiagError as exc:
        _print_json({"error": exc.as_dict()})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())





