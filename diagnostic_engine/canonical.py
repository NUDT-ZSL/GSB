from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def canonical_dumps(value: Any) -> str:
    """Return deterministic JSON used for hashes and ledger pointers."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def signature(data: bytes) -> str:
    return f"sha256:{sha256_hex(data)}"


def write_stable_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def deterministic_payload(
    seed: int | str,
    seq: int,
    object_key: str,
    version: int,
    size: int,
) -> bytes:
    """Generate repeatable payload bytes without using external storage."""
    if size < 0:
        raise ValueError("payload size must be non-negative")
    seed_material = canonical_bytes(
        {
            "seed": seed,
            "seq": seq,
            "key": object_key,
            "version": version,
            "algorithm": "sha256-counter-v1",
        }
    )
    chunks: list[bytes] = []
    remaining = size
    counter = 0
    while remaining > 0:
        block = sha256_hex(seed_material + counter.to_bytes(8, "big")).encode("ascii")
        # Hex blocks are stable and avoid exposing binary control characters.
        take = min(remaining, len(block))
        chunks.append(block[:take])
        remaining -= take
        counter += 1
    return b"".join(chunks)
