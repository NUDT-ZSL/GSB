from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import numpy as np


BASE_LOGICAL_MS = 1_767_225_600_000


class LogicalClock:
    """Deterministic millisecond clock used by snapshots and ledgers."""

    def __init__(self, start_ms: int = BASE_LOGICAL_MS, step_ms: int = 1_000) -> None:
        self.value_ms = int(start_ms)
        self.step_ms = int(step_ms)

    def tick(self) -> int:
        current = self.value_ms
        self.value_ms += self.step_ms
        return current

    def now(self) -> int:
        return self.value_ms


def canonical_dumps(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_hash(digest: str, length: int = 12) -> str:
    return digest[:length]


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_text(path, canonical_dumps(value) + "\n")


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def append_jsonl(path: str | Path, records: list[dict[str, Any]]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(canonical_dumps(record) + "\n")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL line {line_number} must be an object")
            records.append(value)
    return records


def array_digest(array: np.ndarray) -> dict[str, Any]:
    arr = np.asarray(array)
    contiguous = np.ascontiguousarray(arr)
    digest = hashlib.sha256()
    digest.update(str(arr.dtype).encode("ascii"))
    digest.update(b"|")
    digest.update(json.dumps(list(arr.shape), separators=(",", ":")).encode("ascii"))
    digest.update(b"|")
    digest.update(contiguous.tobytes(order="C"))
    return {
        "sha256": digest.hexdigest(),
        "shape": [int(dim) for dim in arr.shape],
        "dtype": str(arr.dtype),
    }


def write_npy_atomic(path: str | Path, array: np.ndarray) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".npy", dir=target.parent)
    os.close(fd)
    try:
        np.save(tmp_name, np.asarray(array), allow_pickle=False)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
