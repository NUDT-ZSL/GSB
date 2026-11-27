"""Stable constants that form the public reuse contract."""

from __future__ import annotations

SCHEMA_VERSION = 1
GENERATOR_VERSION = 1

# A candidate is eligible when it is referenced by at least two query sites.
# weight = (reference_count - 1) * estimated_cost - estimated_rows
MATERIALIZE_THRESHOLD = 400

# Deterministic budget.  Candidates are ranked by:
#   (-weight, -estimated_cost, -reference_count, fingerprint)
MAX_MATERIALIZED = 24

# Selectivity is represented in basis points so every estimate remains an
# integer and does not depend on platform floating-point behaviour.
SELECTIVITY_BP = {
    "eq": 1_500,
    "ne": 8_000,
    "lt": 4_000,
    "le": 4_000,
    "gt": 4_000,
    "ge": 4_000,
    "in_per_value": 1_500,
    "in_max": 6_000,
    "contains": 3_000,
}

EVENT_TABLE = "incidents"
VEHICLE_TABLE = "vehicles"
WATER_TABLE = "water_sources"

INCIDENT_COLUMNS = (
    "incident_id",
    "occurred_at_ms",
    "event_type",
    "severity",
    "district",
    "address_path",
    "persons_json",
    "station_id",
    "vehicle_id",
    "source_version",
)

VEHICLE_COLUMNS = (
    "vehicle_id",
    "station_id",
    "status",
    "capacity",
    "source_version",
)

WATER_COLUMNS = (
    "water_id",
    "district",
    "capacity_tons",
    "pressure",
    "address_path",
)

TABLE_COLUMNS = {
    EVENT_TABLE: INCIDENT_COLUMNS,
    VEHICLE_TABLE: VEHICLE_COLUMNS,
    WATER_TABLE: WATER_COLUMNS,
}

NORMALIZED_FIELDS = (
    "incident_id",
    "occurred_at_ms",
    "event_type",
    "severity",
    "district",
    "address_path",
    "persons_json",
    "station_id",
    "vehicle_id",
)
