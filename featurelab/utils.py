"""Small deterministic and I/O utilities shared by the application."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Sequence

import numpy as np

from .errors import config_error


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def text_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_yaml(path: Path) -> Any:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise config_error("PyYAML is required") from exc
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise config_error("configuration file not found", path=str(path)) from exc
    except Exception as exc:
        raise config_error(
            "configuration YAML could not be parsed",
            path=str(path),
            error=str(exc),
        ) from exc


def deep_get(value: Mapping[str, Any], dotted: str, default: Any = None) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return default
        current = current[part]
    return current


def deep_set(value: MutableMapping[str, Any], dotted: str, item: Any) -> None:
    parts = dotted.split(".")
    current: MutableMapping[str, Any] = value
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, MutableMapping):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = item


def require_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise config_error(f"{name} must be a mapping")
    return value


def require_list(value: Any, name: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise config_error(f"{name} must be a list")
    return value


def require_str(value: Any, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise config_error(f"{name} must be a non-empty string")
    return value


def require_int(value: Any, name: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise config_error(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise config_error(f"{name} must be >= {minimum}")
    return value


def require_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise config_error(f"{name} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise config_error(f"{name} must be finite")
    return number


def require_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise config_error(f"{name} must be a boolean")
    return value


def matrix_fingerprint(matrix: np.ndarray) -> str:
    array = np.ascontiguousarray(matrix, dtype=np.float64)
    digest = hashlib.sha256()
    digest.update(str(tuple(array.shape)).encode("ascii"))
    digest.update(b"\0")
    digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def vector_fingerprint(values: Iterable[Any]) -> str:
    return fingerprint(list(values))


def fsum(values: Iterable[float]) -> float:
    return math.fsum(float(value) for value in values)


def normalized_mean(values: Iterable[float]) -> float:
    items = list(values)
    if not items:
        return 0.0
    return fsum(items) / len(items)


def parse_cli_value(raw: str) -> Any:
    """Parse the deliberately small value grammar used by interactive commands."""

    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "none"}:
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw
