from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .errors import fail


class DuplicateKeyError(ValueError):
    def __init__(self, key: str) -> None:
        super().__init__(f"duplicate object key: {key}")
        self.key = key


def _no_duplicates(pairs: Iterable[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError(key)
        result[key] = value
    return result


def load_json_strict(path: str | Path, *, code: str) -> Any:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        fail(code, f"cannot read JSON file {path}: {exc}")
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates)
    except DuplicateKeyError as exc:
        fail(code, f"{path}: {exc}")
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        fail(code, f"{path} is not valid JSON: {exc}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=True,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_bytes_atomic(path: str | Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def write_json_atomic(path: str | Path, value: Any) -> None:
    write_bytes_atomic(path, canonical_bytes(value))


def json_key(value: Any) -> str:
    return canonical_json(value)


def safe_float(value: Any, *, code: str, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        fail(code, f"{what} must be numeric")
    number = float(value)
    if math.isinf(number):
        fail(code, f"{what} must not be infinite")
    return number

