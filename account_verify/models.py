from __future__ import annotations

from typing import Any, Dict, List

ALGORITHM_CATALOG: Dict[str, Dict[str, Any]] = {
    "legacy-pbkdf2-sha1": {"rank": 10, "new_tier": False, "hash": "sha1"},
    "pbkdf2-sha256": {"rank": 20, "new_tier": True, "hash": "sha256"},
    "argon2id": {"rank": 30, "new_tier": True, "hash": None},
}


def is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def rejection(
    source_category: str,
    record_index: int,
    code: str,
    message: str,
    **details: Any,
) -> Dict[str, Any]:
    return {
        "source_category": source_category,
        "record_index": record_index,
        "record_no": record_index + 1 if record_index >= 0 else None,
        "code": code,
        "message": message,
        "details": details,
    }


def pick(record: Dict[str, Any], *names: str, default=None):
    for name in names:
        if name in record and record[name] is not None:
            return record[name]
    return default


def records_from_payload(payload: Any) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("records", "credentials", "tokens", "policies", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []
