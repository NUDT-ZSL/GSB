from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any


class StrictJSONError(ValueError):
    pass


def _parse_constant(token: str) -> float:
    if token == "NaN":
        return math.nan
    if token == "Infinity":
        return math.inf
    if token == "-Infinity":
        return -math.inf
    raise StrictJSONError(f"unsupported JSON constant {token!r}")


def strict_json_loads(text: str) -> Any:
    try:
        return json.loads(text, parse_constant=_parse_constant)
    except StrictJSONError:
        raise
    except json.JSONDecodeError as exc:
        raise StrictJSONError(f"invalid JSON: {exc}") from exc


def canonical_dumps(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".tmp-{target.name}-{os.getpid()}")
    try:
        with open(temp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        _fsync_dir(target.parent)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def float_hex(value: float) -> str:
    return float(value).hex()


def normalize_float_cell(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if isinstance(value, str) and value in {"NaN", "Infinity", "-Infinity"}:
            if value == "NaN":
                return math.nan
            if value == "Infinity":
                return math.inf
            return -math.inf
        raise TypeError("float cell must be a JSON number or a canonical non-finite token")
    return float(value)


def json_float_cell(value: float) -> float | str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if value == 0.0 and math.copysign(1.0, value) < 0:
        return -0.0
    return value
