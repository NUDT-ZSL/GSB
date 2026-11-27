"""Version-specific normalization and field-level provenance."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .generator import (
    EVENT_TYPE_TO_V1,
    EVENT_TYPE_TO_V2_CODE,
    V2_LEVEL_TO_SEVERITY,
    V3_LEVEL_TO_SEVERITY,
)
from .util import canonical_json


@dataclass(frozen=True)
class NormalizedEvent:
    values: dict[str, Any]
    provenance: dict[str, dict[str, Any]]


def _prov(version: int, source_path: str, transform: str) -> dict[str, Any]:
    return {
        "version": version,
        "source_path": source_path,
        "transform": transform,
    }


def _person_list(raw_names: str) -> list[dict[str, str]]:
    return [
        {"name": name, "role": "unknown"}
        for name in raw_names.split(";")
        if name
    ]


def _v2_person_list(raw: str) -> list[dict[str, str]]:
    values = json.loads(raw)
    result: list[dict[str, str]] = []
    for value in values:
        if isinstance(value, str):
            result.append({"name": value, "role": "unknown"})
        elif isinstance(value, dict):
            result.append(
                {
                    "name": str(value.get("name", "")),
                    "role": str(value.get("role", "unknown")),
                }
            )
    return result


def _normalize_v1(raw: dict[str, Any]) -> NormalizedEvent:
    address = tuple(str(part) for part in raw["address_line"].split("|"))
    persons = _person_list(str(raw["person_names"]))
    event_type = EVENT_TYPE_TO_V1[int(raw["alarm_type_code"])]
    values = {
        "incident_id": str(raw["event_no"]),
        "occurred_at_ms": int(raw["reported_epoch_s"]) * 1000,
        "event_type": event_type,
        "severity": int(raw["level_code"]),
        "district": address[2],
        "address_path": "/".join(address),
        "persons_json": canonical_json(persons),
        "station_id": str(raw["station_code"]),
        "vehicle_id": str(raw["vehicle_code"]),
        "source_version": 1,
    }
    provenance = {
        "incident_id": _prov(1, "event_no", "direct"),
        "occurred_at_ms": _prov(1, "reported_epoch_s", "seconds_to_milliseconds"),
        "event_type": _prov(1, "alarm_type_code", "numeric_code_map"),
        "severity": _prov(1, "level_code", "direct"),
        "district": _prov(1, "address_line[2]", "pipe_delimited_path_component"),
        "address_path": _prov(1, "address_line", "pipe_delimited_path_to_slash_path"),
        "persons_json": _prov(1, "person_names", "semicolon_list_to_person_objects"),
        "station_id": _prov(1, "station_code", "direct"),
        "vehicle_id": _prov(1, "vehicle_code", "direct"),
    }
    return NormalizedEvent(values=values, provenance=provenance)


def _normalize_v2(raw: dict[str, Any]) -> NormalizedEvent:
    address_object = json.loads(raw["address_json"])
    address = (
        str(address_object["province"]),
        str(address_object["city"]),
        str(address_object["district"]),
        str(address_object["street"]),
        str(address_object["building"]),
    )
    persons = _v2_person_list(str(raw["persons_json"]))
    values = {
        "incident_id": str(raw["call_id"]),
        "occurred_at_ms": int(raw["timestamp_ms"]),
        "event_type": EVENT_TYPE_TO_V2_CODE[str(raw["incident_kind"])],
        "severity": V2_LEVEL_TO_SEVERITY[str(raw["priority_text"])],
        "district": address[2],
        "address_path": "/".join(address),
        "persons_json": canonical_json(persons),
        "station_id": str(raw["station_id"]),
        "vehicle_id": str(raw["vehicle_id"]),
        "source_version": 2,
    }
    provenance = {
        "incident_id": _prov(2, "call_id", "direct"),
        "occurred_at_ms": _prov(2, "timestamp_ms", "direct"),
        "event_type": _prov(2, "incident_kind", "text_code_map"),
        "severity": _prov(2, "priority_text", "text_level_map"),
        "district": _prov(2, "address_json.district", "json_object_field"),
        "address_path": _prov(
            2,
            "address_json",
            "json_object_to_slash_path",
        ),
        "persons_json": _prov(2, "persons_json", "json_name_list_to_person_objects"),
        "station_id": _prov(2, "station_id", "direct"),
        "vehicle_id": _prov(2, "vehicle_id", "direct"),
    }
    return NormalizedEvent(values=values, provenance=provenance)


def _normalize_v3(raw: dict[str, Any]) -> NormalizedEvent:
    address_object = raw["location"]["address"]
    detail = address_object["detail"]
    address = (
        str(address_object["province"]),
        str(address_object["city"]),
        str(address_object["district"]),
        str(address_object["street"]),
        str(detail["building"]),
    )
    persons = [
        {
            "name": str(person.get("name", "")),
            "role": str(person.get("role", "unknown")),
        }
        for person in raw["involved"]["persons"]
    ]
    classification = raw["incident"]["classification"]
    values = {
        "incident_id": str(raw["incident"]["id"]),
        "occurred_at_ms": int(raw["incident"]["occurred_at"]["epoch_ms"]),
        "event_type": str(classification["type"]),
        "severity": V3_LEVEL_TO_SEVERITY[str(classification["level"])],
        "district": address[2],
        "address_path": "/".join(address),
        "persons_json": canonical_json(persons),
        "station_id": str(raw["responders"]["station"]["id"]),
        "vehicle_id": str(raw["responders"]["vehicle"]["id"]),
        "source_version": 3,
    }
    provenance = {
        "incident_id": _prov(3, "incident.id", "nested_json_path"),
        "occurred_at_ms": _prov(3, "incident.occurred_at.epoch_ms", "direct"),
        "event_type": _prov(3, "incident.classification.type", "nested_json_path"),
        "severity": _prov(3, "incident.classification.level", "text_level_map"),
        "district": _prov(3, "location.address.district", "nested_json_path"),
        "address_path": _prov(3, "location.address", "nested_tree_to_slash_path"),
        "persons_json": _prov(3, "involved.persons", "nested_person_objects"),
        "station_id": _prov(3, "responders.station.id", "nested_json_path"),
        "vehicle_id": _prov(3, "responders.vehicle.id", "nested_json_path"),
    }
    return NormalizedEvent(values=values, provenance=provenance)


def normalize_record(version: int, raw: dict[str, Any]) -> NormalizedEvent:
    if version == 1:
        return _normalize_v1(raw)
    if version == 2:
        return _normalize_v2(raw)
    if version == 3:
        return _normalize_v3(raw)
    raise ValueError(f"unsupported source schema version: {version}")


def provenance_rows(event: NormalizedEvent) -> list[tuple[Any, ...]]:
    incident_id = event.values["incident_id"]
    return [
        (
            incident_id,
            field,
            detail["version"],
            detail["source_path"],
            detail["transform"],
        )
        for field, detail in sorted(event.provenance.items())
    ]
