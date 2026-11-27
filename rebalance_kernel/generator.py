"""Fixed-seed generator for three schema versions and deterministic streams."""

from __future__ import annotations

import json
from pathlib import Path
import random
from typing import Any

from .model import partition_for_key


TOPICS = {
    "deal_confirmation": 4,
    "contract_signing": 3,
    "fund_supervision": 3,
    "transfer_appointment": 2,
}

MEMBERS = {
    "consumer-a": {
        "rate": 60.0,
        "timeout_ms": 9000,
        "subscriptions": [
            "deal_confirmation",
            "contract_signing",
            "fund_supervision",
            "transfer_appointment",
        ],
    },
    "consumer-b": {
        "rate": 45.0,
        "timeout_ms": 7500,
        "subscriptions": [
            "deal_confirmation",
            "contract_signing",
            "fund_supervision",
        ],
    },
    "consumer-c": {
        "rate": 30.0,
        "timeout_ms": 6000,
        "subscriptions": [
            "contract_signing",
            "fund_supervision",
            "transfer_appointment",
        ],
    },
    "consumer-d": {
        "rate": 20.0,
        "timeout_ms": 12000,
        "subscriptions": [
            "deal_confirmation",
            "fund_supervision",
            "transfer_appointment",
        ],
    },
    "consumer-e": {
        "rate": 15.0,
        "timeout_ms": 5000,
        "subscriptions": [
            "deal_confirmation",
            "transfer_appointment",
        ],
    },
}

GROUP_ID = "housing-transaction-ops"

AVAILABILITY_TIMES_MS = [
    0,
    900,
    1900,
    2900,
    5900,
    7400,
    8900,
    10800,
    11800,
    13800,
    14800,
    16800,
    17800,
    18800,
    20800,
    22800,
    23800,
]


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def serialize_v1() -> dict[str, Any]:
    partitions = []
    for topic in sorted(TOPICS):
        for partition in range(TOPICS[topic]):
            partitions.append(
                {
                    "topic": topic,
                    "partition": partition,
                    "start_offset": 0,
                }
            )
    return {
        "schema": "v1",
        "consumer_group": GROUP_ID,
        "members": [
            {
                "member_id": member_id,
                "processing_rate": int(spec["rate"]),
                "session_timeout_ms": spec["timeout_ms"],
                "subscriptions": list(spec["subscriptions"]),
            }
            for member_id, spec in sorted(MEMBERS.items())
        ],
        "partitions": partitions,
    }


def serialize_v2() -> dict[str, Any]:
    return {
        "schema": "v2",
        "group": {"id": GROUP_ID},
        "members": [
            {
                "id": member_id,
                "rate_per_second": f"{spec['rate']:.2f}",
                "lease": {
                    "session_timeout_sec": spec["timeout_ms"] / 1000.0,
                },
                "topics": list(spec["subscriptions"]),
            }
            for member_id, spec in sorted(MEMBERS.items())
        ],
        "partition_tree": {
            topic: [
                {
                    "number": partition + 1,
                    "start_offset": 0,
                }
                for partition in range(TOPICS[topic])
            ]
            for topic in sorted(TOPICS)
        },
    }


def serialize_v3() -> dict[str, Any]:
    members = []
    for index, (member_id, spec) in enumerate(sorted(MEMBERS.items())):
        if index % 2:
            timeout = {
                "value": spec["timeout_ms"] / 1000.0,
                "unit": "s",
            }
        else:
            timeout = {
                "value": spec["timeout_ms"],
                "unit": "ms",
            }
        members.append(
            {
                "consumer_id": member_id,
                "handling_rate": float(spec["rate"]),
                "heartbeat": {"session_timeout": timeout},
                "subscribe": list(spec["subscriptions"]),
            }
        )
    return {
        "schema": "v3",
        "group_id": GROUP_ID,
        "consumer_specs": members,
        "topic_partitions": {
            topic: list(range(TOPICS[topic]))
            for topic in sorted(TOPICS)
        },
    }


def member_events() -> list[dict[str, Any]]:
    definitions = [
        (0, "join", "consumer-a", None),
        (0, "join", "consumer-b", None),
        (0, "join", "consumer-c", None),
        (2000, "rate_change", "consumer-a", 90.0),
        (2000, "heartbeat", "consumer-a", None),
        (3000, "join", "consumer-d", None),
        (6000, "heartbeat", "consumer-c", None),
        (7500, "heartbeat", "consumer-b", None),
        (9000, "rate_change", "consumer-c", 40.0),
        (11000, "leave", "consumer-b", None),
        (11000, "heartbeat", "consumer-a", None),
        (12001, "heartbeat", "consumer-a", None),
        (14000, "leave", "consumer-d", None),
        (14000, "join", "consumer-e", None),
        (17000, "rate_change", "consumer-e", 25.0),
        (19000, "heartbeat", "consumer-e", None),
        (23000, "heartbeat", "consumer-e", None),
        (24000, "heartbeat", "consumer-e", None),
    ]
    result = []
    for index, (at_ms, kind, member_id, rate) in enumerate(definitions):
        event = {
            "event_id": f"member-event-{index:04d}",
            "at_ms": at_ms,
            "type": kind,
            "member_id": member_id,
        }
        if rate is not None:
            event["rate"] = rate
        result.append(event)
    return result


def _keys_for_partition(
    topic: str,
    partition: int,
    partition_count: int,
    count: int,
) -> list[str]:
    prefixes = {
        "deal_confirmation": "HOUSE",
        "contract_signing": "DEAL",
        "fund_supervision": "ESCROW",
        "transfer_appointment": "APPT",
    }
    prefix = prefixes.get(topic, "KEY")
    keys = []
    candidate = 0
    while len(keys) < count:
        key = f"{prefix}-{partition:02d}-{candidate:05d}"
        if partition_for_key(topic, key, partition_count) == partition:
            keys.append(key)
        candidate += 1
        if candidate > 2_000_000:
            raise RuntimeError(f"could not find keys for {topic}:{partition}")
    return keys


def keyed_events(seed: int) -> list[dict[str, Any]]:
    event_types = {
        "deal_confirmation": ["deal_confirmed", "deal_cancelled"],
        "contract_signing": ["contract_signed", "contract_revoked"],
        "fund_supervision": ["fund_deposited", "fund_released"],
        "transfer_appointment": [
            "appointment_created",
            "appointment_rescheduled",
        ],
    }
    result: list[dict[str, Any]] = []

    for topic in sorted(TOPICS):
        partition_count = TOPICS[topic]
        for partition in range(partition_count):
            if topic == "transfer_appointment":
                if partition == 0:
                    times = [
                        value
                        for value in AVAILABILITY_TIMES_MS
                        for _ in range(2)
                    ]
                else:
                    times = [0, 5900]
            else:
                times = list(AVAILABILITY_TIMES_MS)

            rng = random.Random(f"{seed}:{topic}:{partition}")
            key_pool = _keys_for_partition(
                topic,
                partition,
                partition_count,
                max(8, len(times)),
            )
            pending = []
            for index, available_at in enumerate(times):
                pending.append(
                    {
                        "available_at_ms": available_at,
                        "index": index,
                        "partition_key": key_pool[index % len(key_pool)],
                        "event_type": rng.choice(event_types[topic]),
                        "payload": {
                            "house_id": key_pool[index % len(key_pool)],
                            "case_id": f"CASE-{topic[:3].upper()}-{partition:02d}-"
                            f"{index:04d}",
                            "amount_cent": rng.randrange(100_000, 9_000_000),
                        },
                    }
                )
            pending.sort(
                key=lambda item: (
                    item["available_at_ms"],
                    item["index"],
                    item["partition_key"],
                )
            )
            for offset, item in enumerate(pending):
                result.append(
                    {
                        "event_id": (
                            f"evt-{topic[:4]}-{partition:02d}-{offset:04d}"
                        ),
                        "topic": topic,
                        "partition": partition,
                        "offset": offset,
                        "partition_key": item["partition_key"],
                        "available_at_ms": item["available_at_ms"],
                        "event_type": item["event_type"],
                        "payload": item["payload"],
                    }
                )
    result.sort(key=lambda item: (item["topic"], item["partition"], item["offset"]))
    return result


def _minimal_v1(
    group_id: str,
    members: dict[str, dict[str, Any]],
    topics: dict[str, int],
) -> dict[str, Any]:
    return {
        "schema": "v1",
        "consumer_group": group_id,
        "members": [
            {
                "member_id": member_id,
                "processing_rate": spec["rate"],
                "session_timeout_ms": spec["timeout_ms"],
                "subscriptions": list(spec["subscriptions"]),
            }
            for member_id, spec in sorted(members.items())
        ],
        "partitions": [
            {"topic": topic, "partition": partition, "start_offset": 0}
            for topic in sorted(topics)
            for partition in range(topics[topic])
        ],
    }


def _minimal_events(
    prefix: str,
    topics: dict[str, int],
    keys: dict[tuple[str, int], list[str]],
) -> list[dict[str, Any]]:
    result = []
    counters: dict[tuple[str, int], int] = {}
    for topic in sorted(topics):
        for partition in range(topics[topic]):
            partition_keys = keys.get((topic, partition))
            if partition_keys is None:
                partition_keys = _keys_for_partition(
                    topic, partition, topics[topic], 1
                )
            for key in partition_keys:
                offset = counters.get((topic, partition), 0)
                counters[(topic, partition)] = offset + 1
                result.append(
                    {
                        "event_id": f"{prefix}-{topic}-{partition}-{offset}",
                        "topic": topic,
                        "partition": partition,
                        "offset": offset,
                        "partition_key": key,
                        "available_at_ms": 0,
                        "event_type": "edge_event",
                        "payload": {},
                    }
                )
    return result


def edge_cases() -> list[dict[str, Any]]:
    cases = []

    topics = {"appointment_topic": 1}
    cases.append(
        {
            "name": "more_members_than_partitions",
            "description": "1 个分区、3 个存活成员",
            "project": _minimal_v1(
                "edge-more-members",
                {
                    "edge-a": {
                        "rate": 10,
                        "timeout_ms": 5000,
                        "subscriptions": ["appointment_topic"],
                    },
                    "edge-b": {
                        "rate": 20,
                        "timeout_ms": 5000,
                        "subscriptions": ["appointment_topic"],
                    },
                    "edge-c": {
                        "rate": 30,
                        "timeout_ms": 5000,
                        "subscriptions": ["appointment_topic"],
                    },
                },
                topics,
            ),
            "member_events": [
                {
                    "event_id": "edge-join-a",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-a",
                },
                {
                    "event_id": "edge-join-b",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-b",
                },
                {
                    "event_id": "edge-join-c",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-c",
                },
            ],
            "keyed_events": _minimal_events(
                "edge-mm",
                topics,
                {
                    ("appointment_topic", 0): _keys_for_partition(
                        "appointment_topic", 0, 1, 3
                    )
                },
            ),
        }
    )

    topics = {"signing_topic": 3}
    keys = {
        ("signing_topic", partition): _keys_for_partition(
            "signing_topic", partition, 3, 3
        )
        for partition in range(3)
    }
    cases.append(
        {
            "name": "simultaneous_join_leave",
            "description": "同一逻辑时刻 edge-b/edge-c 退出、edge-d/edge-e 加入",
            "project": _minimal_v1(
                "edge-simultaneous",
                {
                    "edge-a": {
                        "rate": 10,
                        "timeout_ms": 10000,
                        "subscriptions": ["signing_topic"],
                    },
                    "edge-b": {
                        "rate": 20,
                        "timeout_ms": 10000,
                        "subscriptions": ["signing_topic"],
                    },
                    "edge-c": {
                        "rate": 30,
                        "timeout_ms": 10000,
                        "subscriptions": ["signing_topic"],
                    },
                    "edge-d": {
                        "rate": 25,
                        "timeout_ms": 10000,
                        "subscriptions": ["signing_topic"],
                    },
                    "edge-e": {
                        "rate": 15,
                        "timeout_ms": 10000,
                        "subscriptions": ["signing_topic"],
                    },
                },
                topics,
            ),
            "member_events": [
                {
                    "event_id": "edge-join-a",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-a",
                },
                {
                    "event_id": "edge-join-b",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-b",
                },
                {
                    "event_id": "edge-leave-b",
                    "at_ms": 1000,
                    "type": "leave",
                    "member_id": "edge-b",
                },
                {
                    "event_id": "edge-leave-c",
                    "at_ms": 1000,
                    "type": "leave",
                    "member_id": "edge-c",
                },
                {
                    "event_id": "edge-join-d",
                    "at_ms": 1000,
                    "type": "join",
                    "member_id": "edge-d",
                },
                {
                    "event_id": "edge-join-e",
                    "at_ms": 1000,
                    "type": "join",
                    "member_id": "edge-e",
                },
            ],
            "keyed_events": _minimal_events("edge-sim", topics, keys),
        }
    )

    topics = {"contract_topic": 2}
    keys = {
        ("contract_topic", partition): _keys_for_partition(
            "contract_topic", partition, 2, 2
        )
        for partition in range(2)
    }
    cases.append(
        {
            "name": "timeout_boundary",
            "description": (
                "edge-a 在恰好 5000ms 时心跳仍存活；10001ms 时超过边界失效"
            ),
            "project": _minimal_v1(
                "edge-timeout",
                {
                    "edge-a": {
                        "rate": 10,
                        "timeout_ms": 5000,
                        "subscriptions": ["contract_topic"],
                    },
                    "edge-b": {
                        "rate": 10,
                        "timeout_ms": 100000,
                        "subscriptions": ["contract_topic"],
                    },
                },
                topics,
            ),
            "member_events": [
                {
                    "event_id": "edge-join-a",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-a",
                },
                {
                    "event_id": "edge-join-b",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-b",
                },
                {
                    "event_id": "edge-boundary-heartbeat-a",
                    "at_ms": 5000,
                    "type": "heartbeat",
                    "member_id": "edge-a",
                },
                {
                    "event_id": "edge-check-b-5001",
                    "at_ms": 5001,
                    "type": "heartbeat",
                    "member_id": "edge-b",
                },
                {
                    "event_id": "edge-check-b-10001",
                    "at_ms": 10001,
                    "type": "heartbeat",
                    "member_id": "edge-b",
                },
            ],
            "keyed_events": _minimal_events("edge-to", topics, keys),
        }
    )

    topics = {"concentrated_topic": 4}
    concentrated_keys = {
        ("concentrated_topic", 0): _keys_for_partition(
            "concentrated_topic", 0, 4, 60
        )
    }
    cases.append(
        {
            "name": "high_key_concentration",
            "description": "60 个事件键稳定落在同一分区，另 3 个分区各 1 个键",
            "project": _minimal_v1(
                "edge-concentrated",
                {
                    "edge-a": {
                        "rate": 10,
                        "timeout_ms": 10000,
                        "subscriptions": ["concentrated_topic"],
                    },
                    "edge-b": {
                        "rate": 20,
                        "timeout_ms": 10000,
                        "subscriptions": ["concentrated_topic"],
                    },
                    "edge-c": {
                        "rate": 30,
                        "timeout_ms": 10000,
                        "subscriptions": ["concentrated_topic"],
                    },
                },
                topics,
            ),
            "member_events": [
                {
                    "event_id": "edge-join-a",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-a",
                },
                {
                    "event_id": "edge-join-b",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-b",
                },
                {
                    "event_id": "edge-join-c",
                    "at_ms": 0,
                    "type": "join",
                    "member_id": "edge-c",
                },
            ],
            "keyed_events": _minimal_events(
                "edge-hc", topics, concentrated_keys
            ),
        }
    )
    return cases


def generate_bundle(seed: int, output_dir: str | Path) -> dict[str, Any]:
    target = Path(output_dir)
    target.mkdir(parents=True, exist_ok=True)
    files = {
        "project_v1": target / "project_v1.json",
        "project_v2": target / "project_v2.json",
        "project_v3": target / "project_v3.json",
        "member_events": target / "member_events.json",
        "keyed_events": target / "keyed_events.json",
        "edge_cases": target / "edge_cases.json",
    }
    _write_json(files["project_v1"], serialize_v1())
    _write_json(files["project_v2"], serialize_v2())
    _write_json(files["project_v3"], serialize_v3())
    _write_json(files["member_events"], member_events())
    _write_json(files["keyed_events"], keyed_events(seed))
    _write_json(files["edge_cases"], edge_cases())
    manifest = {
        "seed": seed,
        "group_id": GROUP_ID,
        "schema_versions": ["v1", "v2", "v3"],
        "files": {
            name: str(path.relative_to(target))
            for name, path in sorted(files.items())
        },
        "event_count": len(keyed_events(seed)),
        "member_event_count": len(member_events()),
        "edge_case_count": len(edge_cases()),
    }
    _write_json(target / "manifest.json", manifest)
    return {
        **files,
        "manifest": target / "manifest.json",
        "root": target,
    }


def load_generated(paths: dict[str, Path]) -> dict[str, Any]:
    def read(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    return {
        "project_v1": read(paths["project_v1"]),
        "project_v2": read(paths["project_v2"]),
        "project_v3": read(paths["project_v3"]),
        "member_events": read(paths["member_events"]),
        "keyed_events": read(paths["keyed_events"]),
        "edge_cases": read(paths["edge_cases"]),
        "manifest": read(paths["manifest"]),
    }
