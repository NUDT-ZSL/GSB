"""Rule configuration, conversion, and role lookup helpers."""

from __future__ import annotations

import json
import re
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .errors import JsonInvalidError
from .model import Document


ROUNDING_MODES = {
    "half_up": ROUND_HALF_UP,
    "half_even": ROUND_HALF_EVEN,
    "floor": ROUND_FLOOR,
    "ceil": ROUND_CEILING,
}


def load_rule_config(path: str | Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JsonInvalidError(f"cannot load rule config: {exc}") from exc
    if not isinstance(value, dict):
        raise JsonInvalidError("rule config must be a JSON object")
    for key in (
        "version",
        "conversions",
        "relation_triggers",
        "event_triggers",
        "event_roles",
        "entity_types",
        "roles",
        "confidence_bands",
    ):
        if key not in value:
            raise JsonInvalidError(f"rule config missing key {key!r}")
    return value


def max_workers(config: dict[str, Any], override: int | None = None) -> int:
    if override is not None:
        return max(1, int(override))
    return max(1, int(config.get("max_workers", 4)))


def units_per_second(config: dict[str, Any]) -> Decimal:
    value = config.get("conversions", {}).get("v1_units_per_second", 1000)
    result = Decimal(str(value))
    if result <= 0:
        raise JsonInvalidError("conversions.v1_units_per_second must be positive")
    return result


def v1_origin(config: dict[str, Any]) -> Decimal:
    return Decimal(str(config.get("conversions", {}).get("v1_origin", 0)))


def rounding_mode(config: dict[str, Any]):
    name = str(config.get("conversions", {}).get("v2_to_v1_rounding", "half_up"))
    try:
        return ROUNDING_MODES[name]
    except KeyError as exc:
        raise JsonInvalidError(f"unknown v2_to_v1_rounding {name!r}") from exc


def v1_units_to_seconds(value: int, config: dict[str, Any]) -> Decimal:
    return (Decimal(value) - v1_origin(config)) / units_per_second(config)


def seconds_to_v1_units(value: Decimal, config: dict[str, Any]) -> int:
    raw = value * units_per_second(config) + v1_origin(config)
    return int(raw.to_integral_value(rounding=rounding_mode(config)))


def seconds_to_v1_units_exact(value: Decimal, config: dict[str, Any]) -> tuple[int, Decimal]:
    units = seconds_to_v1_units(value, config)
    restored = v1_units_to_seconds(units, config)
    return units, abs(restored - value)


def document_duration_seconds(document: Document, config: dict[str, Any]) -> Decimal:
    if not document.token_positions:
        return Decimal(str(config.get("conversions", {}).get("annotation_margin_seconds", 1)))
    maximum = max(position.end for position in document.token_positions)
    margin = Decimal(str(config.get("conversions", {}).get("annotation_margin_seconds", 1)))
    return maximum / units_per_second(config) + margin


def event_roles(config: dict[str, Any], event_type: str) -> list[str]:
    value = config.get("event_roles", {}).get(event_type, [])
    if not isinstance(value, list):
        raise JsonInvalidError(f"event_roles[{event_type!r}] must be a list")
    return [str(item) for item in value]


def all_roles(config: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for roles in config.get("event_roles", {}).values():
        if isinstance(roles, list):
            result.update(str(role) for role in roles)
    return result


def role_key(config: dict[str, Any], role_name: str) -> str:
    role_config = config.get("roles", {}).get(role_name)
    if isinstance(role_config, dict) and role_config.get("key"):
        return str(role_config["key"])
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", role_name).strip("_").upper()
    return "R_" + (cleaned or "ROLE")


def role_name_from_key(config: dict[str, Any], key: str) -> str | None:
    for name, value in config.get("roles", {}).items():
        if isinstance(value, dict) and str(value.get("key", "")) == key:
            return str(name)
    return None


def confidence_band(config: dict[str, Any], confidence: Decimal) -> str:
    for band in config.get("confidence_bands", []):
        try:
            low = Decimal(str(band["min"]))
            high = Decimal(str(band["max"]))
            label = str(band["label"])
        except (KeyError, TypeError, ValueError) as exc:
            raise JsonInvalidError("invalid confidence band") from exc
        if low <= confidence <= high:
            return label
    return "unbounded"


def ensure_role_table(config: dict[str, Any], role_names: dict[str, str], names: set[str]) -> dict[str, str]:
    table = dict(role_names)
    for name in sorted(names):
        key = role_key(config, name)
        existing = table.get(key)
        if existing is not None and existing != name:
            raise JsonInvalidError(f"role key collision for {key!r}: {existing!r} vs {name!r}")
        table[key] = name
    return {key: table[key] for key in sorted(table)}


def load_injections(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        return {"schema": "anomaly-injections/1", "injections": []}
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise JsonInvalidError(f"cannot load injection config: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("injections"), list):
        raise JsonInvalidError("injection config requires an injections array")
    return value
