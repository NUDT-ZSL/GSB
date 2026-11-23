"""Canonical hashing and deterministic artifact serialization."""

from __future__ import annotations

import hashlib
import io
import json
import os
import struct
import tempfile
from pathlib import Path
from typing import Any, Mapping

import numpy as np


_ARTIFACT_MAGIC = b"ROPE-ARTIFACT-1\n"


def jsonable(value: Any) -> Any:
    """Convert common scientific values to stable JSON-compatible objects."""

    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, float):
        if not np.isfinite(value):
            raise ValueError(f"non-finite float cannot be canonicalized: {value!r}")
        return value
    return value


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _array_digest(array: np.ndarray) -> bytes:
    array = np.ascontiguousarray(array)
    header = canonical_json_bytes(
        {
            "dtype": array.dtype.str,
            "shape": list(array.shape),
            "fortran_order": False,
        }
    )
    return hashlib.sha256(header + b"\0" + array.tobytes(order="C")).digest()


def hash_arrays(arrays: Mapping[str, np.ndarray]) -> str:
    """Hash a named array bundle independently of its on-disk representation."""

    digest = hashlib.sha256()
    for name in sorted(arrays):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_array_digest(np.asarray(arrays[name])))
    return digest.hexdigest()


def encode_artifact(meta: Mapping[str, Any], arrays: Mapping[str, np.ndarray]) -> bytes:
    """Encode arrays deterministically into one atomically writable blob."""

    payloads: list[bytes] = []
    index: dict[str, dict[str, Any]] = {}
    offset = 0
    for name in sorted(arrays):
        array = np.ascontiguousarray(arrays[name])
        buffer = io.BytesIO()
        np.save(buffer, array, allow_pickle=False)
        payload = buffer.getvalue()
        index[name] = {
            "dtype": array.dtype.str,
            "shape": list(array.shape),
            "offset": offset,
            "length": len(payload),
        }
        payloads.append(payload)
        offset += len(payload)

    header = canonical_json_bytes({"meta": meta, "arrays": index})
    return b"".join(
        [
            _ARTIFACT_MAGIC,
            struct.pack("<Q", len(header)),
            header,
            *payloads,
        ]
    )


def decode_artifact(blob: bytes) -> tuple[dict[str, Any], dict[str, np.ndarray]]:
    if not blob.startswith(_ARTIFACT_MAGIC):
        raise ValueError("invalid artifact magic")
    start = len(_ARTIFACT_MAGIC)
    if len(blob) < start + 8:
        raise ValueError("truncated artifact header")
    (header_length,) = struct.unpack("<Q", blob[start : start + 8])
    header_start = start + 8
    header_end = header_start + header_length
    if header_end > len(blob):
        raise ValueError("truncated artifact metadata")
    header = json.loads(blob[header_start:header_end].decode("utf-8"))
    data_start = header_end
    arrays: dict[str, np.ndarray] = {}
    for name, item in header["arrays"].items():
        begin = data_start + int(item["offset"])
        end = begin + int(item["length"])
        if end > len(blob):
            raise ValueError(f"truncated array payload: {name}")
        array = np.load(io.BytesIO(blob[begin:end]), allow_pickle=False)
        expected_shape = tuple(int(value) for value in item["shape"])
        if tuple(array.shape) != expected_shape:
            raise ValueError(f"shape mismatch for array: {name}")
        arrays[name] = array
    return header["meta"], arrays


def atomic_write_bytes(path: Path, blob: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: Path, value: Any) -> None:
    # A trailing newline makes human inspection of manifests and reports pleasant.
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(descriptor)
        except OSError:
            pass
    finally:
        os.close(descriptor)
