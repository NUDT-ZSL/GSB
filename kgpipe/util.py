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


def pretty_json(value: Any) -> str:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def content_checksum(value: Any, *, exclude: Iterable[str] = ("checksum",)) -> str:
    excluded = set(exclude)
    if isinstance(value, dict):
        payload = {key: item for key, item in value.items() if key not in excluded}
    else:
        payload = value
    return "sha256:" + sha256_text(canonical_json(payload))


def attach_checksum(value: dict[str, Any]) -> dict[str, Any]:
    result = dict(value)
    result["checksum"] = content_checksum(result)
    return result


def verify_checksum(value: dict[str, Any], *, category: str, version: int) -> None:
    from .errors import KGError

    expected = value.get("checksum")
    if not isinstance(expected, str):
        raise KGError(
            "E_SNAPSHOT_CORRUPT",
            f"{category} 版本 {version} 缺少内容校验和",
            details={"snapshot_category": category, "version": version},
        )
    actual = content_checksum(value)
    if expected != actual:
        raise KGError(
            "E_SNAPSHOT_CORRUPT",
            f"{category} 版本 {version} 内容校验和不符",
            details={
                "snapshot_category": category,
                "version": version,
                "expected_checksum": expected,
                "actual_checksum": actual,
            },
        )


def stable_id(prefix: str, value: Any, *, length: int = 16) -> str:
    digest = sha256_text(canonical_json(value))[:length]
    return f"{prefix}_{digest}"


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_text(path: str | Path, text: str) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, destination)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def atomic_write_json(path: str | Path, value: Any) -> None:
    atomic_write_text(path, pretty_json(value))


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number} 不是 JSON 对象")
            rows.append(row)
    return rows


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    content = "".join(canonical_json(row) + "\n" for row in rows)
    atomic_write_text(path, content)


def clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, value))


def mb_to_bytes(value: float) -> int:
    return int(value * 1024 * 1024)


def json_safe_float(value: int, scale: int = 10000) -> float:
    return round(value / scale, 6)

