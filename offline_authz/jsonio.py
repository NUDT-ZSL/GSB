"""Deterministic JSON, hashing and atomic filesystem writes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, TypeVar

T = TypeVar("T")
R = TypeVar("R")


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


def sha256_text(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def short_marker(value: Any, length: int = 16) -> str:
    return sha256_text(value)[:length]


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def parse_json_text(text: str) -> Any:
    return json.loads(text)


def atomic_write_json(path: str | Path, payload: Any) -> Path:
    """Write, parse-back, fsync and atomically replace a JSON file."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with temporary.open("r", encoding="utf-8") as handle:
            parsed = json.load(handle)
        json.dumps(parsed, allow_nan=False, sort_keys=True)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        return target
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        finally:
            pass
        raise


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(str(directory), flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def list_stale_temporary_files(directory: str | Path) -> list[str]:
    root = Path(directory)
    if not root.exists():
        return []
    return sorted(str(path) for path in root.rglob("*.tmp") if path.is_file())


def ordered_map(
    items: Iterable[T],
    worker: Callable[[T], R],
    workers: int = 1,
) -> list[R]:
    """Map while preserving input order; worker must be deterministic/pure."""

    materialized = list(items)
    worker_count = max(1, int(workers or 1))
    if worker_count == 1 or len(materialized) < 2:
        return [worker(item) for item in materialized]
    with ThreadPoolExecutor(max_workers=min(worker_count, len(materialized))) as pool:
        return list(pool.map(worker, materialized))
