from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Tuple


def canonical_json_bytes(value: Any) -> bytes:
    """Return deterministic canonical JSON bytes.

    Dict keys are sorted, insignificant whitespace is removed, and non-ASCII
    characters are represented literally.  ``allow_nan=False`` ensures NaN and
    Infinity can never participate in a digest.
    """

    text = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_hex(value: Any) -> str:
    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    elif isinstance(value, str):
        data = value.encode("utf-8")
    else:
        data = canonical_json_bytes(value)
    return hashlib.sha256(data).hexdigest()


def version_identity(manifest: Dict[str, Any], events: list[Dict[str, Any]]) -> Tuple[str, str, str]:
    """Build the replay version identity from manifest and ordered events.

    Event list order is intentionally preserved because adjudication depends on
    declaration order.  Object key order is normalized by canonical JSON.
    """

    manifest_digest = sha256_hex(manifest)
    events_digest = sha256_hex({"events": events})
    version_id = sha256_hex(f"{manifest_digest}:{events_digest}")
    return version_id, manifest_digest, events_digest
