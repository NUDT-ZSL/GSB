from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json_bytes(value: Any) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return text.encode("utf-8")


def digest_bytes(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stable_hash64(domain: str, lattice_value: int) -> int:
    payload = f"fixed-join-v1|{domain}|{lattice_value}".encode("ascii")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def partition_for_key(domain: str, lattice_value: int, partitions: int) -> int:
    return stable_hash64(domain, lattice_value) % partitions
