from __future__ import annotations

import hashlib
import json
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

from .errors import InputError


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def load_json(path: str | Path) -> Any:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(
            "E_JSON",
            f"cannot read JSON file: {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except (ValueError, TypeError) as exc:
        raise InputError(
            "E_JSON",
            f"invalid JSON file: {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc


def _canonicalize(value: Any) -> Any:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal is not serializable")
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        if text in ("", "-0"):
            return "0"
        return text
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    return value


def canonical_json_bytes(value: Any) -> bytes:
    normalized = _canonicalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_value(value: Any) -> str:
    return digest_bytes(canonical_json_bytes(value))


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")


def read_json_checked(path: str | Path) -> Any:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputError(
            "E_JSON",
            f"cannot read JSON file: {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    try:
        return json.loads(
            raw.decode("utf-8"),
            parse_float=float,
            parse_int=int,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise InputError(
            "E_JSON",
            f"invalid JSON file: {path}",
            {"path": str(path), "reason": str(exc)},
        ) from exc

