"""Canonical model helpers shared by loaders, storage and display code."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
from typing import Any, Iterable

from .errors import SchemaIssue


PORT_MIN = 1
PORT_MAX = 65535
STATES = ("new", "established", "teardown", "timeout")
TERMINAL_STATES = ("teardown", "timeout")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalized_priority(value: int | str) -> list[int] | None:
    """Convert an integer or a numeric hierarchy into a stable priority vector.

    Lower vectors win. A numeric hierarchy such as ``zone.edge.10.0`` becomes
    ``[10]`` so that trailing zero levels are equivalent to integer priority 10.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return [value] if value >= 0 else None
    if isinstance(value, str):
        if re.search(r"(?:^|[./_\s])-[0-9]", value):
            return None
        numbers = re.findall(r"[0-9]+", value.replace("-", "."))
        if not numbers:
            return None
        segments = [int(number) for number in numbers]
        while len(segments) > 1 and segments[-1] == 0:
            segments.pop()
        return segments
    return None


def priority_key(priority: Iterable[int]) -> tuple[int, ...]:
    return tuple(int(part) for part in priority)


def is_port(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and PORT_MIN <= value <= PORT_MAX
    )


def validate_ip(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


def validate_cidr(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        return False
    return True


def cidr_contains(cidr: str, address: str) -> bool:
    try:
        return ipaddress.ip_address(address) in ipaddress.ip_network(
            cidr, strict=False
        )
    except ValueError:
        return False


def schema_issue(
    schema: str | None,
    entity_type: str | None,
    entity_id: str | None,
    field: str | None,
    reason: str,
    source: str | None = None,
) -> SchemaIssue:
    return SchemaIssue(
        code="E_SCHEMA",
        schema=schema,
        entity_type=entity_type,
        entity_id=entity_id,
        field=field,
        reason=reason,
        source=source,
    )


def final_state_counts(sessions: list[dict[str, Any]]) -> dict[str, int]:
    counts = {state: 0 for state in STATES}
    for session in sessions:
        state = session.get("state")
        if state in counts:
            counts[state] += 1
    return counts


def summarize_mapping(mapping: dict[str, Any] | None) -> str:
    if not mapping:
        return "unmapped"
    if mapping.get("status") != "mapped":
        return f"unmapped:{mapping.get('reason', 'unknown')}"
    return (
        f"{mapping.get('rule_id')}->"
        f"{mapping.get('external_ip')}:{mapping.get('external_port')}"
    )

