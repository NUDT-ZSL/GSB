"""Canonical JSON helpers used by every deterministic artifact."""

from __future__ import annotations

import hashlib
import json
from typing import Any


class CanonicalJSONError(ValueError):
    """Raised when a value cannot be represented as canonical JSON."""


def canonical_dumps(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise CanonicalJSONError(str(exc)) from exc


def sha256_canonical(value: Any) -> str:
    return hashlib.sha256(canonical_dumps(value).encode("utf-8")).hexdigest()


def pretty_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )

