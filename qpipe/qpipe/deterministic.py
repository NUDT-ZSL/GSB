"""Determinism primitives: byte serialisation, checksums, timers, budgets.

Nothing in this module consults the wall clock for *values*; timing is only
captured as an explicit observation attached to run records.  All hashing goes
through canonical byte encodings so that identical logical content always maps
to identical bytes regardless of process, platform dict ordering or time.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Tuple

import numpy as np

#: Integer widths (bits) used for memory estimation of de-quantised weights.
FLOAT32_BYTES = 4

#: Quantised integer storage width (bytes) keyed by dtype name.
QUANT_STORAGE_BYTES = {
    "int8": 1,
    "uint8": 1,
    "int16": 2,
    "uint16": 2,
    "int32": 4,
}

#: Representable integer ranges per supported quant dtype.
QUANT_RANGES: Dict[str, Tuple[int, int]] = {
    "int8": (-128, 127),
    "uint8": (0, 255),
    "int16": (-32768, 32767),
    "uint16": (0, 65535),
    "int32": (-2147483648, 2147483647),
}


def canonical_json_bytes(obj: Any) -> bytes:
    """Serialise ``obj`` to canonical JSON bytes.

    ``sort_keys=True`` removes any dependence on dictionary traversal order and
    separators are compacted so the output is stable across environments.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def array_bytes(arr: np.ndarray) -> bytes:
    """Return the raw little-endian byte image of a NumPy array.

    Arrays are coerced to little-endian, contiguous form first; therefore two
    arrays with equal logical values always produce equal bytes even if their
    in-memory strides or endianness differed.
    """
    contiguous = np.ascontiguousarray(arr)
    if contiguous.dtype.byteorder == ">":
        contiguous = contiguous.byteswap().newbyteorder("<")
    header = canonical_json_bytes(
        {"shape": list(contiguous.shape), "dtype": str(contiguous.dtype)}
    )
    return header + b"|" + contiguous.tobytes(order="C")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checksum_array(arr: np.ndarray) -> str:
    """Stable SHA-256 checksum over an array's shape, dtype and values."""
    return sha256_hex(array_bytes(arr))


def checksum_json(obj: Any) -> str:
    return sha256_hex(canonical_json_bytes(obj))


def checksum_arrays(arrs: Iterable[np.ndarray]) -> str:
    """Order-sensitive checksum over a sequence of arrays."""
    digest = hashlib.sha256()
    for arr in arrs:
        digest.update(array_bytes(arr))
    return digest.hexdigest()


def layer_numel(shape: Iterable[int]) -> int:
    numel = 1
    for dim in shape:
        numel *= int(dim)
    return numel


def estimate_quant_bytes(shape: Iterable[int], quant_dtype: str) -> int:
    """Estimated bytes for the stored quantised integer weights of one layer."""
    if quant_dtype not in QUANT_STORAGE_BYTES:
        raise KeyError(quant_dtype)
    return layer_numel(shape) * QUANT_STORAGE_BYTES[quant_dtype]


def estimate_dequant_bytes(shape: Iterable[int]) -> int:
    """Estimated bytes for the float32 de-quantised weights of one layer."""
    return layer_numel(shape) * FLOAT32_BYTES


@dataclass
class Timer:
    """Lightweight timing observation using a monotonic clock.

    The clock never feeds any computed tensor or checksum; it only records an
    elapsed observation for the run summary.
    """

    label: str
    _start: float = 0.0
    elapsed_ms: float = 0.0

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000.0

    def to_dict(self) -> Dict[str, float]:
        return {"label": self.label, "elapsed_ms": round(self.elapsed_ms, 6)}
