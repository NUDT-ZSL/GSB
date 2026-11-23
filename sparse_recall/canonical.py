"""Canonical bytes, stable floating-point values, and atomic file writes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import E_INPUT, RecallError


DEFAULT_DECIMALS = 12
DEFAULT_SUBNORMAL_FLOOR = 1.0e-300


def stable_float(
    value: float,
    decimals: int = DEFAULT_DECIMALS,
    subnormal_floor: float = DEFAULT_SUBNORMAL_FLOOR,
) -> float:
    """Normalize finite floats and collapse subnormal noise to positive zero."""

    value = float(value)
    if not math.isfinite(value):
        raise RecallError(E_INPUT, "numeric", f"non-finite numeric value: {value!r}")
    if abs(value) < subnormal_floor:
        return 0.0
    rounded = round(value, decimals)
    if rounded == 0:
        return 0.0
    return rounded


def canonical_json_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise RecallError(E_INPUT, "serialization", f"cannot canonicalize value: {exc}") from exc
    return text.encode("utf-8")


def canonical_json_line(value: Any) -> bytes:
    return canonical_json_bytes(value) + b"\n"


def canonical_json_text(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_bytes_atomic(path: str | os.PathLike[str], data: bytes) -> str:
    """Write bytes through a same-directory temp file and atomic replacement."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        _fsync_directory(target.parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise
    return sha256_bytes(data)


def write_json_atomic(path: str | os.PathLike[str], value: Any) -> str:
    data = canonical_json_bytes(value) + b"\n"
    return write_bytes_atomic(path, data)


def write_jsonl_atomic(path: str | os.PathLike[str], rows: list[Any]) -> str:
    data = b"".join(canonical_json_line(row) for row in rows)
    return write_bytes_atomic(path, data)


def read_json(path: str | os.PathLike[str]) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise
    except (OSError, json.JSONDecodeError) as exc:
        raise RecallError(E_INPUT, "read_json", f"cannot read JSON {path}: {exc}") from exc


def read_jsonl(path: str | os.PathLike[str]) -> list[Any]:
    rows: list[Any] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    rows.append(json.loads(text))
                except json.JSONDecodeError as exc:
                    raise RecallError(
                        E_INPUT,
                        "read_jsonl",
                        f"invalid JSON at {path}:{line_no}: {exc}",
                        details={"line": line_no},
                    ) from exc
    except FileNotFoundError:
        raise
    return rows


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
