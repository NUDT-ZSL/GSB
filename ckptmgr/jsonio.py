"""Canonical JSON and crash-safe file helpers."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Union

from .errors import PlanFieldError

PathLike = Union[str, os.PathLike]


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_text(value: Any) -> str:
    return canonical_bytes(value).decode("utf-8")


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_bytes(value))


def digest_file(path: PathLike) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def read_json(path: PathLike) -> Any:
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PlanFieldError("file", "JSON file does not exist", path=str(target)) from exc
    except OSError as exc:
        raise PlanFieldError("file", "JSON file cannot be read", path=str(target)) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PlanFieldError(
            "file",
            "file is not valid JSON",
            path=str(target),
            line=exc.lineno,
            column=exc.colno,
        ) from exc


def ensure_parent(path: PathLike) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def fsync_dir(path: PathLike) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: PathLike, data: bytes, mode: int = 0o644) -> None:
    """Write and fsync a sibling temporary file, then rename it atomically."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="." + target.name + ".tmp.", dir=str(target.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, target)
        fsync_dir(target.parent)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: PathLike, value: Any) -> None:
    atomic_write_bytes(path, canonical_bytes(value) + b"\n")


def load_optional_json(path: Union[PathLike, None], section: str) -> Dict[str, Any]:
    if path is None:
        return {}
    value = read_json(path)
    if not isinstance(value, dict):
        raise PlanFieldError(section, "section must be a JSON object")
    if section in value and isinstance(value[section], dict):
        return dict(value[section])
    return value
