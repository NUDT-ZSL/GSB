from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> str:
    """Return the one canonical JSON spelling used by every digest."""
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def sha256_digest(value: Any) -> str:
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = canonical_json(value).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def normalize_digest(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("digest must be a string")
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ValueError("digest must be a 64-character SHA-256 hexadecimal value")
    return text


def digest_matches(actual: Any, expected: str) -> bool:
    try:
        return normalize_digest(actual) == normalize_digest(expected)
    except ValueError:
        return False


def semantic_digest(
    instance_id: str,
    obs_seq: int,
    outcome: str,
    status_codes: list[int],
) -> str:
    """Stable observation identity that ignores v1/v2 representation details.

    The digest deliberately excludes representation-specific absolute time,
    histogram spelling, and latency.  These fields are checked by the
    cross-version equivalence rules, while the committed ledger basis remains
    stable if an equivalent alias is loaded later.
    """
    return sha256_digest(
        {
            "domain": "probe-observation-semantic-v1",
            "instance_id": instance_id,
            "obs_seq": obs_seq,
            "outcome": outcome,
            "status_codes": sorted(status_codes),
        }
    )


def missing_digest(instance_id: str, obs_seq: int) -> str:
    return sha256_digest(
        {
            "domain": "probe-observation-missing-v1",
            "instance_id": instance_id,
            "obs_seq": obs_seq,
        }
    )
