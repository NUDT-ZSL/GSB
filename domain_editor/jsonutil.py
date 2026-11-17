"""Canonical JSON and crash-safe file replacement helpers."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any


def decimal_text(value: Decimal | int | str) -> str:
    """Return a deterministic, exponent-free decimal string."""

    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if not dec.is_finite():
        raise ValueError("non-finite decimal is not supported")
    if dec == 0:
        return "0"
    text = format(dec, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _json_number(value: Decimal) -> str:
    text = decimal_text(value)
    if "." not in text:
        text += ".0"
    if text == "-0.0":
        text = "0.0"
    return text


def dumps_canonical(obj: Any, *, newline: bool = False) -> str:
    """Encode JSON with sorted keys, stable decimal spelling, and no whitespace."""

    def encode(value: Any) -> str:
        if value is None:
            return "null"
        if value is True:
            return "true"
        if value is False:
            return "false"
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, Decimal):
            return _json_number(value)
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise ValueError("non-finite float is not supported")
            return _json_number(Decimal(str(value)))
        if isinstance(value, (list, tuple)):
            return "[" + ",".join(encode(item) for item in value) + "]"
        if isinstance(value, dict):
            pieces: list[str] = []
            for key in sorted(value):
                if not isinstance(key, str):
                    raise TypeError("JSON object keys must be strings")
                pieces.append(json.dumps(key, ensure_ascii=False) + ":" + encode(value[key]))
            return "{" + ",".join(pieces) + "}"
        raise TypeError(f"unsupported JSON value: {type(value).__name__}")

    text = encode(obj)
    return text + ("\n" if newline else "")


def loads_decimal(text: str | bytes) -> Any:
    return json.loads(text, parse_float=Decimal)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def stable_hash(obj: Any) -> str:
    return sha256_text(dumps_canonical(obj))


def ensure_directory(path: str | os.PathLike[str]) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    """Write bytes to a sibling temp file, fsync, then atomically replace."""

    target = Path(path)
    ensure_directory(target.parent)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.tmp-", dir=target.parent)
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


def atomic_write_text(path: str | os.PathLike[str], text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str | os.PathLike[str], obj: Any) -> None:
    atomic_write_text(path, dumps_canonical(obj, newline=True))


def read_json(path: str | os.PathLike[str]) -> Any:
    return loads_decimal(Path(path).read_text(encoding="utf-8"))
