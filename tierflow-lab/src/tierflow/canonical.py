from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Any


def canonical_dumps(value: Any, *, indent: int | None = 2) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=indent,
        separators=None if indent else (",", ":"),
        default=str,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value, indent=None).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_hex(canonical_bytes(value))


def load_json(path: str | Path, *, decimal: bool = False) -> Any:
    raw = Path(path).read_text(encoding="utf-8")
    if decimal:
        return json.loads(raw, parse_float=Decimal, parse_int=int)
    return json.loads(raw)


def write_json(path: str | Path, value: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(canonical_dumps(value) + "\n", encoding="utf-8")
    return target
