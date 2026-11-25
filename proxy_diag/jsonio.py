"""Deterministic JSON and atomic filesystem output helpers."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def stable_dumps(value: Any, *, indent: int | None = None) -> str:
    """Serialize with sorted keys and no nondeterministic whitespace."""

    if indent is None:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        allow_nan=False,
    ) + "\n"


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(stable_dumps(value).encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_atomic(path: Path, value: Any, *, indent: int = 2) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        temporary.write_text(stable_dumps(value, indent=indent), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_jsonl(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(stable_dumps(value) + "\n")
