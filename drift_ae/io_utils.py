"""Deterministic serialization, checksums and atomic filesystem helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import numpy as np


def canonical_json_bytes(payload: Any) -> bytes:
    """Return stable JSON bytes.

    Floats are encoded by Python's ``repr`` via json.  The same logical object
    therefore always gets the same checksum in one runtime/version.
    """

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def payload_checksum(payload: Any) -> str:
    return sha256_hex(canonical_json_bytes(payload))


def array_fingerprint(array: np.ndarray) -> str:
    """Fingerprint dtype, shape and raw little-endian payload."""

    arr = np.ascontiguousarray(array)
    header = f"{arr.dtype.str}|{arr.shape}|".encode("utf-8")
    return sha256_hex(header + arr.tobytes(order="C"))


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """Write bytes via a same-directory temp file and atomic rename."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{target.name}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
    tmp_path = target.parent / tmp_name
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        _fsync_directory(target.parent)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
    return target


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    return atomic_write_bytes(path, canonical_json_bytes(payload) + b"\n")


def read_json(path: str | Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def append_jsonl(path: str | Path, record: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json_bytes(record) + b"\n"
    with open(target, "ab") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())
    return target


def read_jsonl(path: str | Path) -> list[Any]:
    target = Path(path)
    if not target.exists():
        return []
    records = []
    with open(target, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        fd = os.open(str(directory), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Some virtual/network filesystems do not support directory fsync.
        pass
    finally:
        os.close(fd)
