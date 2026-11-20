"""Deterministic v1-to-v2 semantic migration."""

from __future__ import annotations

import copy
from typing import Any

from .canonical import digest_value
from .validation import validate_parameter_value


def migrate_v1_entries(
    parsed: dict[str, Any],
    *,
    clock_ms: int,
) -> dict[str, Any]:
    """Normalize v1 entries to v2 semantics without changing their validity."""
    output = copy.deepcopy(parsed)
    compatibility_defaults = output["v2"].get("compatibility_defaults", {})
    schema = output["v2"]["parameter_schema"]
    migrations: list[dict[str, Any]] = []
    for entry in output["entries"]:
        if entry["source_version"] != "v1":
            entry["expired_at_clock"] = (
                clock_ms > entry["effective_at_ms"] + entry["ttl_ms"]
            )
            continue
        if entry.get("migration") is not None:
            migrations.append(entry["migration"])
            continue
        defaults_applied: list[dict[str, Any]] = []
        for parameter in sorted(compatibility_defaults):
            if parameter in entry["params"]:
                continue
            value = copy.deepcopy(compatibility_defaults[parameter])
            validate_parameter_value(
                parameter,
                value,
                schema[parameter],
                stage="migrate",
                location={
                    "environment": entry["environment"],
                    "service": entry["service"],
                },
            )
            entry["params"][parameter] = value
            entry["compatibility_defaults"].append(parameter)
            defaults_applied.append(
                {
                    "parameter": parameter,
                    "default": value,
                    "reason": "v2_field_absent_in_v1",
                    "mapping": "declared_default",
                }
            )
        expires_at_ms = entry["effective_at_ms"] + entry["ttl_ms"]
        expired = clock_ms > expires_at_ms
        entry["expired_at_clock"] = expired
        migration = {
            "migration_id": digest_value(
                {
                    "candidate_id": entry["candidate_id"],
                    "algorithm": "v1-to-v2/1",
                }
            )[:24],
            "algorithm": "v1-to-v2/1",
            "environment": entry["environment"],
            "service": entry["service"],
            "source_file": entry["source_file"],
            "time": {
                "from": entry["original_time"],
                "to": {
                    "kind": "absolute_ms",
                    "value": entry["effective_at_ms"],
                },
                "clock_basis": "injected_clock_ms",
            },
            "generation": {
                "from": None,
                "to": 0,
                "mapping": "v1_missing_generation_to_zero",
            },
            "upstreams": {
                "from": None,
                "to": [],
                "mapping": "v1_missing_upstreams_to_empty",
            },
            "compatibility_defaults": defaults_applied,
            "expiry": {
                "effective_at_ms": entry["effective_at_ms"],
                "ttl_ms": entry["ttl_ms"],
                "expires_at_ms": expires_at_ms,
                "expired_before_at_injected_clock": expired,
                "expired_after_at_injected_clock": expired,
                "preserved": True,
                "clock_basis": "injected_clock_ms",
            },
            "idempotent": True,
        }
        entry["migration"] = migration
        migrations.append(migration)
    output["migrations"] = sorted(
        {item["migration_id"]: item for item in migrations}.values(),
        key=lambda item: (
            item["environment"],
            item["service"],
            item["migration_id"],
        ),
    )
    output["compatibility_degradations"] = sorted(
        [
            {
                "environment": entry["environment"],
                "service": entry["service"],
                "parameter": default["parameter"],
                "default_value": default["default"],
                "source_version": "v1",
                "reason": default["reason"],
                "mapping": default["mapping"],
            }
            for entry in output["entries"]
            for default in entry.get("migration", {}).get(
                "compatibility_defaults", []
            )
        ],
        key=lambda item: (
            item["environment"],
            item["service"],
            item["parameter"],
        ),
    )
    return output
