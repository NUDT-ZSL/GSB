from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical_dumps(value: Any) -> str:
    """Serialize JSON-native data in a stable canonical form."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_digest(value: Any, *, prefix: str = "") -> str:
    digest = hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()
    return f"{prefix}{digest}" if prefix else digest


def short_digest(value: Any, length: int = 16) -> str:
    return canonical_digest(value)[:length]


def atomic_write_json(path: str | Path, payload: Any) -> None:
    """Atomically write JSON via a same-directory temporary file and replace."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
