"""Canonical hashing, path handling, and crash-safe file writes."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import BusinessError, validation_error


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise validation_error(
            "E_POINTCLOUD_IO",
            "cannot read file for digest",
            path=str(path),
            os_error=str(exc),
        ) from exc
    return digest.hexdigest()


def read_json(path: Path, code: str = "E_JSON") -> Any:
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise validation_error(code, "cannot read JSON file", path=str(path), os_error=str(exc)) from exc
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise validation_error(code, "invalid JSON", path=str(path), os_error=str(exc)) from exc


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


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, value: Any) -> None:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ).encode("utf-8") + b"\n"
    atomic_write_bytes(path, payload)


def atomic_savez(path: Path, arrays: dict[str, Any]) -> None:
    """Write a NumPy archive via a same-directory temporary file."""

    import numpy as np

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def ensure_writable_directory(path: Path, code: str = "E_STATE") -> None:
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=".write-probe-", dir=str(path))
        os.close(fd)
        Path(temp_name).unlink(missing_ok=True)
    except OSError as exc:
        raise BusinessError(
            code=code,
            message="directory is not writable",
            details={"path": str(path), "os_error": str(exc)},
        ) from exc


def safe_component(value: str, code: str, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise validation_error(code, f"{field} must be a non-empty string", field=field)
    if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in value):
        raise validation_error(
            code,
            f"{field} contains unsupported path characters",
            field=field,
            value=value,
        )
    return value


def finite_number(value: Any, code: str, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise validation_error(code, f"{field} must be a finite number", field=field, value=value)
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise validation_error(
            code,
            f"{field} is outside the supported finite numeric range",
            field=field,
            value=repr(value),
        ) from exc
    if not math.isfinite(number):
        raise validation_error(code, f"{field} must be finite", field=field, value=value)
    return number


def finite_positive(value: Any, code: str, field: str) -> float:
    number = finite_number(value, code, field)
    if number <= 0:
        raise validation_error(code, f"{field} must be positive", field=field, value=value)
    return number


def positive_int(value: Any, code: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise validation_error(code, f"{field} must be a positive integer", field=field, value=value)
    if value <= 0:
        raise validation_error(code, f"{field} must be positive", field=field, value=value)
    return value


def resolve_local_path(base: Path, value: Any, code: str, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise validation_error(code, f"{field} must be a non-empty path string", field=field)
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()

