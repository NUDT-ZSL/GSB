"""Fixed-seed incident, station, water, query, and update generation."""

from __future__ import annotations

import copy
import json
import random
from typing import Any

from .model import GeneratedProblem, TruthEvent

SEED_DEFAULT = 20241128
BASE_EVENT_COUNT = 72

EVENT_TYPES = ("fire", "medical", "rescue", "hazmat")
EVENT_TYPE_TO_V1 = {1: "fire", 2: "medical", 3: "rescue", 4: "hazmat"}
EVENT_TYPE_TO_V1_CODE = {value: key for key, value in EVENT_TYPE_TO_V1.items()}
EVENT_TYPE_TO_V2 = {
    "fire": "火灾",
    "medical": "医疗",
    "rescue": "救援",
    "hazmat": "危化",
}
EVENT_TYPE_TO_V2_CODE = {value: key for key, value in EVENT_TYPE_TO_V2.items()}
V3_LEVEL = {1: "L1", 2: "L2", 3: "L3", 4: "L4"}
V3_LEVEL_TO_SEVERITY = {value: key for key, value in V3_LEVEL.items()}
V2_LEVEL = {1: "一级", 2: "二级", 3: "三级", 4: "四级"}
V2_LEVEL_TO_SEVERITY = {value: key for key, value in V2_LEVEL.items()}

DISTRICTS = ("海淀区", "朝阳区", "西城区", "东城区")
STREETS = ("中关村大街", "建国路", "金融街", "安定门外大街")
BUILDINGS = ("1号楼", "8号楼", "12号楼", "23号楼")
NAMES = ("张伟", "李娜", "王强", "刘洋", "陈静", "赵磊", "周敏", "吴军")
ROLES = ("报警人", "伤员", "现场联系人", "被困人员")

T0_MS = 1_704_067_200_000  # 2024-01-01T00:00:00Z
MINUTE_MS = 60_000


def _build_event(seq: int, rng: random.Random, offset_minutes: int) -> TruthEvent:
    district_index = rng.randrange(len(DISTRICTS))
    street_index = rng.randrange(len(STREETS))
    station_index = rng.randrange(8) + 1
    event_type = EVENT_TYPES[rng.randrange(len(EVENT_TYPES))]
    severity = rng.randrange(1, 5)
    person_count = rng.randrange(0, 4)
    persons = tuple(
        {
            "name": NAMES[rng.randrange(len(NAMES))],
            "role": ROLES[rng.randrange(len(ROLES))],
        }
        for _ in range(person_count)
    )
    address = (
        "北京市",
        "北京市",
        DISTRICTS[district_index],
        STREETS[street_index],
        BUILDINGS[rng.randrange(len(BUILDINGS))],
    )
    return TruthEvent(
        incident_id=f"INC{seq:05d}",
        occurred_at_ms=T0_MS + offset_minutes * MINUTE_MS,
        event_type=event_type,
        severity=severity,
        address=address,
        persons=persons,
        station_id=f"S{station_index:02d}",
        vehicle_id=f"V{(station_index - 1) * 2 + (1 if rng.randrange(2) == 0 else 2):02d}",
    )


def _to_v1(event: TruthEvent) -> dict[str, Any]:
    return {
        "event_no": event.incident_id,
        "reported_epoch_s": event.occurred_at_ms // 1000,
        "alarm_type_code": EVENT_TYPE_TO_V1_CODE[event.event_type],
        "level_code": event.severity,
        "address_line": "|".join(event.address),
        "person_names": ";".join(person["name"] for person in event.persons),
        "station_code": event.station_id,
        "vehicle_code": event.vehicle_id,
    }


def _to_v2(event: TruthEvent) -> dict[str, Any]:
    address = {
        "province": event.address[0],
        "city": event.address[1],
        "district": event.address[2],
        "street": event.address[3],
        "building": event.address[4],
    }
    return {
        "call_id": event.incident_id,
        "timestamp_ms": event.occurred_at_ms,
        "incident_kind": EVENT_TYPE_TO_V2[event.event_type],
        "priority_text": V2_LEVEL[event.severity],
        "address_json": json.dumps(address, ensure_ascii=False, sort_keys=True),
        "persons_json": json.dumps(
            [person["name"] for person in event.persons],
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        "station_id": event.station_id,
        "vehicle_id": event.vehicle_id,
    }


def _to_v3(event: TruthEvent) -> dict[str, Any]:
    return {
        "incident": {
            "id": event.incident_id,
            "occurred_at": {"epoch_ms": event.occurred_at_ms},
            "classification": {
                "type": event.event_type,
                "level": V3_LEVEL[event.severity],
            },
        },
        "location": {
            "address": {
                "province": event.address[0],
                "city": event.address[1],
                "district": event.address[2],
                "street": event.address[3],
                "detail": {"building": event.address[4]},
            }
        },
        "involved": {
            "persons": [
                {"name": person["name"], "role": person["role"]}
                for person in event.persons
            ]
        },
        "responders": {
            "station": {"id": event.station_id},
            "vehicle": {"id": event.vehicle_id},
        },
    }


def _source_record(event: TruthEvent, version: int) -> dict[str, Any]:
    if version == 1:
        return _to_v1(event)
    if version == 2:
        return _to_v2(event)
    return _to_v3(event)


def _build_vehicles(rng: random.Random) -> list[dict[str, Any]]:
    statuses = ("available", "dispatched", "maintenance", "available")
    vehicles: list[dict[str, Any]] = []
    for number in range(1, 17):
        station = (number - 1) // 2 + 1
        vehicles.append(
            {
                "vehicle_id": f"V{number:02d}",
                "station_id": f"S{station:02d}",
                "status": statuses[(number + rng.randrange(len(statuses))) % len(statuses)],
                "capacity": 2 + (number * 3 + rng.randrange(5)) % 5,
                "source_version": 1,
            }
        )
    return vehicles


def _build_water_sources(rng: random.Random) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    number = 1
    for district_index, district in enumerate(DISTRICTS):
        for branch in range(2):
            result.append(
                {
                    "water_id": f"W{number:02d}",
                    "district": district,
                    "capacity_tons": 15 + (district_index * 11 + branch * 9 + rng.randrange(9)) % 36,
                    "pressure": round(0.30 + ((number * 7 + rng.randrange(20)) % 60) / 100, 2),
                    "address_path": f"北京市/{district}/市政水源{number}",
                }
            )
            number += 1
    return result


def _query(
    query_id: str,
    description: str,
    event_filter: dict[str, Any],
    joins: list[dict[str, Any]],
    aggregate: dict[str, Any] | None,
    project: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "query_id": query_id,
        "description": description,
        "event_filter": event_filter,
        "joins": joins,
        "aggregate": aggregate,
        "project": project,
    }


def _window(number: int) -> tuple[int, int]:
    starts = (0, 60, 120, 180, 0)
    ends = (150, 240, 330, 420, 24 * 60)
    return T0_MS + starts[number] * MINUTE_MS, T0_MS + ends[number] * MINUTE_MS


def _vehicle_join(statuses: list[str] | None = None, min_capacity: int | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"table": "vehicles"}
    filters: dict[str, Any] = {}
    if statuses is not None:
        filters["status"] = statuses
    if min_capacity is not None:
        filters["min_capacity"] = min_capacity
    value["filter"] = filters
    return value


def _water_join(min_capacity: int | None = None, min_pressure: float | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"table": "water_sources"}
    filters: dict[str, Any] = {}
    if min_capacity is not None:
        filters["min_capacity_tons"] = min_capacity
    if min_pressure is not None:
        filters["min_pressure"] = min_pressure
    value["filter"] = filters
    return value


def _station_aggregate() -> dict[str, Any]:
    return {
        "group_by": ["e.station_id"],
        "metrics": [
            {"func": "count", "field": "*", "as": "incident_count"},
            {"func": "avg", "field": "e.severity", "as": "avg_severity"},
            {"func": "max", "field": "v.capacity", "as": "max_vehicle_capacity"},
        ],
    }


def _district_aggregate() -> dict[str, Any]:
    return {
        "group_by": ["e.district"],
        "metrics": [
            {"func": "count", "field": "*", "as": "incident_count"},
            {"func": "max", "field": "w.pressure", "as": "max_pressure"},
            {"func": "sum", "field": "w.capacity_tons", "as": "water_capacity"},
        ],
    }


def _station_project() -> list[dict[str, str]]:
    return [
        {"field": "e.station_id", "as": "station_id"},
        {"field": "incident_count", "as": "incident_count"},
        {"field": "avg_severity", "as": "avg_severity"},
        {"field": "max_vehicle_capacity", "as": "max_vehicle_capacity"},
    ]


def _district_project() -> list[dict[str, str]]:
    return [
        {"field": "e.district", "as": "district"},
        {"field": "incident_count", "as": "incident_count"},
        {"field": "max_pressure", "as": "max_pressure"},
        {"field": "water_capacity", "as": "water_capacity"},
    ]


def _base_queries() -> list[dict[str, Any]]:
    w1_from, w1_to = _window(0)
    w2_from, w2_to = _window(1)
    w3_from, w3_to = _window(2)
    all_from, all_to = _window(4)
    active = ["fire", "rescue"]
    vehicle_ready = _vehicle_join(["available"], 3)
    water_25 = _water_join(25, None)

    return [
        _query(
            "Q01",
            "W1 火警/救援可用车辆按救援站汇总",
            {
                "from_ms": w1_from,
                "to_ms": w1_to,
                "event_types": active,
                "min_severity": 2,
            },
            [vehicle_ready],
            _station_aggregate(),
            _station_project(),
        ),
        _query(
            "Q02",
            "W1 火警/救援可用车辆明细",
            {
                "from_ms": w1_from,
                "to_ms": w1_to,
                "event_types": active,
                "min_severity": 2,
            },
            [vehicle_ready],
            None,
            [
                {"field": "e.incident_id", "as": "incident_id"},
                {"field": "e.occurred_at_ms", "as": "occurred_at_ms"},
                {"field": "e.event_type", "as": "event_type"},
                {"field": "e.severity", "as": "severity"},
                {"field": "e.station_id", "as": "station_id"},
                {"field": "v.vehicle_id", "as": "vehicle_id"},
                {"field": "v.status", "as": "vehicle_status"},
                {"field": "v.capacity", "as": "vehicle_capacity"},
            ],
        ),
        _query(
            "Q03",
            "W1 火警/救援与水源按行政区汇总",
            {
                "from_ms": w1_from,
                "to_ms": w1_to,
                "event_types": active,
                "min_severity": 2,
            },
            [water_25],
            _district_aggregate(),
            _district_project(),
        ),
        _query(
            "Q04",
            "W1 火警/救援按事件类型汇总车辆容量",
            {
                "from_ms": w1_from,
                "to_ms": w1_to,
                "event_types": active,
                "min_severity": 2,
            },
            [vehicle_ready],
            {
                "group_by": ["e.event_type"],
                "metrics": [
                    {"func": "count", "field": "*", "as": "incident_count"},
                    {"func": "sum", "field": "v.capacity", "as": "vehicle_capacity"},
                ],
            },
            [
                {"field": "e.event_type", "as": "event_type"},
                {"field": "incident_count", "as": "incident_count"},
                {"field": "vehicle_capacity", "as": "vehicle_capacity"},
            ],
        ),
        _query(
            "Q05",
            "W2 火警/救援可用车辆按救援站汇总",
            {
                "from_ms": w2_from,
                "to_ms": w2_to,
                "event_types": active,
                "min_severity": 2,
            },
            [vehicle_ready],
            _station_aggregate(),
            _station_project(),
        ),
        _query(
            "Q06",
            "W2 火警/救援与水源按行政区汇总",
            {
                "from_ms": w2_from,
                "to_ms": w2_to,
                "event_types": active,
                "min_severity": 2,
            },
            [water_25],
            _district_aggregate(),
            _district_project(),
        ),
        _query(
            "Q07",
            "W1 火警/救援水源明细",
            {
                "from_ms": w1_from,
                "to_ms": w1_to,
                "event_types": active,
                "min_severity": 2,
            },
            [_water_join(15, 0.4)],
            None,
            [
                {"field": "e.incident_id", "as": "incident_id"},
                {"field": "e.occurred_at_ms", "as": "occurred_at_ms"},
                {"field": "e.district", "as": "district"},
                {"field": "w.water_id", "as": "water_id"},
                {"field": "w.capacity_tons", "as": "water_capacity_tons"},
                {"field": "w.pressure", "as": "pressure"},
            ],
        ),
        _query(
            "Q08",
            "W1 医疗/危化已派车辆按救援站汇总",
            {
                "from_ms": w1_from,
                "to_ms": w1_to,
                "event_types": ["medical", "hazmat"],
                "min_severity": 1,
            },
            [_vehicle_join(["dispatched"], None)],
            {
                "group_by": ["e.station_id"],
                "metrics": [
                    {"func": "count", "field": "*", "as": "incident_count"},
                    {"func": "avg", "field": "e.severity", "as": "avg_severity"},
                ],
            },
            [
                {"field": "e.station_id", "as": "station_id"},
                {"field": "incident_count", "as": "incident_count"},
                {"field": "avg_severity", "as": "avg_severity"},
            ],
        ),
        _query(
            "Q09",
            "W1 火警/救援可用车辆按行政区与类型汇总",
            {
                "from_ms": w1_from,
                "to_ms": w1_to,
                "event_types": active,
                "min_severity": 2,
            },
            [vehicle_ready],
            {
                "group_by": ["e.district", "e.event_type"],
                "metrics": [
                    {"func": "count", "field": "*", "as": "incident_count"},
                    {"func": "avg", "field": "v.capacity", "as": "avg_vehicle_capacity"},
                ],
            },
            [
                {"field": "e.district", "as": "district"},
                {"field": "e.event_type", "as": "event_type"},
                {"field": "incident_count", "as": "incident_count"},
                {"field": "avg_vehicle_capacity", "as": "avg_vehicle_capacity"},
            ],
        ),
        _query(
            "Q10",
            "W2 车辆与水源联合调度汇总",
            {
                "from_ms": w2_from,
                "to_ms": w2_to,
                "event_types": active,
                "min_severity": 2,
            },
            [vehicle_ready, water_25],
            {
                "group_by": ["e.district", "e.station_id"],
                "metrics": [
                    {"func": "count", "field": "*", "as": "incident_count"},
                    {"func": "max", "field": "w.pressure", "as": "max_pressure"},
                ],
            },
            [
                {"field": "e.district", "as": "district"},
                {"field": "e.station_id", "as": "station_id"},
                {"field": "incident_count", "as": "incident_count"},
                {"field": "max_pressure", "as": "max_pressure"},
            ],
        ),
        _query(
            "Q11",
            "W3 仅二版接入车辆状态",
            {
                "from_ms": w3_from,
                "to_ms": w3_to,
                "event_types": active,
                "min_severity": 2,
                "source_versions": [2],
            },
            [vehicle_ready],
            _station_aggregate(),
            _station_project(),
        ),
        _query(
            "Q12",
            "全日仅一版接入与水源汇总",
            {
                "from_ms": all_from,
                "to_ms": all_to,
                "event_types": list(EVENT_TYPES),
                "min_severity": 1,
                "source_versions": [1],
            },
            [water_25],
            _district_aggregate(),
            _district_project(),
        ),
    ]


def _edited_q03(base: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    w2_from, w2_to = _window(1)
    result["description"] = "W2 火警/救援高等级事件与低压水源汇总"
    result["event_filter"] = {
        "from_ms": w2_from,
        "to_ms": w2_to,
        "event_types": ["fire", "rescue"],
        "min_severity": 3,
    }
    result["joins"] = [_water_join(20, 0.5)]
    return result


def _edited_q05(base: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    result["description"] = "W2 火警/救援可用车辆按站汇总（仅二版）"
    result["event_filter"]["source_versions"] = [2]
    return result


def generate_problem(seed: int = SEED_DEFAULT) -> GeneratedProblem:
    """Generate a deterministic problem instance.

    The first 72 truth events form the initial database.  Three backfill events
    are produced for version 1 and three for version 3 as later update batches.
    """

    rng = random.Random(seed)
    truth_events: list[TruthEvent] = []
    for index in range(BASE_EVENT_COUNT):
        offset = index * 5
        truth_events.append(_build_event(index + 1, rng, offset))

    # Backfilled batches intentionally land inside the W1/W2 windows.
    append_specs = ((1, 10), (1, 37), (1, 71), (3, 18), (3, 82), (3, 133))
    for append_index, (version, offset) in enumerate(append_specs):
        truth_events.append(
            _build_event(BASE_EVENT_COUNT + append_index + 1, rng, offset)
        )

    base_events = truth_events[:BASE_EVENT_COUNT]
    append_events = truth_events[BASE_EVENT_COUNT:]
    base_records: dict[int, list[dict[str, Any]]] = {1: [], 2: [], 3: []}
    for event in base_events:
        source_version = ((int(event.incident_id[3:]) - 1) % 3) + 1
        base_records[source_version].append(_source_record(event, source_version))

    append_batches: dict[int, list[dict[str, Any]]] = {1: [], 3: []}
    for event, (version, _offset) in zip(append_events, append_specs):
        append_batches[version].append(_source_record(event, version))

    queries = _base_queries()
    initial_by_id = {query["query_id"]: query for query in queries}
    updates: list[dict[str, Any]] = [
        {
            "kind": "append_events",
            "source_version": 1,
            "events": append_batches[1][:3],
        },
        {
            "kind": "edit_query",
            "query": _edited_q03(initial_by_id["Q03"]),
        },
        {
            "kind": "append_events",
            "source_version": 3,
            "events": append_batches[3],
        },
        {
            "kind": "edit_query",
            "query": _edited_q05(initial_by_id["Q05"]),
        },
    ]

    return GeneratedProblem(
        seed=seed,
        base_records=base_records,
        vehicles=_build_vehicles(rng),
        water_sources=_build_water_sources(rng),
        queries=queries,
        updates=updates,
        truth_events=truth_events,
    )
