"""Canonical serialization, checksums and file helpers."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from pathlib import Path
from typing import Any, Iterable


def canonical_json(obj: Any) -> str:
    """Return canonical JSON without a trailing newline."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def pretty_json(obj: Any) -> str:
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_obj(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def payload_checksum(payload: dict[str, Any]) -> str:
    """Checksum a payload while excluding its own checksum field."""
    body = {key: value for key, value in payload.items() if key != "checksum"}
    return sha256_obj(body)


def with_checksum(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    result.pop("checksum", None)
    result["checksum"] = payload_checksum(result)
    return result


def write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)


def write_json(path: str | Path, obj: Any) -> None:
    write_text(path, pretty_json(obj))


def read_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    text = "".join(canonical_json(row) + "\n" for row in rows)
    write_text(path, text)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSONL at line {line_number}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"JSONL line {line_number} is not an object")
            rows.append(row)
    return rows


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def library_manifest(root: str | Path) -> dict[str, Any]:
    root_path = Path(root)
    files = sorted(
        path for path in root_path.rglob("*") if path.is_file() and path.suffix in {".py", ".toml", ".txt", ".md"}
    )
    records = []
    for path in files:
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        records.append(
            {
                "path": path.relative_to(root_path).as_posix(),
                "sha256": file_sha256(path),
            }
        )
    combined = sha256_obj(records)
    return {
        "file_count": len(records),
        "checksum": combined,
        "files": records,
    }


def environment_info() -> dict[str, Any]:
    try:
        import numpy as np

        numpy_version = np.__version__
    except Exception:  # pragma: no cover - diagnostic only
        numpy_version = "missing"
    return {
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "numpy_version": numpy_version,
        "platform": platform.platform(),
        "executable": sys.executable,
    }


def round_float(value: float, digits: int = 6) -> float:
    return round(float(value), digits)

