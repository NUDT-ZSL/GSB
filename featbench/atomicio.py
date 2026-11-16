"""Atomic file writes and JSON load helpers built on temp file + os.replace."""
from __future__ import annotations

import json
import os
import pickle
import tempfile
from typing import Any

SNAPSHOT_VERSION = 1


def atomic_write_bytes(path: str, payload: bytes) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def atomic_write_text(path: str, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: str, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, sort_keys=True, indent=2,
                                       ensure_ascii=False))


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write_pickle(path: str, obj: Any) -> None:
    atomic_write_bytes(path, pickle.dumps(obj, protocol=4))


def read_pickle(path: str) -> Any:
    with open(path, "rb") as handle:
        return pickle.load(handle)
