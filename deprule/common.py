"""Hashing, canonical JSON and atomic filesystem helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def canonical_bytes(obj: Any) -> bytes:
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def canonical_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_hash(value: str, length: int = 12) -> str:
    return value[:length]


def read_json(path: str | os.PathLike) -> Any:
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        from .errors import Codes, DomainError

        raise DomainError(
            f"JSON file does not exist: {p}",
            code=Codes.E_NOT_FOUND,
            field=str(p),
        ) from exc
    except json.JSONDecodeError as exc:
        from .errors import Codes, DomainError

        raise DomainError(
            f"invalid JSON in {p}: {exc.msg}",
            code=Codes.E_FORMAT_CORRUPT,
            field="$",
            path=str(p),
            line=exc.lineno,
            column=exc.colno,
        ) from exc


def write_json(path: str | os.PathLike, obj: Any, *, pretty: bool = True) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if pretty:
        data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    else:
        data = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    p.write_text(data, encoding="utf-8")
    return str(p)


def atomic_write_json(path: str | os.PathLike, obj: Any) -> str:
    """Write JSON through a same-directory temporary file and os.replace."""

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    data = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    with tmp.open("w", encoding="utf-8") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, p)
    return str(p)


def atomic_write_canonical(path: str | os.PathLike, obj: Any) -> str:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with tmp.open("wb") as handle:
        handle.write(canonical_bytes(obj))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, p)
    return str(p)


def write_jsonl(path: str | os.PathLike, rows: list[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def file_sha256(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
