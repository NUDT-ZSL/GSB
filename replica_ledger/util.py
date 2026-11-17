"""Shared deterministic and durability helpers."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class DuplicateJsonKey(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json_strict(path: str | Path) -> Any:
    raw = Path(path).read_bytes()
    text = raw.decode("utf-8")
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


def canonical_json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_json_bytes(value: Any) -> bytes:
    return canonical_json_text(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_json(value: Any) -> str:
    return sha256_hex(canonical_json_bytes(value))


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_nonnegative_int(value: Any) -> bool:
    return is_int(value) and value >= 0


def is_positive_int(value: Any) -> bool:
    return is_int(value) and value > 0


def ceil_div(numerator: int, denominator: int) -> int:
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    return -((-numerator) // denominator)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def fsync_directory(path: Path) -> None:
    """Best-effort directory fsync; Windows does not allow opening directories."""

    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, data: bytes, mode: int = 0o644) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target.name}.tmp-", dir=str(target.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, target)
        fsync_directory(target.parent)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any, *, indent: int | None = 2) -> None:
    if indent is None:
        text = canonical_json_text(value)
    else:
        text = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=indent,
            allow_nan=False,
        )
    atomic_write_bytes(path, (text + "\n").encode("utf-8"))


def set_read_only(path: str | Path) -> None:
    target = Path(path)
    mode = stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH
    os.chmod(target, mode)
    try:
        os.chmod(target, mode)
    except OSError:
        pass


class JsonlEventLog:
    """Thread-safe JSON-lines audit log with the mandatory diagnostic keys."""

    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(
        self,
        event: str,
        *,
        level: str = "INFO",
        code: str = "",
        message: str = "",
        field_path: str = "-",
        node_id: str = "-",
        block_id: str = "-",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record = {
            "timestamp_utc": utc_now_iso(),
            "level": level,
            "event": event,
            "code": code,
            "message": message,
            "field_path": field_path if field_path else "-",
            "node_id": node_id if node_id else "-",
            "block_id": block_id if block_id else "-",
            "context": context or {},
        }
        if self.path is not None:
            line = canonical_json_text(record) + "\n"
            with self._lock:
                with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                    stream.write(line)
                    stream.flush()
                    os.fsync(stream.fileno())
        return record

    def error(self, error: LedgerDomainError, *, event: str = "BUSINESS_ERROR") -> dict[str, Any]:
        return self.emit(
            event,
            level="ERROR",
            code=error.code,
            message=error.message,
            field_path=error.field_path,
            node_id=error.node_id,
            block_id=error.block_id,
            context=error.details,
        )


def stable_sorted(values: Iterable[Any], key: Any) -> list[Any]:
    """A named helper to make determinism intentional at call sites."""

    return sorted(values, key=key)


def safe_id_component(value: str) -> str:
    """Encode arbitrary identifiers into a Windows-safe path component."""

    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]


def ratio_ppm(numerator: int, denominator: int) -> int:
    if denominator == 0:
        return 0
    return ceil_div(numerator * 1_000_000, denominator)


def clamp_nonnegative(value: int) -> int:
    return max(0, value)


def json_ready(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, tuple):
        return [json_ready(item) for item in value]
    if isinstance(value, list):
        return [json_ready(item) for item in value]
    if isinstance(value, dict):
        return {str(key): json_ready(val) for key, val in value.items()}
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not allowed in ledger output")
        return value
    return value
