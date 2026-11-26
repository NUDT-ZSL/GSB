from __future__ import annotations

import re
from typing import Any, Dict

from .errors import Code, WorkbenchError


_INT_RE = re.compile(r"^-?\d+$")


def is_plain_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def require_nonnegative_int(value: Any, code: str, field: str, **details: Any) -> int:
    if not is_plain_int(value) or value < 0:
        raise WorkbenchError(code, f"{field} must be a non-negative integer", {"field": field, "value": value, **details})
    return value


def require_positive_int(value: Any, code: str, field: str, **details: Any) -> int:
    if not is_plain_int(value) or value <= 0:
        raise WorkbenchError(code, f"{field} must be a positive integer", {"field": field, "value": value, **details})
    return value


def fnv1a_32(value: str) -> int:
    digest = 2166136261
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * 16777619) & 0xFFFFFFFF
    return digest


def parse_key_rule(rule: Any, partition_count: int) -> Dict[str, Any]:
    if not isinstance(rule, str) or ":" not in rule:
        raise WorkbenchError(
            Code.KEY_RULE_INVALID,
            "partition key rule must look like suffix_mod:N, fnv1a_mod:N, or map:key=partition,...",
            {"rule": rule},
        )
    kind, expression = rule.split(":", 1)
    kind = kind.strip()
    expression = expression.strip()
    if kind in {"suffix_mod", "fnv1a_mod"}:
        if not _INT_RE.fullmatch(expression):
            raise WorkbenchError(Code.KEY_RULE_INVALID, f"{kind} modulus must be an integer", {"rule": rule})
        modulus = int(expression)
        if modulus <= 0:
            raise WorkbenchError(Code.KEY_RULE_INVALID, "key rule modulus must be positive", {"rule": rule})
        if modulus != partition_count:
            raise WorkbenchError(
                Code.KEY_RULE_INVALID,
                "key rule modulus must equal partition count",
                {"rule": rule, "modulus": modulus, "partitions": partition_count},
            )
        return {"kind": kind, "modulus": modulus, "raw": rule}
    if kind == "map":
        mapping: Dict[str, int] = {}
        if not expression:
            raise WorkbenchError(Code.KEY_RULE_INVALID, "map rule must contain at least one key", {"rule": rule})
        for pair in expression.split(","):
            if "=" not in pair:
                raise WorkbenchError(Code.KEY_RULE_INVALID, "map rule pair must be key=partition", {"pair": pair, "rule": rule})
            key, raw_partition = pair.split("=", 1)
            key = key.strip()
            raw_partition = raw_partition.strip()
            if not key or not _INT_RE.fullmatch(raw_partition):
                raise WorkbenchError(Code.KEY_RULE_INVALID, "map rule has invalid key or partition", {"pair": pair})
            partition = int(raw_partition)
            if partition < 0 or partition >= partition_count:
                raise WorkbenchError(
                    Code.KEY_RULE_INVALID,
                    "mapped partition is outside topic partition range",
                    {"key": key, "partition": partition, "partitions": partition_count},
                )
            if key in mapping and mapping[key] != partition:
                raise WorkbenchError(Code.KEY_RULE_INVALID, "map rule contains conflicting key assignments", {"key": key, "rule": rule})
            mapping[key] = partition
        return {"kind": "map", "mapping": mapping, "raw": rule}
    raise WorkbenchError(Code.KEY_RULE_INVALID, "unsupported partition key rule kind", {"rule": rule, "kind": kind})


def partition_for_key(parsed_rule: Dict[str, Any], key: str, partition_count: int) -> int:
    kind = parsed_rule["kind"]
    if kind == "suffix_mod":
        match = re.search(r"(\d+)$", key)
        if not match:
            raise WorkbenchError(Code.EVENT_KEY_UNCOVERED, "key has no trailing numeric suffix for suffix_mod rule", {"key": key, "rule": parsed_rule["raw"]})
        return int(match.group(1)) % parsed_rule["modulus"]
    if kind == "fnv1a_mod":
        return fnv1a_32(key) % parsed_rule["modulus"]
    mapping = parsed_rule["mapping"]
    if key not in mapping:
        raise WorkbenchError(Code.EVENT_KEY_UNCOVERED, "key is not covered by map rule", {"key": key, "rule": parsed_rule["raw"]})
    return mapping[key]
