"""Canonical keys and deterministic serialization helpers."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def is_safe_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(ID_RE.fullmatch(value))


def profile_key(profile_id: str) -> str:
    return f"profile:{profile_id}"


def grant_key(key: str) -> str:
    if key.startswith("grant:"):
        return key
    return f"grant:{key}"


def raw_grant_key(canonical: str) -> str:
    return canonical[6:] if canonical.startswith("grant:") else canonical


def process_key(pid: int | str) -> str:
    return f"process:{pid}"


def canonicalize_dependency(value: str) -> str:
    return grant_key(value.strip())
