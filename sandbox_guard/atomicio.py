"""Atomic JSON replacement and report helpers."""

from __future__ import annotations

import itertools
import json
import os
from pathlib import Path
from typing import Any

from .codes import E_SNAPSHOT_CORRUPT
from .errors import GuardError


_TEMP_COUNTER = itertools.count(1)


def atomic_write_bytes(path: str | os.PathLike[str], data: bytes) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    token = next(_TEMP_COUNTER)
    tmp = target.parent / f".{target.name}.tmp-{os.getpid()}-{token}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        with open(tmp, "rb") as handle:
            json.loads(handle.read().decode("utf-8"))
        os.replace(tmp, target)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, json.JSONDecodeError):
            raise GuardError(
                E_SNAPSHOT_CORRUPT,
                "refusing to atomically replace with unparsable JSON",
                details={"path": str(target)},
            ) from exc
        raise


def atomic_write_json(path: str | os.PathLike[str], payload: Any) -> None:
    data = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    atomic_write_bytes(path, data)


def load_json_file(path: str | os.PathLike[str]) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)
