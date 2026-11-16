from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_fingerprint(input_dir: Path, names: list[str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(names):
        path = input_dir / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        if path.exists():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def generation_fingerprint(
    input_dir: Path,
    *,
    files: list[str],
    config: dict[str, Any],
) -> str:
    operational_keys = {
        "concurrency",
        "precision_mode",
        "precision_error_threshold",
        "boundary_epsilon",
        "report_version",
    }
    generation_config = {
        key: value for key, value in config.items() if key not in operational_keys
    }
    digest = hashlib.sha256()
    for name in sorted(files):
        path = input_dir / name
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.exists() else b"<missing>")
        digest.update(b"\0")
    digest.update(canonical_json(generation_config))
    return digest.hexdigest()


def derive_unit_interval(seed: bytes, *parts: str) -> float:
    digest = hashlib.sha256(seed)
    for part in parts:
        digest.update(b"\0")
        digest.update(part.encode("utf-8"))
    value = int.from_bytes(digest.digest()[:16], "big")
    return value / float(1 << 128)

