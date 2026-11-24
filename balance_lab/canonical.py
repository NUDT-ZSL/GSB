from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_text(value: Any) -> str:
    """Serialize JSON-compatible data in a stable cross-process order."""
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_text(value).encode("utf-8")


def digest_value(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: str | Path, value: Any, *, indent: int = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=indent, allow_nan=False)
        handle.write("\n")


def atomic_write_bytes(path: str | Path, payload: bytes) -> None:
    """Write beside the target and atomically replace it.

    ``os.replace`` is used on both POSIX and Windows. The temporary file is in
    the destination directory, so source and target are on the same volume.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            pass
        raise


def atomic_write_json(path: str | Path, value: Any, *, indent: int = 2) -> None:
    payload = (
        json.dumps(value, ensure_ascii=False, indent=indent, allow_nan=False) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, payload)
