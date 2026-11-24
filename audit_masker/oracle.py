from __future__ import annotations

import hashlib
from typing import Any, Dict

from .models import UnifiedView
from .rules import active_rules_for_field, rule_ranking_key


def _path_match(pattern: str, field_name: str) -> bool:
    normalized = pattern.strip()
    if normalized == "payload.*" or normalized == "*":
        return True
    if not normalized.startswith("payload."):
        normalized = "payload." + normalized
    candidate = "payload." + field_name
    if "*" in normalized:
        import fnmatch

        return fnmatch.fnmatchcase(candidate, normalized)
    return candidate == normalized


def _reference_mask(mask: str, value: str) -> str:
    if mask == "keep":
        return value
    if mask == "redact":
        return "***"
    if mask == "email":
        if "@" not in value:
            return "***"
        local, domain = value.rsplit("@", 1)
        return f"{local[0]}***@{domain}" if local and domain else "***"
    if mask == "last4":
        return "***" if len(value) <= 4 else "***" + value[-4:]
    if mask == "hash":
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return "sha256:" + digest[:16]
    raise ValueError(f"bad reference mask {mask!r}")


def reference_expected_fields(
    payload: Dict[str, str],
    category: str,
    view: UnifiedView,
) -> Dict[str, Dict[str, Any]]:
    """Independent, small reference implementation used only by truth validation."""
    expected: Dict[str, Dict[str, Any]] = {}
    for field_name in sorted(payload):
        matches = [
            rule
            for rule in active_rules_for_field(view, field_name)
            if rule.field_path
        ]
        matches.sort(key=rule_ranking_key)
        if matches:
            winner = matches[0]
            mask = winner.mask or "keep"
            expected[field_name] = {
                "masked_value": _reference_mask(mask, payload[field_name]),
                "winning_rule_name": winner.rule_name,
                "winning_generation": winner.generation or 0,
            }
            continue
        default = view.defaults.get(category)
        if default and any(_path_match(path, field_name) for path in default.fields):
            expected[field_name] = {
                "masked_value": _reference_mask(default.mask, payload[field_name]),
                "winning_rule_name": default.rule_name,
                "winning_generation": default.generation,
            }
        else:
            expected[field_name] = {
                "masked_value": payload[field_name],
                "winning_rule_name": "<none>",
                "winning_generation": 0,
            }
    return expected
