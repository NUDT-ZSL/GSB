from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Iterable

from .errors import DigestError


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_value(value: Any) -> str:
    return sha256_bytes(canonical_bytes(value))


def digest_of_body(body: Any, digest_field: str = "content_digest") -> str:
    if not isinstance(body, dict):
        raise TypeError("digest body must be a JSON object")
    value = deepcopy(body)
    value.pop(digest_field, None)
    return sha256_value(value)


def with_digest(body: dict[str, Any], digest_field: str = "content_digest") -> dict[str, Any]:
    result = deepcopy(body)
    result[digest_field] = digest_of_body(result, digest_field)
    return result


def verify_digest(
    body: Any,
    *,
    digest_field: str = "content_digest",
    expected: str | None = None,
    location: str | None = None,
) -> str:
    if not isinstance(body, dict):
        raise DigestError("digest body is not an object", location=location)
    recorded = body.get(digest_field)
    if expected is not None:
        if recorded is not None and recorded != expected:
            raise DigestError(
                "snapshot digest disagrees with metadata",
                location=location,
                details={"recorded": recorded, "metadata": expected},
            )
        recorded = expected
    if not isinstance(recorded, str) or not recorded.startswith("sha256:"):
        raise DigestError("snapshot digest is missing or malformed", location=location)
    actual = digest_of_body(body, digest_field)
    if actual != recorded:
        raise DigestError(
            "snapshot content digest mismatch",
            location=location,
            details={"recorded": recorded, "actual": actual},
        )
    return actual


def stable_sorted(values: Iterable[Any]) -> list[Any]:
    return sorted(values, key=canonical_json)

