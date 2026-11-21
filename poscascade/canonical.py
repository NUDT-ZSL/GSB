from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def object_checksum(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def text_checksum(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))


def quantize(value: float, digits: int = 12) -> float:
    rounded = round(float(value), digits)
    if rounded == 0:
        return 0.0
    return rounded


def canonical_jsonl(rows: Iterable[dict[str, Any]]) -> str:
    return "".join(canonical_json(row) + "\n" for row in rows)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
        try:
            dir_fd = os.open(str(target.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def atomic_write_text(path: str | Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: str | Path) -> list[tuple[int, Any]]:
    rows: list[tuple[int, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for index, raw_line in enumerate(handle, start=1):
            if not raw_line.strip():
                continue
            rows.append((index, json.loads(raw_line)))
    return rows
