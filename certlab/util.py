from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def pretty_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json_file(path: Path) -> Any:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)


def fault_point(name: str) -> None:
    """Test hook: simulate SIGKILL-like termination at a named durable boundary."""

    requested = os.environ.get("CERTLAB_FAULT_POINT", "")
    if requested == name:
        os._exit(86)


def canonical_alias(value: str) -> str:
    return value.strip().casefold()

