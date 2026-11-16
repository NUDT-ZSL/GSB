"""Deterministic serialization and filesystem helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import RowState


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def state_digest(rows: Mapping[str, Any] | Iterable[RowState]) -> str:
    """Hash only row keys and payloads, not row/global versions."""

    if isinstance(rows, Mapping):
        payloads: dict[str, Any] = {}
        for key, value in rows.items():
            if isinstance(value, RowState):
                payloads[str(key)] = value.payload
            else:
                payloads[str(key)] = value
    else:
        payloads = {}
        for row in rows:
            if isinstance(row, RowState):
                payloads[row.key] = row.payload
            elif isinstance(row, Mapping):
                payloads[str(row["key"])] = row["payload"]
            else:
                raise TypeError("rows must contain RowState or row dictionaries")
    digest_input = canonical_json(
        {key: payloads[key] for key in sorted(payloads)}
    )
    return hashlib.sha256(digest_input.encode("utf-8")).hexdigest()


def script_fingerprint(raw_script: Any) -> str:
    return hashlib.sha256(canonical_json(raw_script).encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically so a forced stop leaves either old or new state."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                payload,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
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


_SAFE_SNAPSHOT_NAME = re.compile(r"^[A-Za-z0-9._\-\u4e00-\u9fff]+$")


def safe_snapshot_name(name: str) -> str:
    name = name.strip()
    if not name or not _SAFE_SNAPSHOT_NAME.fullmatch(name):
        raise ValueError(
            "snapshot name may contain letters, digits, '.', '_', '-' and CJK characters"
        )
    return name


def resolve_relative_path(base_file: Path, candidate: str) -> Path:
    path = Path(candidate)
    if path.is_absolute():
        return path
    return (base_file.parent / path).resolve()


def read_text(path: str | Path, *, code: str, label: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        from .errors import LabError

        raise LabError(code, f"cannot read {label} {path}: {exc}") from exc


def read_json(path: str | Path, *, code: str, label: str) -> Any:
    text = read_text(path, code=code, label=label)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        from .errors import LabError

        raise LabError(
            code,
            f"invalid JSON in {label} {path}: line {exc.lineno}, column {exc.colno}",
        ) from exc
