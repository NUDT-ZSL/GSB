from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _fsync_dir(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY
    try:
        fd = os.open(str(directory), flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Write bytes to a temporary file, validate caller-visible parsing later, then rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # The replacement boundary only sees parseable content.
        if not data:
            raise ValueError("refusing to atomically replace with empty content")
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        finally:
            pass
        raise


def atomic_write_json(path: str | Path, payload: Any) -> bytes:
    data = canonical_json_bytes(payload)
    # Validate exactly the bytes that will be renamed.
    json.loads(data.decode("utf-8"))
    atomic_write_bytes(path, data)
    return data


def canonical_json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            indent=2,
        )
        + "\n"
    ).encode("utf-8")


def parse_json_file(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))
