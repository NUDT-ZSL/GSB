"""Small deterministic utilities shared by the pipeline."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

from .errors import PipelineError


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                block = handle.read(chunk_size)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise PipelineError("E_IO", f"cannot read {path}: {exc}") from exc
    return digest.hexdigest()


def load_json(path: Path, *, input_id: str | None = None,
              code: str = "E_CONFIG") -> Any:
    def reject_constant(value: str) -> Any:
        raise ValueError(f"non-finite JSON constant {value!r} is not allowed")

    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle, parse_constant=reject_constant)
    except FileNotFoundError as exc:
        raise PipelineError(code, f"missing JSON file: {path}", input_id=input_id) from exc
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise PipelineError(code, f"cannot parse JSON file {path}: {exc}",
                            input_id=input_id) from exc


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise PipelineError("E_IO", f"cannot write {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value) + b"\n")


def finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def as_int(value: Any, name: str, *, minimum: int | None = None,
           maximum: int | None = None, code: str = "E_CONFIG") -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PipelineError(code, f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise PipelineError(code, f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise PipelineError(code, f"{name} must be <= {maximum}")
    return value


def as_number(value: Any, name: str, *, minimum: float | None = None,
              maximum: float | None = None,
              exclusive_minimum: bool = False,
              exclusive_maximum: bool = False,
              code: str = "E_CONFIG") -> float:
    if not finite_number(value):
        raise PipelineError(code, f"{name} must be a finite number")
    number = float(value)
    if minimum is not None:
        if exclusive_minimum and number <= minimum:
            raise PipelineError(code, f"{name} must be > {minimum}")
        if not exclusive_minimum and number < minimum:
            raise PipelineError(code, f"{name} must be >= {minimum}")
    if maximum is not None:
        if exclusive_maximum and number >= maximum:
            raise PipelineError(code, f"{name} must be < {maximum}")
        if not exclusive_maximum and number > maximum:
            raise PipelineError(code, f"{name} must be <= {maximum}")
    return number


def safe_resolve(base: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def ensure_inside(root: Path, path: Path) -> Path:
    root = root.resolve()
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PipelineError("E_IO", f"path escapes workspace: {path}") from exc
    return path


def rounded(value: float | None, digits: int = 6) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return round(value, digits)
