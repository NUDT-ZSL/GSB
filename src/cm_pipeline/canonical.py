from __future__ import annotations

import hashlib
import json
import math
import struct
from typing import Any

import numpy as np

from .errors import PipelineError

PAYLOAD_MAGIC = b"CMPAY1\x00"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def pretty_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _canonical_dtype(dtype: np.dtype) -> str:
    dtype = np.dtype(dtype)
    if dtype == np.dtype("float64"):
        return "<f8"
    if dtype == np.dtype("float32"):
        return "<f4"
    if dtype == np.dtype("int64"):
        return "<i8"
    if dtype == np.dtype("int32"):
        return "<i4"
    if dtype == np.dtype("uint8"):
        return "|u1"
    raise PipelineError(
        "E_CONFIG_INVALID",
        f"Unsupported array dtype for deterministic payload: {dtype}",
        stage="canonicalization",
        field="dtype",
    )


def canonical_array(array: np.ndarray) -> np.ndarray:
    dtype = _canonical_dtype(array.dtype)
    return np.ascontiguousarray(array, dtype=np.dtype(dtype))


def array_digest(array: np.ndarray) -> str:
    canonical = canonical_array(array)
    header = canonical_json_bytes(
        {
            "dtype": canonical.dtype.str,
            "shape": list(canonical.shape),
        }
    )
    return sha256_bytes(
        b"CMP-ARRAY-v1\x00"
        + struct.pack(">Q", len(header))
        + header
        + canonical.tobytes(order="C")
    )


def pack_payload(arrays: dict[str, np.ndarray], meta: dict[str, Any] | None = None) -> bytes:
    """Pack arrays without ZIP metadata, preserving deterministic byte order."""
    meta = meta or {}
    names = sorted(arrays)
    descriptors: list[dict[str, Any]] = []
    chunks: list[bytes] = []
    for name in names:
        if not name or not isinstance(name, str):
            raise PipelineError(
                "E_INCONSISTENT",
                "Array names must be non-empty strings",
                stage="payload",
                field="arrays",
            )
        array = canonical_array(arrays[name])
        descriptors.append(
            {
                "name": name,
                "dtype": array.dtype.str,
                "shape": list(array.shape),
                "nbytes": int(array.nbytes),
            }
        )
        chunks.append(array.tobytes(order="C"))
    header = canonical_json_bytes(
        {
            "schema": "cm-payload-v1",
            "arrays": descriptors,
            "meta": meta,
        }
    )
    return (
        PAYLOAD_MAGIC
        + struct.pack(">Q", len(header))
        + header
        + b"".join(chunks)
    )


def unpack_payload(payload: bytes) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    if not payload.startswith(PAYLOAD_MAGIC):
        raise PipelineError(
            "E_INCONSISTENT",
            "Invalid payload magic",
            stage="cache",
            field="payload",
        )
    offset = len(PAYLOAD_MAGIC)
    if len(payload) < offset + 8:
        raise PipelineError(
            "E_INCONSISTENT",
            "Truncated payload header",
            stage="cache",
            field="payload",
        )
    header_len = struct.unpack(">Q", payload[offset : offset + 8])[0]
    offset += 8
    header_end = offset + header_len
    if header_end > len(payload):
        raise PipelineError(
            "E_INCONSISTENT",
            "Payload header length exceeds payload size",
            stage="cache",
            field="payload",
        )
    try:
        header = json.loads(payload[offset:header_end].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PipelineError(
            "E_INCONSISTENT",
            f"Malformed payload header: {exc}",
            stage="cache",
            field="payload",
        ) from exc
    offset = header_end
    arrays: dict[str, np.ndarray] = {}
    for descriptor in header.get("arrays", []):
        name = descriptor["name"]
        dtype = np.dtype(descriptor["dtype"])
        shape = tuple(int(x) for x in descriptor["shape"])
        nbytes = int(descriptor["nbytes"])
        end = offset + nbytes
        if end > len(payload):
            raise PipelineError(
                "E_INCONSISTENT",
                f"Truncated array payload for {name}",
                stage="cache",
                field=name,
            )
        arrays[name] = np.frombuffer(payload[offset:end], dtype=dtype).reshape(shape).copy()
        offset = end
    if offset != len(payload):
        raise PipelineError(
            "E_INCONSISTENT",
            "Payload contains trailing bytes",
            stage="cache",
            field="payload",
        )
    return arrays, header.get("meta", {})


def fsum(values: list[float]) -> float:
    """Stable sum helper for report-level comparisons."""
    return math.fsum(values)

