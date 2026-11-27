"""Strict loaders for the three supported schema generations."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .errors import SchemaError
from .model import (
    PORT_MAX,
    PORT_MIN,
    cidr_contains,
    is_port,
    normalized_priority,
    schema_issue,
    validate_cidr,
    validate_ip,
)


CANONICAL_SCHEMA = "natcheck-canonical/1"

_SESSION_TYPES = {
    "new": "new",
    "connect": "new",
    "open": "new",
    "create": "new",
    "session_start": "new",
    "session_new": "new",
    "established": "established",
    "establish": "established",
    "accept": "established",
    "active": "established",
    "session_established": "established",
    "teardown": "teardown",
    "close": "teardown",
    "disconnect": "teardown",
    "destroy": "teardown",
    "remove": "teardown",
    "session_end": "teardown",
    "timeout": "timeout",
    "expire": "timeout",
    "expired": "timeout",
    "session_timeout": "timeout",
    "tick": "tick",
    "advance": "tick",
    "sweep": "tick",
    "clock": "tick",
    "checkpoint": "tick",
}

_POLICY_TYPES = {
    "policy",
    "policy_change",
    "config",
    "config_change",
    "rule_change",
    "pool_change",
}

_OPERATIONS = {
    "set_priority": "set_rule_priority",
    "priority": "set_rule_priority",
    "set_rule_priority": "set_rule_priority",
    "change_priority": "set_rule_priority",
    "delete_rule": "delete_rule",
    "remove_rule": "delete_rule",
    "drop_rule": "delete_rule",
    "upsert_rule": "upsert_rule",
    "add_rule": "upsert_rule",
    "put_rule": "upsert_rule",
    "replace_rule": "upsert_rule",
    "delete_pool": "delete_pool",
    "remove_pool": "delete_pool",
    "upsert_pool": "upsert_pool",
    "add_pool": "upsert_pool",
    "put_pool": "upsert_pool",
    "replace_pool": "upsert_pool",
    "resize_pool": "upsert_pool",
}

_DIRECTIONS = {
    "in": "inbound",
    "inbound": "inbound",
    "ingress": "inbound",
    "out": "outbound",
    "outbound": "outbound",
    "egress": "outbound",
    "any": "any",
    "*": "any",
}


@dataclass
class NormalizationContext:
    source: str
    schema: str | None
    issues: list[Any] = field(default_factory=list)
    provenance: list[dict[str, Any]] = field(default_factory=list)

    def issue(
        self,
        entity_type: str | None,
        entity_id: str | None,
        field_name: str | None,
        reason: str,
    ) -> None:
        self.issues.append(
            schema_issue(
                self.schema,
                entity_type,
                entity_id,
                field_name,
                reason,
                self.source,
            )
        )

    def provenance_entry(
        self,
        entity_type: str,
        entity_id: str,
        field_name: str,
        raw_path: str,
        raw_value: Any,
        normalized_value: Any,
    ) -> None:
        self.provenance.append(
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "field": field_name,
                "raw_path": raw_path,
                "raw_value": raw_value,
                "normalized": normalized_value,
            }
        )


def _lookup(obj: Any, path: str) -> tuple[bool, Any]:
    current = obj
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _raw(
    ctx: NormalizationContext,
    obj: Any,
    path: str,
    entity_type: str,
    entity_id: str | None,
    canonical_field: str,
    *,
    required: bool = True,
) -> Any:
    present, value = _lookup(obj, path)
    if not present:
        if required:
            ctx.issue(entity_type, entity_id, path, "missing required field")
        return None
    return value


def _field(
    ctx: NormalizationContext,
    obj: Any,
    path: str,
    entity_type: str,
    entity_id: str | None,
    canonical_field: str,
    normalizer: Callable[[Any], Any],
    *,
    required: bool = True,
    error: str = "invalid value",
) -> Any:
    value = _raw(
        ctx,
        obj,
        path,
        entity_type,
        entity_id,
        canonical_field,
        required=required,
    )
    if value is None and not _lookup(obj, path)[0]:
        return None
    try:
        normalized = normalizer(value)
    except (TypeError, ValueError):
        normalized = None
    if normalized is None:
        ctx.issue(entity_type, entity_id, path, error)
        ctx.provenance_entry(
            entity_type,
            entity_id or "?",
            canonical_field,
            path,
            value,
            None,
        )
        return None
    if isinstance(normalized, str):
        normalized = normalized[:]
    ctx.provenance_entry(
        entity_type,
        entity_id or "?",
        canonical_field,
        path,
        value,
        normalized,
    )
    return normalized


def _as_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _as_nonnegative_int(value: Any) -> int | None:
    parsed = _as_int(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _as_positive_int(value: Any) -> int | None:
    parsed = _as_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _as_protocol(value: Any) -> str | None:
    text = _as_text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in {"any", "*"}:
        return "any"
    if lowered not in {"tcp", "udp", "sctp"}:
        return None
    return lowered


def _as_direction(value: Any) -> str | None:
    text = _as_text(value)
    if text is None:
        return None
    return _DIRECTIONS.get(text.lower())


def _as_priority(value: Any) -> list[int] | None:
    return normalized_priority(value)


def _as_ip(value: Any) -> str | None:
    return value if isinstance(value, str) and validate_ip(value) else None


def _as_cidr(value: Any) -> str | None:
    return value if isinstance(value, str) and validate_cidr(value) else None


def _as_port(value: Any) -> int | None:
    return value if is_port(value) else None


def _parse_port_expression(
    ctx: NormalizationContext,
    obj: Any,
    path: str,
    entity_type: str,
    entity_id: str | None,
    canonical_field: str,
    *,
    required: bool = True,
) -> int | None:
    present, raw = _lookup(obj, path)
    if not present:
        if required:
            ctx.issue(entity_type, entity_id, path, "missing required field")
        return None
    if raw is None:
        if required:
            ctx.issue(entity_type, entity_id, path, "missing required field")
        return None

    parsed: int | None = None
    if isinstance(raw, int) and not isinstance(raw, bool):
        parsed = raw if is_port(raw) else None
    elif isinstance(raw, dict):
        base_present, base = _lookup(raw, "base")
        if not base_present:
            base_present, base = _lookup(raw, "base_port")
        offset_present, offset = _lookup(raw, "offset")
        if not offset_present:
            offset_present, offset = _lookup(raw, "offset_start")
        if not base_present or not offset_present:
            ctx.issue(
                entity_type,
                entity_id,
                path,
                "port expression requires base and offset",
            )
        elif (
            isinstance(base, int)
            and not isinstance(base, bool)
            and isinstance(offset, int)
            and not isinstance(offset, bool)
            and 0 <= offset
            and 0 <= base
        ):
            candidate = base + offset
            parsed = candidate if is_port(candidate) else None
            if parsed is None:
                ctx.issue(
                    entity_type,
                    entity_id,
                    path,
                    (
                        f"port allocation out of range: base={base}, "
                        f"offset={offset}, result={candidate}"
                    ),
                )
        else:
            ctx.issue(
                entity_type,
                entity_id,
                path,
                "port base and offset must be non-negative integers",
            )
    else:
        ctx.issue(entity_type, entity_id, path, "invalid port expression")

    if parsed is None and not any(
        issue.field == path and issue.entity_id == entity_id for issue in ctx.issues
    ):
        ctx.issue(
            entity_type,
            entity_id,
            path,
            f"port must be in [{PORT_MIN}, {PORT_MAX}]",
        )
    ctx.provenance_entry(
        entity_type,
        entity_id or "?",
        canonical_field,
        path,
        raw,
        parsed,
    )
    return parsed


def _retag_provenance(
    ctx: NormalizationContext,
    entity_type: str,
    old_entity_id: str,
    new_entity_id: str | None,
) -> None:
    if not new_entity_id:
        return
    for entry in ctx.provenance:
        if (
            entry["entity_type"] == entity_type
            and entry["entity_id"] == old_entity_id
        ):
            entry["entity_id"] = new_entity_id


def _parse_window(
    ctx: NormalizationContext,
    obj: Any,
    start_path: str,
    end_path: str,
    entity_type: str,
    entity_id: str | None,
    start_field: str,
    end_field: str,
) -> tuple[int | None, int | None]:
    start = _parse_port_expression(
        ctx, obj, start_path, entity_type, entity_id, start_field
    )
    end = _parse_port_expression(
        ctx, obj, end_path, entity_type, entity_id, end_field
    )
    if start is not None and end is not None and start > end:
        ctx.issue(
            entity_type,
            entity_id,
            f"{start_path}..{end_path}",
            f"port range start {start} exceeds end {end}",
        )
    return start, end


def _parse_connection(
    ctx: NormalizationContext, raw: Any, index: int, shape: str
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        ctx.issue("connection", f"#{index}", None, "connection must be an object")
        return {}

    if shape == "v1":
        connection_id = _field(
            ctx, raw, "connection_id", "connection", f"#{index}", "connection_id",
            _as_text, error="connection_id must be a non-empty string"
        )
        greenhouse_id = _field(
            ctx, raw, "greenhouse_id", "connection", connection_id or f"#{index}",
            "greenhouse_id", _as_text
        )
        src_ip = _field(
            ctx, raw, "src_ip", "connection", connection_id or f"#{index}",
            "five_tuple.src_ip", _as_ip, error="src_ip is not a valid IP address"
        )
        dst_ip = _field(
            ctx, raw, "dst_ip", "connection", connection_id or f"#{index}",
            "five_tuple.dst_ip", _as_ip, error="dst_ip is not a valid IP address"
        )
        src_port = _parse_port_expression(
            ctx, raw, "src_port", "connection", connection_id or f"#{index}",
            "five_tuple.src_port"
        )
        dst_port = _parse_port_expression(
            ctx, raw, "dst_port", "connection", connection_id or f"#{index}",
            "five_tuple.dst_port"
        )
        protocol = _field(
            ctx, raw, "protocol", "connection", connection_id or f"#{index}",
            "five_tuple.protocol", _as_protocol
        )
        direction = _field(
            ctx, raw, "direction", "connection", connection_id or f"#{index}",
            "direction", _as_direction
        )
        timeout = _field(
            ctx, raw, "tracking_timeout", "connection", connection_id or f"#{index}",
            "tracking_timeout", _as_positive_int,
            error="tracking_timeout must be a positive integer"
        )
    elif shape == "v2":
        connection_id = _field(
            ctx, raw, "id", "connection", f"#{index}", "connection_id", _as_text,
            error="id must be a non-empty string"
        )
        greenhouse_id = _field(
            ctx, raw, "site", "connection", connection_id or f"#{index}",
            "greenhouse_id", _as_text
        )
        src_ip = _field(
            ctx, raw, "tuple.source.ip", "connection", connection_id or f"#{index}",
            "five_tuple.src_ip", _as_ip, error="source IP is not valid"
        )
        src_port = _parse_port_expression(
            ctx, raw, "tuple.source.port", "connection",
            connection_id or f"#{index}", "five_tuple.src_port"
        )
        dst_ip = _field(
            ctx, raw, "tuple.destination.ip", "connection",
            connection_id or f"#{index}", "five_tuple.dst_ip", _as_ip,
            error="destination IP is not valid"
        )
        dst_port = _parse_port_expression(
            ctx, raw, "tuple.destination.port", "connection",
            connection_id or f"#{index}", "five_tuple.dst_port"
        )
        protocol = _field(
            ctx, raw, "tuple.transport", "connection", connection_id or f"#{index}",
            "five_tuple.protocol", _as_protocol
        )
        direction = _field(
            ctx, raw, "flow", "connection", connection_id or f"#{index}",
            "direction", _as_direction
        )
        timeout = _field(
            ctx, raw, "idle_timeout", "connection", connection_id or f"#{index}",
            "tracking_timeout", _as_positive_int,
            error="idle_timeout must be a positive integer"
        )
    else:
        connection_id = _field(
            ctx, raw, "id", "connection", f"#{index}", "connection_id", _as_text,
            error="id must be a non-empty string"
        )
        greenhouse_id = _field(
            ctx, raw, "greenhouse", "connection", connection_id or f"#{index}",
            "greenhouse_id", _as_text
        )
        src_ip = _field(
            ctx, raw, "src_ip", "connection", connection_id or f"#{index}",
            "five_tuple.src_ip", _as_ip, error="src_ip is not valid"
        )
        src_port = _parse_port_expression(
            ctx, raw, "src_port", "connection", connection_id or f"#{index}",
            "five_tuple.src_port"
        )
        dst_ip = _field(
            ctx, raw, "dst_ip", "connection", connection_id or f"#{index}",
            "five_tuple.dst_ip", _as_ip, error="dst_ip is not valid"
        )
        dst_port = _parse_port_expression(
            ctx, raw, "dst_port", "connection", connection_id or f"#{index}",
            "five_tuple.dst_port"
        )
        protocol = _field(
            ctx, raw, "protocol", "connection", connection_id or f"#{index}",
            "five_tuple.protocol", _as_protocol
        )
        direction = _field(
            ctx, raw, "direction", "connection", connection_id or f"#{index}",
            "direction", _as_direction
        )
        timeout = _field(
            ctx, raw, "timeout", "connection", connection_id or f"#{index}",
            "tracking_timeout", _as_positive_int,
            error="timeout must be a positive integer"
        )

    parsed_connection = {
        "connection_id": connection_id,
        "greenhouse_id": greenhouse_id,
        "direction": direction,
        "tracking_timeout": timeout,
        "five_tuple": {
            "src_ip": src_ip,
            "src_port": src_port,
            "dst_ip": dst_ip,
            "dst_port": dst_port,
            "protocol": protocol,
        },
    }
    _retag_provenance(ctx, "connection", f"#{index}", connection_id)
    return parsed_connection


def _parse_rule(
    ctx: NormalizationContext, raw: Any, index: int, shape: str
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        ctx.issue("rule", f"#{index}", None, "mapping rule must be an object")
        return {}

    if shape == "v1":
        rule_id = _field(
            ctx, raw, "rule_id", "rule", f"#{index}", "rule_id", _as_text,
            error="rule_id must be a non-empty string"
        )
        greenhouse_id = _field(
            ctx, raw, "greenhouse_id", "rule", rule_id or f"#{index}",
            "greenhouse_id", _as_text
        )
        priority = _field(
            ctx, raw, "priority", "rule", rule_id or f"#{index}", "priority",
            _as_priority, error="priority must be an integer or numeric hierarchy"
        )
        protocol = _field(
            ctx, raw, "protocol", "rule", rule_id or f"#{index}", "protocol",
            _as_protocol
        )
        direction = _field(
            ctx, raw, "direction", "rule", rule_id or f"#{index}", "direction",
            _as_direction
        )
        src_cidr = _field(
            ctx, raw, "source_cidr", "rule", rule_id or f"#{index}", "src_cidr",
            _as_cidr, error="source_cidr is not a valid CIDR"
        )
        dst_start, dst_end = _parse_window(
            ctx,
            raw,
            "destination_port_start",
            "destination_port_end",
            "rule",
            rule_id or f"#{index}",
            "dst_port_start",
            "dst_port_end",
        )
        src_start = _parse_port_expression(
            ctx, raw, "source_port_start", "rule", rule_id or f"#{index}",
            "src_port_start", required=False
        )
        src_end = _parse_port_expression(
            ctx, raw, "source_port_end", "rule", rule_id or f"#{index}",
            "src_port_end", required=False
        )
        pool_id = _field(
            ctx, raw, "pool_id", "rule", rule_id or f"#{index}", "pool_id", _as_text
        )
    elif shape == "v2":
        rule_id = _field(
            ctx, raw, "name", "rule", f"#{index}", "rule_id", _as_text,
            error="name must be a non-empty string"
        )
        greenhouse_id = _field(
            ctx, raw, "site", "rule", rule_id or f"#{index}", "greenhouse_id",
            _as_text
        )
        priority = _field(
            ctx, raw, "rank", "rule", rule_id or f"#{index}", "priority",
            _as_priority, error="rank must be an integer or numeric hierarchy"
        )
        protocol = _field(
            ctx, raw, "match.protocol", "rule", rule_id or f"#{index}", "protocol",
            _as_protocol
        )
        direction = _field(
            ctx, raw, "match.direction", "rule", rule_id or f"#{index}", "direction",
            _as_direction
        )
        src_cidr = _field(
            ctx, raw, "match.source_cidr", "rule", rule_id or f"#{index}",
            "src_cidr", _as_cidr, error="match.source_cidr is not valid"
        )
        dst_start, dst_end = _parse_window(
            ctx,
            raw,
            "match.destination_port.from",
            "match.destination_port.to",
            "rule",
            rule_id or f"#{index}",
            "dst_port_start",
            "dst_port_end",
        )
        src_start = _parse_port_expression(
            ctx, raw, "match.source_port.from", "rule", rule_id or f"#{index}",
            "src_port_start", required=False
        )
        src_end = _parse_port_expression(
            ctx, raw, "match.source_port.to", "rule", rule_id or f"#{index}",
            "src_port_end", required=False
        )
        pool_id = _field(
            ctx, raw, "route.pool", "rule", rule_id or f"#{index}", "pool_id",
            _as_text
        )
    else:
        rule_id = _field(
            ctx, raw, "id", "rule", f"#{index}", "rule_id", _as_text,
            error="id must be a non-empty string"
        )
        greenhouse_id = _field(
            ctx, raw, "greenhouse", "rule", rule_id or f"#{index}",
            "greenhouse_id", _as_text
        )
        priority = _field(
            ctx, raw, "priority_path", "rule", rule_id or f"#{index}", "priority",
            _as_priority, error="priority_path must contain a numeric hierarchy"
        )
        protocol = _field(
            ctx, raw, "protocol", "rule", rule_id or f"#{index}", "protocol",
            _as_protocol
        )
        direction = _field(
            ctx, raw, "direction", "rule", rule_id or f"#{index}", "direction",
            _as_direction
        )
        src_cidr = _field(
            ctx, raw, "src_cidr", "rule", rule_id or f"#{index}", "src_cidr",
            _as_cidr, error="src_cidr is not valid"
        )
        base = _field(
            ctx, raw, "dst_port_base", "rule", rule_id or f"#{index}",
            "dst_port_base", _as_port, error="dst_port_base must be in port range"
        )
        offset_start = _field(
            ctx, raw, "dst_offset_start", "rule", rule_id or f"#{index}",
            "dst_offset_start", _as_nonnegative_int,
            error="dst_offset_start must be a non-negative integer"
        )
        offset_end = _field(
            ctx, raw, "dst_offset_end", "rule", rule_id or f"#{index}",
            "dst_offset_end", _as_nonnegative_int,
            error="dst_offset_end must be a non-negative integer"
        )
        dst_start = (
            base + offset_start
            if base is not None and offset_start is not None
            else None
        )
        dst_end = (
            base + offset_end
            if base is not None and offset_end is not None
            else None
        )
        if dst_start is not None and not is_port(dst_start):
            ctx.issue(
                "rule",
                rule_id or f"#{index}",
                "dst_port_base + dst_offset_start",
                f"port allocation out of range: {dst_start}",
            )
            dst_start = None
        if dst_end is not None and not is_port(dst_end):
            ctx.issue(
                "rule",
                rule_id or f"#{index}",
                "dst_port_base + dst_offset_end",
                f"port allocation out of range: {dst_end}",
            )
            dst_end = None
        if dst_start is not None and dst_end is not None and dst_start > dst_end:
            ctx.issue(
                "rule",
                rule_id or f"#{index}",
                "destination port window",
                f"port range start {dst_start} exceeds end {dst_end}",
            )
        ctx.provenance_entry(
            "rule",
            rule_id or "?",
            "dst_port_start",
            "dst_port_base + dst_offset_start",
            {"dst_port_base": base, "dst_offset_start": offset_start},
            dst_start,
        )
        ctx.provenance_entry(
            "rule",
            rule_id or "?",
            "dst_port_end",
            "dst_port_base + dst_offset_end",
            {"dst_port_base": base, "dst_offset_end": offset_end},
            dst_end,
        )
        src_start = _parse_port_expression(
            ctx, raw, "src_port_start", "rule", rule_id or f"#{index}",
            "src_port_start", required=False
        )
        src_end = _parse_port_expression(
            ctx, raw, "src_port_end", "rule", rule_id or f"#{index}",
            "src_port_end", required=False
        )
        pool_id = _field(
            ctx, raw, "pool", "rule", rule_id or f"#{index}", "pool_id", _as_text
        )

    parsed_rule = {
        "rule_id": rule_id,
        "greenhouse_id": greenhouse_id,
        "priority": priority,
        "protocol": protocol,
        "direction": direction,
        "src_cidr": src_cidr,
        "dst_port_start": dst_start,
        "dst_port_end": dst_end,
        "src_port_start": src_start,
        "src_port_end": src_end,
        "pool_id": pool_id,
    }
    _retag_provenance(ctx, "rule", f"#{index}", rule_id)
    return parsed_rule


def _parse_pool(
    ctx: NormalizationContext, raw: Any, index: int, shape: str
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        ctx.issue("pool", f"#{index}", None, "address pool must be an object")
        return {}

    if shape == "v1":
        pool_id = _field(
            ctx, raw, "pool_id", "pool", f"#{index}", "pool_id", _as_text,
            error="pool_id must be a non-empty string"
        )
        greenhouse_id = _field(
            ctx, raw, "greenhouse_id", "pool", pool_id or f"#{index}",
            "greenhouse_id", _as_text
        )
        external_ip = _field(
            ctx, raw, "external_ip", "pool", pool_id or f"#{index}",
            "external_ip", _as_ip, error="external_ip is not valid"
        )
        start, end = _parse_window(
            ctx,
            raw,
            "port_start",
            "port_end",
            "pool",
            pool_id or f"#{index}",
            "port_start",
            "port_end",
        )
    elif shape == "v2":
        pool_id = _field(
            ctx, raw, "name", "pool", f"#{index}", "pool_id", _as_text,
            error="name must be a non-empty string"
        )
        greenhouse_id = _field(
            ctx, raw, "site", "pool", pool_id or f"#{index}", "greenhouse_id",
            _as_text
        )
        external_ip = _field(
            ctx, raw, "address", "pool", pool_id or f"#{index}", "external_ip",
            _as_ip, error="address is not valid"
        )
        start, end = _parse_window(
            ctx,
            raw,
            "port_window.from",
            "port_window.to",
            "pool",
            pool_id or f"#{index}",
            "port_start",
            "port_end",
        )
    else:
        pool_id = _field(
            ctx, raw, "id", "pool", f"#{index}", "pool_id", _as_text,
            error="id must be a non-empty string"
        )
        greenhouse_id = _field(
            ctx, raw, "greenhouse", "pool", pool_id or f"#{index}",
            "greenhouse_id", _as_text
        )
        external_ip = _field(
            ctx, raw, "external_ip", "pool", pool_id or f"#{index}",
            "external_ip", _as_ip, error="external_ip is not valid"
        )
        base = _field(
            ctx, raw, "port_base", "pool", pool_id or f"#{index}",
            "port_base", _as_port, error="port_base must be in port range"
        )
        offset_start = _field(
            ctx, raw, "offset_start", "pool", pool_id or f"#{index}",
            "offset_start", _as_nonnegative_int,
            error="offset_start must be a non-negative integer"
        )
        offset_end = _field(
            ctx, raw, "offset_end", "pool", pool_id or f"#{index}",
            "offset_end", _as_nonnegative_int,
            error="offset_end must be a non-negative integer"
        )
        start = (
            base + offset_start
            if base is not None and offset_start is not None
            else None
        )
        end = (
            base + offset_end
            if base is not None and offset_end is not None
            else None
        )
        if start is not None and not is_port(start):
            ctx.issue(
                "pool",
                pool_id or f"#{index}",
                "port_base + offset_start",
                f"port allocation out of range: {start}",
            )
            start = None
        if end is not None and not is_port(end):
            ctx.issue(
                "pool",
                pool_id or f"#{index}",
                "port_base + offset_end",
                f"port allocation out of range: {end}",
            )
            end = None
        if start is not None and end is not None and start > end:
            ctx.issue(
                "pool",
                pool_id or f"#{index}",
                "port allocation window",
                f"port range start {start} exceeds end {end}",
            )
        ctx.provenance_entry(
            "pool",
            pool_id or "?",
            "port_start",
            "port_base + offset_start",
            {"port_base": base, "offset_start": offset_start},
            start,
        )
        ctx.provenance_entry(
            "pool",
            pool_id or "?",
            "port_end",
            "port_base + offset_end",
            {"port_base": base, "offset_end": offset_end},
            end,
        )

    parsed_pool = {
        "pool_id": pool_id,
        "greenhouse_id": greenhouse_id,
        "external_ip": external_ip,
        "port_start": start,
        "port_end": end,
    }
    _retag_provenance(ctx, "pool", f"#{index}", pool_id)
    return parsed_pool


def _normalize_event_type(value: Any) -> str | None:
    text = _as_text(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in _POLICY_TYPES:
        return "policy_change"
    return _SESSION_TYPES.get(lowered)


def _parse_policy_change(
    ctx: NormalizationContext,
    raw: dict[str, Any],
    event_id: str | None,
    shape: str,
) -> dict[str, Any]:
    entity = event_id or "?"
    operation_path = "operation"
    raw_operation = _raw(
        ctx, raw, operation_path, "config_change", entity, "operation"
    )
    operation = (
        _OPERATIONS.get(raw_operation.lower())
        if isinstance(raw_operation, str)
        else None
    )
    if operation is None:
        ctx.issue(
            "config_change",
            entity,
            operation_path,
            "missing or unsupported config operation",
        )
        return {}

    ctx.provenance_entry(
        "config_change",
        entity,
        "operation",
        operation_path,
        raw_operation,
        operation,
    )
    result: dict[str, Any] = {"operation": operation}

    if operation == "set_rule_priority":
        if shape == "v1":
            rule_path, priority_path = "rule_id", "priority"
        elif shape == "v2":
            rule_path, priority_path = "rule", "rank"
        else:
            rule_path, priority_path = "rule", "priority"
        rule_id = _field(
            ctx, raw, rule_path, "config_change", entity, "rule_id", _as_text
        )
        priority = _field(
            ctx,
            raw,
            priority_path,
            "config_change",
            entity,
            "priority",
            _as_priority,
            error="new priority must be an integer or numeric hierarchy",
        )
        result.update({"rule_id": rule_id, "priority": priority})
    elif operation == "delete_rule":
        rule_path = "rule_id" if shape == "v1" else "rule"
        result["rule_id"] = _field(
            ctx, raw, rule_path, "config_change", entity, "rule_id", _as_text
        )
    elif operation == "upsert_rule":
        parsed_rule: dict[str, Any] = {}
        embedded_path: str | None = None
        embedded_value = None
        for candidate in ("rule_object", "mapping_rule", "rule"):
            present, value = _lookup(raw, candidate)
            if present and isinstance(value, dict):
                embedded_path = candidate
                embedded_value = value
                break
        if embedded_path is not None:
            parsed_rule = _parse_rule(ctx, embedded_value, 0, shape)
            rule_id = parsed_rule.get("rule_id")
        else:
            rule_path = "rule_id" if shape == "v1" else "rule"
            rule_id = _field(
                ctx,
                raw,
                rule_path,
                "config_change",
                entity,
                "rule_id",
                _as_text,
            )
            if not isinstance(raw.get("rule"), dict):
                ctx.issue(
                    "config_change",
                    entity,
                    "rule",
                    "upsert_rule requires a rule object",
                )
        result["rule_id"] = rule_id
        result["rule"] = parsed_rule
        if (
            embedded_path is not None
            and parsed_rule.get("rule_id") is not None
            and isinstance(raw.get("rule_id"), str)
            and raw.get("rule_id") != parsed_rule.get("rule_id")
        ):
            ctx.issue(
                "config_change",
                entity,
                "rule_id",
                "embedded rule id does not match the change rule id",
            )
    elif operation == "delete_pool":
        pool_path = "pool_id" if shape == "v1" else "pool"
        result["pool_id"] = _field(
            ctx, raw, pool_path, "config_change", entity, "pool_id", _as_text
        )
    elif operation == "upsert_pool":
        parsed_pool: dict[str, Any] = {}
        embedded_path = None
        embedded_value = None
        for candidate in ("pool_object", "address_pool", "pool"):
            present, value = _lookup(raw, candidate)
            if present and isinstance(value, dict):
                embedded_path = candidate
                embedded_value = value
                break
        if embedded_path is not None:
            parsed_pool = _parse_pool(ctx, embedded_value, 0, shape)
            pool_id = parsed_pool.get("pool_id")
        else:
            pool_path = "pool_id" if shape == "v1" else "pool"
            pool_id = _field(
                ctx,
                raw,
                pool_path,
                "config_change",
                entity,
                "pool_id",
                _as_text,
            )
            ctx.issue(
                "config_change",
                entity,
                "pool_object",
                "upsert_pool requires a pool object",
            )
        result["pool_id"] = pool_id
        result["pool_object"] = parsed_pool
        if (
            embedded_path is not None
            and parsed_pool.get("pool_id") is not None
            and isinstance(raw.get("pool_id"), str)
            and raw.get("pool_id") != parsed_pool.get("pool_id")
        ):
            ctx.issue(
                "config_change",
                entity,
                "pool_id",
                "embedded pool id does not match the change pool id",
            )
    return result


def _parse_events(
    ctx: NormalizationContext, raw_events: Any, shape: str
) -> list[dict[str, Any]]:
    if not isinstance(raw_events, list):
        ctx.issue("event", None, "events", "events/timeline must be an array")
        return []

    result: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            ctx.issue("event", f"#{index}", None, "event must be an object")
            continue
        if shape == "v1":
            id_path, time_path, seq_path, type_path = (
                "event_id",
                "time",
                "seq",
                "type",
            )
        else:
            id_path, time_path, seq_path, type_path = (
                "id",
                "at",
                "order",
                "kind" if shape == "v2" else "action",
            )
        event_id = _field(
            ctx, raw, id_path, "event", f"#{index}", "event_id", _as_text,
            error="event id must be a non-empty string"
        )
        time = _field(
            ctx, raw, time_path, "event", event_id or f"#{index}", "time",
            _as_nonnegative_int, error="event time must be a non-negative integer"
        )
        seq = _field(
            ctx, raw, seq_path, "event", event_id or f"#{index}", "seq",
            _as_nonnegative_int, error="event seq must be a non-negative integer"
        )
        event_type = _field(
            ctx, raw, type_path, "event", event_id or f"#{index}", "type",
            _normalize_event_type, error="missing or unsupported event type"
        )
        if event_id is None or time is None or seq is None or event_type is None:
            continue

        event: dict[str, Any] = {
            "event_id": event_id,
            "time": time,
            "seq": seq,
            "type": event_type,
        }
        if event_type in {"new", "established", "teardown", "timeout"}:
            conn_path = "connection_id" if shape == "v1" else (
                "connection" if shape == "v2" else "connection"
            )
            connection_id = _field(
                ctx, raw, conn_path, "event", event_id, "connection_id", _as_text
            )
            event["connection_id"] = connection_id
        elif event_type == "policy_change":
            event.update(_parse_policy_change(ctx, raw, event_id, shape))
        result.append(event)
    return result


def _top_level(
    ctx: NormalizationContext, raw: dict[str, Any], shape: str
) -> tuple[
    str | None,
    str | None,
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    int | None,
]:
    if shape == "v1":
        project_id = _field(
            ctx, raw, "project_id", "project", None, "project_id", _as_text
        )
        domain_id = _field(
            ctx, raw, "domain_id", "project", project_id, "domain_id", _as_text
        )
        raw_connections = _raw(
            ctx, raw, "connections", "project", project_id, "connections"
        )
        raw_rules = _raw(
            ctx, raw, "mapping_rules", "project", project_id, "rules"
        )
        raw_pools = _raw(
            ctx, raw, "address_pools", "project", project_id, "pools"
        )
        raw_events = _raw(
            ctx, raw, "timeline", "project", project_id, "events"
        )
        horizon = _field(
            ctx, raw, "horizon", "project", project_id, "horizon",
            _as_nonnegative_int, error="horizon must be a non-negative integer"
        )
    elif shape == "v2":
        project_id = _field(
            ctx, raw, "project.id", "project", None, "project_id", _as_text
        )
        domain_id = _field(
            ctx, raw, "project.domain", "project", project_id, "domain_id",
            _as_text
        )
        raw_connections = _raw(
            ctx, raw, "connections", "project", project_id, "connections"
        )
        raw_rules = _raw(ctx, raw, "rules", "project", project_id, "rules")
        raw_pools = _raw(ctx, raw, "pools", "project", project_id, "pools")
        raw_events = _raw(ctx, raw, "events", "project", project_id, "events")
        horizon = _field(
            ctx, raw, "horizon", "project", project_id, "horizon",
            _as_nonnegative_int, error="horizon must be a non-negative integer"
        )
    else:
        project_id = _field(
            ctx, raw, "meta.project", "project", None, "project_id", _as_text
        )
        domain_id = _field(
            ctx, raw, "meta.domain", "project", project_id, "domain_id", _as_text
        )
        raw_connections = _raw(
            ctx, raw, "conns", "project", project_id, "connections"
        )
        raw_rules = _raw(
            ctx, raw, "map_rules", "project", project_id, "rules"
        )
        raw_pools = _raw(ctx, raw, "pools", "project", project_id, "pools")
        raw_events = _raw(
            ctx, raw, "log", "project", project_id, "events"
        )
        horizon = _field(
            ctx, raw, "meta.horizon", "project", project_id, "horizon",
            _as_nonnegative_int, error="horizon must be a non-negative integer"
        )

    connections: list[dict[str, Any]] = []
    if isinstance(raw_connections, list):
        for index, item in enumerate(raw_connections):
            connections.append(_parse_connection(ctx, item, index, shape))
    else:
        ctx.issue("connection", None, "connections", "connections must be an array")

    rules: list[dict[str, Any]] = []
    if isinstance(raw_rules, list):
        for index, item in enumerate(raw_rules):
            rules.append(_parse_rule(ctx, item, index, shape))
    else:
        ctx.issue("rule", None, "rules", "mapping rules must be an array")

    pools: list[dict[str, Any]] = []
    if isinstance(raw_pools, list):
        for index, item in enumerate(raw_pools):
            pools.append(_parse_pool(ctx, item, index, shape))
    else:
        ctx.issue("pool", None, "pools", "address pools must be an array")

    events = _parse_events(ctx, raw_events, shape)
    return project_id, domain_id, connections, rules, pools, events, horizon


def _detect_schema(ctx: NormalizationContext, raw: Any) -> str | None:
    if not isinstance(raw, dict):
        ctx.issue(None, None, None, "project root must be an object")
        return None
    version = None
    for key in ("schema_version", "schema", "version"):
        if key in raw:
            version = raw[key]
            break
    if version is None:
        ctx.issue(None, None, "schema_version", "missing required schema version")
        return None
    if not isinstance(version, str):
        ctx.issue(None, None, "schema_version", "schema version must be a string")
        return None
    text = version.strip().lower()
    if re.fullmatch(r"(?:v|schema[-_. ]?|version[-_. ]?)?1(?:[._-]0)?", text):
        return "v1"
    if re.fullmatch(r"(?:v|schema[-_. ]?|version[-_. ]?)?2(?:[._-]0)?", text):
        return "v2"
    if re.fullmatch(r"(?:v|schema[-_. ]?|version[-_. ]?)?3(?:[._-]0)?", text):
        return "v3"
    ctx.issue(None, None, "schema_version", f"unsupported schema version: {version}")
    return None


def _validate_project(
    ctx: NormalizationContext,
    connections: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    pools: list[dict[str, Any]],
    events: list[dict[str, Any]],
    horizon: int | None,
) -> None:
    connection_ids: set[str] = set()
    for conn in connections:
        connection_id = conn.get("connection_id")
        if not connection_id:
            continue
        if connection_id in connection_ids:
            ctx.issue(
                "connection", connection_id, "connection_id", "duplicate connection id"
            )
        connection_ids.add(connection_id)

    rule_ids: set[str] = set()
    for rule in rules:
        rule_id = rule.get("rule_id")
        if not rule_id:
            continue
        if rule_id in rule_ids:
            ctx.issue("rule", rule_id, "rule_id", "duplicate rule id")
        rule_ids.add(rule_id)

    pool_ids: set[str] = set()
    for pool in pools:
        pool_id = pool.get("pool_id")
        if not pool_id:
            continue
        if pool_id in pool_ids:
            ctx.issue("pool", pool_id, "pool_id", "duplicate pool id")
        pool_ids.add(pool_id)

    for rule in rules:
        if not rule.get("rule_id"):
            continue
        if rule.get("pool_id") not in pool_ids:
            ctx.issue(
                "rule",
                rule.get("rule_id"),
                "pool_id",
                f"unknown pool reference: {rule.get('pool_id')}",
            )

    event_keys: set[tuple[int, int]] = set()
    event_ids: set[str] = set()
    maximum_event_time = 0
    for event in events:
        event_id = event.get("event_id")
        time = event.get("time")
        seq = event.get("seq")
        if event_id:
            if event_id in event_ids:
                ctx.issue(
                    "event", event_id, "event_id", "duplicate event id"
                )
            event_ids.add(event_id)
        if time is not None:
            maximum_event_time = max(maximum_event_time, time)
        if time is not None and seq is not None:
            key = (time, seq)
            if key in event_keys:
                ctx.issue(
                    "event",
                    event_id,
                    "time/seq",
                    f"duplicate event ordering key: time={time}, seq={seq}",
                )
            event_keys.add(key)
        if event.get("type") in {"new", "established", "teardown", "timeout"}:
            connection_id = event.get("connection_id")
            if connection_id not in connection_ids:
                ctx.issue(
                    "event",
                    event_id,
                    "connection_id",
                    f"unknown connection reference: {connection_id}",
                )

    if horizon is not None and maximum_event_time > horizon:
        ctx.issue(
            "project",
            None,
            "horizon",
            f"horizon {horizon} is before event time {maximum_event_time}",
        )


def load_project(path: str | Path) -> dict[str, Any]:
    source = str(path)
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SchemaError(
            [schema_issue(None, None, None, None, "file not found", source)], source
        )
    except json.JSONDecodeError as exc:
        raise SchemaError(
            [
                schema_issue(
                    None,
                    None,
                    None,
                    None,
                    f"invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}",
                    source,
                )
            ],
            source,
        )
    return normalize_project(raw, source=source)


def normalize_project(raw: Any, source: str | None = None) -> dict[str, Any]:
    ctx = NormalizationContext(source=source or "<memory>", schema=None)
    shape = _detect_schema(ctx, raw)
    ctx.schema = shape
    if shape is None:
        raise SchemaError(ctx.issues, ctx.source)

    (
        project_id,
        domain_id,
        connections,
        rules,
        pools,
        events,
        horizon,
    ) = _top_level(ctx, raw, shape)
    _validate_project(ctx, connections, rules, pools, events, horizon)
    if ctx.issues:
        raise SchemaError(ctx.issues, ctx.source)

    events.sort(key=lambda event: (event["time"], event["seq"]))
    model = {
        "canonical_schema": CANONICAL_SCHEMA,
        "source_schema": shape,
        "project_id": project_id,
        "domain_id": domain_id,
        "horizon": horizon,
        "connections": connections,
        "rules": rules,
        "pools": pools,
        "events": events,
        "provenance": ctx.provenance,
    }
    return model


def canonical_for_comparison(model: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(model[key])
        for key in (
            "canonical_schema",
            "project_id",
            "domain_id",
            "horizon",
            "connections",
            "rules",
            "pools",
            "events",
        )
    }


def rule_matches(rule: dict[str, Any], connection: dict[str, Any]) -> bool:
    five_tuple = connection["five_tuple"]
    if rule["greenhouse_id"] not in {"*", "any", connection["greenhouse_id"]}:
        return False
    if rule["protocol"] not in {"any", five_tuple["protocol"]}:
        return False
    if rule["direction"] not in {"any", connection["direction"]}:
        return False
    if not cidr_contains(rule["src_cidr"], five_tuple["src_ip"]):
        return False
    if not (
        rule["dst_port_start"] <= five_tuple["dst_port"] <= rule["dst_port_end"]
    ):
        return False
    if rule["src_port_start"] is not None and rule["src_port_end"] is not None:
        if not (
            rule["src_port_start"] <= five_tuple["src_port"] <= rule["src_port_end"]
        ):
            return False
    return True


def select_rule(
    rules: dict[str, dict[str, Any]], connection: dict[str, Any]
) -> dict[str, Any] | None:
    candidates = [rule for rule in rules.values() if rule_matches(rule, connection)]
    if not candidates:
        return None
    candidates.sort(key=lambda rule: (tuple(rule["priority"]), rule["rule_id"]))
    return copy.deepcopy(candidates[0])

