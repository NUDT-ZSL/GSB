"""Deterministic primitives: RNG construction, hashing, canonical JSON, timing.

No function here reads the wall clock except :func:`perf_counter_seconds`, which
is used purely to *observe* latency for budget reporting. Timing values never
participate in recognition, ordering, hashing or cache decisions.
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from typing import Any, Iterable

import numpy as np


def rng_from_seed(seed: int, *salt: int) -> np.random.Generator:
    """Build a reproducible NumPy PCG64 generator from a seed and integer salt."""
    digest = hashlib.sha256()
    digest.update(int(seed).to_bytes(8, "big", signed=False))
    for item in salt:
        digest.update(int(item).to_bytes(8, "big", signed=False))
    seed_sequence = np.random.SeedSequence(int.from_bytes(digest.digest()[:16], "big"))
    return np.random.default_rng(seed_sequence)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_bytes(obj: Any) -> bytes:
    """Serialize to stable, sort-keyed JSON bytes (``ensure_ascii=False``)."""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def content_hash(obj: Any) -> str:
    """Content-addressed SHA-256 of a JSON-serializable object."""
    return sha256_hex(canonical_bytes(obj))


def write_json(path: str, obj: Any, *, indent: int = 2) -> None:
    # sort_keys keeps every persisted artifact byte-stable across runs.
    text = json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=indent)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.write("\n")


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_jsonl(path: str, rows: Iterable[Any]) -> None:
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
                + "\n"
            )


def read_jsonl(path: str) -> list[Any]:
    rows: list[Any] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                rows.append(json.loads(raw))
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL on line {line_no}: {exc}") from exc
    return rows


def array_hash(image: np.ndarray) -> str:
    """Hash a numeric array via its dtype, shape and little-endian bytes."""
    arr = np.ascontiguousarray(image)
    buffer = io.BytesIO()
    np.save(buffer, arr, allow_pickle=False)
    return sha256_hex(buffer.getvalue())


def round6(value: float) -> float:
    """Deterministic quantization used for every reported float."""
    return round(float(value), 6)


def perf_counter_seconds() -> float:
    return time.perf_counter()
