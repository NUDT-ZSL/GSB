from __future__ import annotations

import hashlib
import re
from typing import Iterable, Optional, Tuple


DEFAULT_MASK_METHODS: Tuple[str, ...] = (
    "keep",
    "redact",
    "email",
    "last4",
    "hash",
)

MASK_ALIASES = {
    "none": "keep",
    "plain": "keep",
    "remove": "redact",
    "mask": "redact",
    "sha256": "hash",
}


def canonical_mask_method(method: str) -> str:
    normalized = method.strip().lower()
    return MASK_ALIASES.get(normalized, normalized)


def mask_value(method: str, value: str) -> str:
    method = canonical_mask_method(method)
    if method == "keep":
        return value
    if method == "redact":
        return "***"
    if method == "email":
        if "@" not in value:
            return "***"
        local, domain = value.rsplit("@", 1)
        if not local or not domain:
            return "***"
        return f"{local[0]}***@{domain}"
    if method == "last4":
        if len(value) <= 4:
            return "***"
        return f"***{value[-4:]}"
    if method == "hash":
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"sha256:{digest[:16]}"
    raise ValueError(f"unsupported mask method: {method}")


def canonical_field_path(path: str) -> str:
    cleaned = path.strip()
    if cleaned in ("*", "payload.*", "payload"):
        return "payload.*" if cleaned != "payload" else "payload.*"
    if cleaned.startswith("payload."):
        return cleaned
    return f"payload.{cleaned}"


def _wildcard_regex(pattern: str) -> "re.Pattern[str]":
    escaped = re.escape(pattern)
    escaped = escaped.replace(r"\*", ".*")
    return re.compile(rf"^{escaped}$")


def field_matches_path(rule_path: Optional[str], field_name: str) -> bool:
    if not rule_path:
        return False
    pattern = canonical_field_path(rule_path)
    candidate = canonical_field_path(field_name)
    if pattern == "payload.*":
        return True
    if "*" not in pattern:
        return pattern == candidate
    return bool(_wildcard_regex(pattern).match(candidate))


def matching_payload_fields(rule_paths: Iterable[str], payload_keys: Iterable[str]) -> set:
    matched = set()
    patterns = list(rule_paths)
    for key in payload_keys:
        if any(field_matches_path(pattern, key) for pattern in patterns):
            matched.add(key)
    return matched


def mask_method_allowed(method: str, allowed: Iterable[str]) -> bool:
    canonical = canonical_mask_method(method)
    return canonical in {canonical_mask_method(item) for item in allowed}
