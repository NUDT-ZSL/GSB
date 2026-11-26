"""Stable serialization, checksums, revision parsing and atomic file writes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def json_pretty(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def stable_rank(seed: int | str, *parts: str) -> str:
    raw = ":".join([str(seed), *parts]).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def iso_to_ms(value: str | int | float | None, fallback: int) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if not value:
        return fallback
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    normalized = text.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


_REVISION_RE = re.compile(
    r"^(?:r|rev|revision)?[-_:]?(?P<major>\d+)"
    r"(?:\.(?P<minor>\d+))?(?:\.(?P<patch>\d+))?$",
    re.IGNORECASE,
)


def parse_annotation_revision(value: Any) -> int:
    """Parse common local revision forms into a deterministic integer."""

    if isinstance(value, bool):
        raise ValueError("boolean is not a revision")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("revision must be non-negative")
        return value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("revision must be a non-empty string or integer")
    text = value.strip()
    match = _REVISION_RE.fullmatch(text)
    if not match:
        raise ValueError(
            "revision must look like 12, r12, rev-12 or 1.2.3"
        )
    major = int(match.group("major"))
    minor = int(match.group("minor") or 0)
    patch = int(match.group("patch") or 0)
    if "." not in text:
        return major
    return major * 1_000_000 + minor * 1_000 + patch


def normalize_text(value: str) -> str:
    return " ".join(value.replace("\r\n", "\n").replace("\r", "\n").split())


def cleanup_temp_files(directory: str | Path) -> list[str]:
    """Remove abandoned temp files written by an interrupted process."""

    root = Path(directory)
    removed: list[str] = []
    if not root.exists():
        return removed
    for path in root.rglob(".tmp-*"):
        if path.is_file():
            path.unlink(missing_ok=True)
            removed.append(str(path))
    return removed


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".tmp-",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
        dir_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_bytes(path, json_pretty(value).encode("utf-8"))


def atomic_write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    payload = "".join(canonical_json(row) + "\n" for row in rows)
    atomic_write_bytes(path, payload.encode("utf-8"))


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"line {line_no} is not a JSON object")
            rows.append(value)
    return rows

