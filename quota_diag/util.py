from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Iterator

from .errors import fail


def strict_int(value: Any, field: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise fail(
            "E_SCHEMA",
            f"{field} must be an integer",
            field=field,
            value=value,
        )
    if minimum is not None and value < minimum:
        raise fail(
            "E_SCHEMA",
            f"{field} must be >= {minimum}",
            field=field,
            value=value,
            minimum=minimum,
        )
    return value


def required_str(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise fail("E_SCHEMA", f"{field} must be a string", field=field, value=value)
    if not allow_empty and not value:
        raise fail("E_SCHEMA", f"{field} must not be empty", field=field)
    return value


def read_json(path: str | os.PathLike[str]) -> Any:
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise fail("E_FILE_NOT_FOUND", "input file does not exist", path=str(p))
    except json.JSONDecodeError as exc:
        raise fail(
            "E_JSON_PARSE",
            "invalid JSON",
            path=str(p),
            line=exc.lineno,
            column=exc.colno,
            position=exc.pos,
        )


def write_json(path: str | os.PathLike[str], payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    with p.open("w", encoding="utf-8") as fh:
        fh.write(data)


def write_json_atomic(path: str | os.PathLike[str], payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, p)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def decode_content(entry: dict[str, Any], *, line: int) -> bytes | None:
    if "content_b64" in entry:
        raw = entry["content_b64"]
        if not isinstance(raw, str):
            raise fail(
                "E_EVENT_CONTENT_ENCODING",
                "content_b64 must be a string",
                line=line,
                field="content_b64",
            )
        try:
            return base64.b64decode(raw, validate=True)
        except (ValueError, TypeError) as exc:
            raise fail(
                "E_EVENT_CONTENT_ENCODING",
                "content_b64 is not valid base64",
                line=line,
                field="content_b64",
                detail=str(exc),
            )
    for field in ("content", "payload"):
        if field in entry:
            value = entry[field]
            if not isinstance(value, str):
                raise fail(
                    "E_EVENT_CONTENT_ENCODING",
                    f"{field} must be a string",
                    line=line,
                    field=field,
                )
            return value.encode("utf-8")
    for field in ("content_base64", "data_b64"):
        if field in entry:
            value = entry[field]
            if not isinstance(value, str):
                raise fail(
                    "E_EVENT_CONTENT_ENCODING",
                    f"{field} must be a string",
                    line=line,
                    field=field,
                )
            try:
                return base64.b64decode(value, validate=True)
            except (ValueError, TypeError) as exc:
                raise fail(
                    "E_EVENT_CONTENT_ENCODING",
                    f"{field} is not valid base64",
                    line=line,
                    field=field,
                    detail=str(exc),
                )
    return None


@contextlib.contextmanager
def file_lock(db_path: str | os.PathLike[str], *, exclusive: bool) -> Iterator[None]:
    lock_path = Path(f"{db_path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as fh:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(fh.fileno(), mode)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_version(version: str) -> tuple[int, int, str]:
    prefix, sep, suffix = version.rpartition("-")
    if sep and suffix.isdigit():
        match = re.search(r"(\d+)$", prefix)
        major = int(match.group(1)) if match else -1
        return (major, int(suffix), prefix)
    if version.isdigit():
        return (int(version), 0, "")
    return (-1, 0, version)
