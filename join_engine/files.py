from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator

from .canonical import canonical_json


def fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with temp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    data = (
        json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, data)


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row at {path}:{line_number} is not an object")
            yield value


def digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def count_jsonl_rows(path: Path) -> int:
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            if line.strip():
                count += 1
    return count


def write_jsonl_rows(
    path: Path,
    rows: Iterable[dict[str, Any]],
    *,
    row_transform=None,
) -> tuple[str, int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
    digest = hashlib.sha256()
    size = 0
    count = 0
    try:
        with temp.open("wb") as handle:
            for row in rows:
                output_row = row_transform(row) if row_transform else row
                encoded = (canonical_json(output_row) + "\n").encode("utf-8")
                handle.write(encoded)
                digest.update(encoded)
                size += len(encoded)
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        fsync_directory(path.parent)
        return digest.hexdigest(), size, count
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)


class JsonlSpool:
    def __init__(self, final_path: Path, *, label: str = "") -> None:
        self.final_path = final_path
        self.label = label
        self.temp_path = (
            final_path.parent
            / f".tmp-{os.getpid()}-{final_path.stem}-{uuid.uuid4().hex}"
        )
        self._handle = None
        self.digest = hashlib.sha256()
        self.size = 0
        self.rows = 0
        self.key_digest = hashlib.sha256()

    def open(self) -> "JsonlSpool":
        self.final_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.temp_path.open("wb")
        return self

    def write(self, row: dict[str, Any], *, key_token_value: str | None = None) -> None:
        if self._handle is None:
            raise RuntimeError("spool is not open")
        encoded = (canonical_json(row) + "\n").encode("utf-8")
        self._handle.write(encoded)
        self.digest.update(encoded)
        self.size += len(encoded)
        self.rows += 1
        if key_token_value is not None:
            self.key_digest.update(key_token_value.encode("utf-8"))
            self.key_digest.update(b"\n")

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            os.fsync(self._handle.fileno())
            self._handle.close()
            self._handle = None

    def commit(self) -> tuple[str, int, int, str]:
        self.close()
        if not self.temp_path.exists() and self.final_path.exists():
            return digest_file(self.final_path)[0], self.final_path.stat().st_size, self.rows, (
                self.key_digest.hexdigest()
            )
        os.replace(self.temp_path, self.final_path)
        fsync_directory(self.final_path.parent)
        return self.digest.hexdigest(), self.size, self.rows, self.key_digest.hexdigest()

    def discard(self) -> None:
        try:
            self.close()
        finally:
            self.temp_path.unlink(missing_ok=True)


def temporary_jsonl_run(directory: Path, prefix: str) -> tuple[Path, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f".{prefix}-{os.getpid()}-{uuid.uuid4().hex}.run"
    handle = path.open("wb")
    return path, handle
