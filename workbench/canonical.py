from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_dumps(value: Any) -> str:
    """Return the stable JSON representation used by all ledgers and hashes."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_dumps(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def observation_digest(observation: dict[str, Any]) -> str:
    """Hash the prescribed content fields, never the digest itself."""

    content = {
        "instance_id": observation["instance_id"],
        "seq": observation["seq"],
        "logical_clock_ms": observation["logical_clock_ms"],
        "status_code": observation["status_code"],
        "latency_ms": observation["latency_ms"],
    }
    return sha256_json(content)
