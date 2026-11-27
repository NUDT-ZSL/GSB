"""Deterministic, dependency-free sample generator for all schema versions."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from .model import canonical_json


PROJECT_ID = "greenhouse-nat-domain"
DOMAIN_ID = "greenhouse-control-domain"
HORIZON = 16


CONNECTIONS = [
    {
        "connection_id": "c1",
        "greenhouse_id": "gh-a",
        "direction": "inbound",
        "tracking_timeout": 50,
        "five_tuple": {
            "src_ip": "198.51.100.20",
            "src_port": 41000,
            "dst_ip": "10.20.1.10",
            "dst_port": 8000,
            "protocol": "tcp",
        },
    },
    {
        "connection_id": "c2",
        "greenhouse_id": "gh-b",
        "direction": "outbound",
        "tracking_timeout": 4,
        "five_tuple": {
            "src_ip": "10.20.2.40",
            "src_port": 12000,
            "dst_ip": "198.51.100.30",
            "dst_port": 9100,
            "protocol": "tcp",
        },
    },
    {
        "connection_id": "c3",
        "greenhouse_id": "gh-a",
        "direction": "inbound",
        "tracking_timeout": 60,
        "five_tuple": {
            "src_ip": "198.51.100.21",
            "src_port": 41001,
            "dst_ip": "10.20.1.11",
            "dst_port": 8001,
            "protocol": "tcp",
        },
    },
    {
        "connection_id": "c4",
        "greenhouse_id": "gh-b",
        "direction": "inbound",
        "tracking_timeout": 80,
        "five_tuple": {
            "src_ip": "198.51.100.25",
            "src_port": 42000,
            "dst_ip": "10.20.2.101",
            "dst_port": 9001,
            "protocol": "tcp",
        },
    },
    {
        "connection_id": "c5",
        "greenhouse_id": "gh-a",
        "direction": "inbound",
        "tracking_timeout": 7,
        "five_tuple": {
            "src_ip": "198.51.100.22",
            "src_port": 41002,
            "dst_ip": "10.20.1.12",
            "dst_port": 8002,
            "protocol": "tcp",
        },
    },
    {
        "connection_id": "c6",
        "greenhouse_id": "gh-a",
        "direction": "inbound",
        "tracking_timeout": 7,
        "five_tuple": {
            "src_ip": "198.51.100.23",
            "src_port": 41003,
            "dst_ip": "10.20.1.13",
            "dst_port": 8003,
            "protocol": "tcp",
        },
    },
    {
        "connection_id": "c7",
        "greenhouse_id": "gh-a",
        "direction": "inbound",
        "tracking_timeout": 90,
        "five_tuple": {
            "src_ip": "198.51.100.24",
            "src_port": 41004,
            "dst_ip": "10.20.1.14",
            "dst_port": 8004,
            "protocol": "tcp",
        },
    },
]

RULES = [
    {
        "rule_id": "r-in-a",
        "greenhouse_id": "gh-a",
        "priority": [10],
        "protocol": "tcp",
        "direction": "inbound",
        "src_cidr": "198.51.100.0/24",
        "dst_port_start": 8000,
        "dst_port_end": 8100,
        "src_port_start": None,
        "src_port_end": None,
        "pool_id": "p-a",
    },
    {
        "rule_id": "r-out-b",
        "greenhouse_id": "gh-b",
        "priority": [20],
        "protocol": "tcp",
        "direction": "outbound",
        "src_cidr": "10.20.2.0/24",
        "dst_port_start": 9100,
        "dst_port_end": 9101,
        "src_port_start": None,
        "src_port_end": None,
        "pool_id": "p-b",
    },
    {
        "rule_id": "r-conflict-high",
        "greenhouse_id": "gh-b",
        "priority": [10],
        "protocol": "tcp",
        "direction": "inbound",
        "src_cidr": "198.51.100.0/24",
        "dst_port_start": 9001,
        "dst_port_end": 9002,
        "src_port_start": None,
        "src_port_end": None,
        "pool_id": "p-c",
    },
    {
        "rule_id": "r-conflict-low",
        "greenhouse_id": "gh-b",
        "priority": [20],
        "protocol": "tcp",
        "direction": "inbound",
        "src_cidr": "198.51.100.0/24",
        "dst_port_start": 9001,
        "dst_port_end": 9002,
        "src_port_start": None,
        "src_port_end": None,
        "pool_id": "p-d",
    },
]

POOLS = [
    {
        "pool_id": "p-a",
        "greenhouse_id": "gh-a",
        "external_ip": "203.0.113.10",
        "port_start": 50000,
        "port_end": 50009,
    },
    {
        "pool_id": "p-b",
        "greenhouse_id": "gh-b",
        "external_ip": "203.0.113.11",
        "port_start": 50010,
        "port_end": 50012,
    },
    {
        "pool_id": "p-c",
        "greenhouse_id": "gh-b",
        "external_ip": "203.0.113.12",
        "port_start": 50020,
        "port_end": 50022,
    },
    {
        "pool_id": "p-d",
        "greenhouse_id": "gh-b",
        "external_ip": "203.0.113.13",
        "port_start": 50030,
        "port_end": 50031,
    },
]

EVENTS: list[dict[str, Any]] = [
    {"event_id": "e-c3-new-1", "time": 0, "seq": 1, "type": "new", "connection_id": "c3"},
    {"event_id": "e-c1-new-1", "time": 0, "seq": 2, "type": "new", "connection_id": "c1"},
    {"event_id": "e-c4-new-1", "time": 0, "seq": 3, "type": "new", "connection_id": "c4"},
    {"event_id": "e-c5-new-1", "time": 0, "seq": 4, "type": "new", "connection_id": "c5"},
    {"event_id": "e-c6-new-1", "time": 0, "seq": 5, "type": "new", "connection_id": "c6"},
    {"event_id": "e-c7-new-1", "time": 0, "seq": 6, "type": "new", "connection_id": "c7"},
    {"event_id": "e-c2-new-1", "time": 0, "seq": 7, "type": "new", "connection_id": "c2"},
    {"event_id": "e-c1-est-1", "time": 1, "seq": 1, "type": "established", "connection_id": "c1"},
    {"event_id": "e-c4-est-1", "time": 1, "seq": 2, "type": "established", "connection_id": "c4"},
    {"event_id": "e-c5-est-1", "time": 1, "seq": 3, "type": "established", "connection_id": "c5"},
    {"event_id": "e-c6-est-1", "time": 1, "seq": 4, "type": "established", "connection_id": "c6"},
    {"event_id": "e-c7-est-1", "time": 1, "seq": 5, "type": "established", "connection_id": "c7"},
    {"event_id": "e-c2-est-1", "time": 2, "seq": 1, "type": "established", "connection_id": "c2"},
    {"event_id": "e-c3-est-1", "time": 2, "seq": 2, "type": "established", "connection_id": "c3"},
    {"event_id": "e-c3-down-1", "time": 4, "seq": 1, "type": "teardown", "connection_id": "c3"},
    {"event_id": "e-c3-new-2", "time": 4, "seq": 2, "type": "new", "connection_id": "c3"},
    {"event_id": "e-c3-est-2", "time": 5, "seq": 1, "type": "established", "connection_id": "c3"},
    {"event_id": "e-c1-down-1", "time": 6, "seq": 1, "type": "teardown", "connection_id": "c1"},
    {
        "event_id": "e-prio-conflict",
        "time": 7,
        "seq": 1,
        "type": "policy_change",
        "operation": "set_rule_priority",
        "rule_id": "r-conflict-high",
        "priority": [30],
    },
    {"event_id": "e-c6-down-1", "time": 8, "seq": 1, "type": "teardown", "connection_id": "c6"},
    {"event_id": "e-tick-8", "time": 8, "seq": 2, "type": "tick"},
    {"event_id": "e-c5-new-2", "time": 9, "seq": 1, "type": "new", "connection_id": "c5"},
    {"event_id": "e-c5-est-2", "time": 10, "seq": 1, "type": "established", "connection_id": "c5"},
    {
        "event_id": "e-pool-expand",
        "time": 10,
        "seq": 2,
        "type": "policy_change",
        "operation": "upsert_pool",
        "pool_id": "p-a",
        "pool_object": {
            "pool_id": "p-a",
            "greenhouse_id": "gh-a",
            "external_ip": "203.0.113.10",
            "port_start": 50000,
            "port_end": 50014,
        },
    },
    {
        "event_id": "e-pool-shrink",
        "time": 15,
        "seq": 1,
        "type": "policy_change",
        "operation": "upsert_pool",
        "pool_id": "p-a",
        "pool_object": {
            "pool_id": "p-a",
            "greenhouse_id": "gh-a",
            "external_ip": "203.0.113.10",
            "port_start": 50002,
            "port_end": 50004,
        },
    },
]


def canonical_spec() -> dict[str, Any]:
    return {
        "project_id": PROJECT_ID,
        "domain_id": DOMAIN_ID,
        "horizon": HORIZON,
        "connections": copy.deepcopy(CONNECTIONS),
        "rules": copy.deepcopy(RULES),
        "pools": copy.deepcopy(POOLS),
        "events": copy.deepcopy(EVENTS),
    }


def _hierarchical(priority: int) -> str:
    return f"tier.{priority}.0"


def _zone_priority(priority: int) -> str:
    return f"zone.edge.{priority}"


def _offset_port(port: int, quantum: int = 1000) -> dict[str, int]:
    base = (port // quantum) * quantum
    return {"base": base, "offset": port - base}


def _v1_event(event: dict[str, Any]) -> dict[str, Any]:
    result = {
        "event_id": event["event_id"],
        "time": event["time"],
        "seq": event["seq"],
        "type": event["type"],
    }
    if "connection_id" in event:
        result["connection_id"] = event["connection_id"]
    if event["type"] == "policy_change":
        result["operation"] = event["operation"]
        if event["operation"] == "set_rule_priority":
            result["rule_id"] = event["rule_id"]
            result["priority"] = event["priority"][0]
        elif event["operation"] == "upsert_pool":
            result["pool_id"] = event["pool_id"]
            pool = copy.deepcopy(event["pool_object"])
            result["pool"] = {
                "pool_id": pool["pool_id"],
                "greenhouse_id": pool["greenhouse_id"],
                "external_ip": pool["external_ip"],
                "port_start": pool["port_start"],
                "port_end": pool["port_end"],
            }
    return result


def _v2_event(event: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": event["event_id"],
        "at": event["time"],
        "order": event["seq"],
        "kind": event["type"],
    }
    if "connection_id" in event:
        result["connection"] = event["connection_id"]
    if event["type"] == "policy_change":
        result["operation"] = event["operation"]
        if event["operation"] == "set_rule_priority":
            result["operation"] = "set_priority"
            result["rule"] = event["rule_id"]
            result["rank"] = _hierarchical(event["priority"][0])
        elif event["operation"] == "upsert_pool":
            result["pool"] = event["pool_id"]
            pool = event["pool_object"]
            result["pool_object"] = {
                "name": pool["pool_id"],
                "site": pool["greenhouse_id"],
                "address": pool["external_ip"],
                "port_window": {
                    "from": pool["port_start"],
                    "to": pool["port_end"],
                },
            }
    return result


def _v3_event(event: dict[str, Any]) -> dict[str, Any]:
    result = {
        "id": event["event_id"],
        "at": event["time"],
        "order": event["seq"],
        "action": event["type"],
    }
    if "connection_id" in event:
        result["connection"] = event["connection_id"]
    if event["type"] == "policy_change":
        result["operation"] = event["operation"]
        if event["operation"] == "set_rule_priority":
            result["operation"] = "priority"
            result["rule"] = event["rule_id"]
            result["priority"] = _zone_priority(event["priority"][0])
        elif event["operation"] == "upsert_pool":
            result["pool"] = event["pool_id"]
            pool = event["pool_object"]
            result["pool_object"] = {
                "id": pool["pool_id"],
                "greenhouse": pool["greenhouse_id"],
                "external_ip": pool["external_ip"],
                "port_base": 50000,
                "offset_start": pool["port_start"] - 50000,
                "offset_end": pool["port_end"] - 50000,
            }
    return result


def build_v1() -> dict[str, Any]:
    return {
        "schema_version": "v1",
        "project_id": PROJECT_ID,
        "domain_id": DOMAIN_ID,
        "horizon": HORIZON,
        "connections": [
            {
                "connection_id": conn["connection_id"],
                "greenhouse_id": conn["greenhouse_id"],
                "src_ip": conn["five_tuple"]["src_ip"],
                "src_port": conn["five_tuple"]["src_port"],
                "dst_ip": conn["five_tuple"]["dst_ip"],
                "dst_port": conn["five_tuple"]["dst_port"],
                "protocol": conn["five_tuple"]["protocol"],
                "direction": conn["direction"],
                "tracking_timeout": conn["tracking_timeout"],
            }
            for conn in CONNECTIONS
        ],
        "mapping_rules": [
            {
                "rule_id": rule["rule_id"],
                "greenhouse_id": rule["greenhouse_id"],
                "priority": rule["priority"][0],
                "protocol": rule["protocol"],
                "direction": rule["direction"],
                "source_cidr": rule["src_cidr"],
                "destination_port_start": rule["dst_port_start"],
                "destination_port_end": rule["dst_port_end"],
                "pool_id": rule["pool_id"],
            }
            for rule in RULES
        ],
        "address_pools": copy.deepcopy(POOLS),
        "timeline": [_v1_event(event) for event in EVENTS],
    }


def build_v2() -> dict[str, Any]:
    return {
        "schema": "v2",
        "project": {"id": PROJECT_ID, "domain": DOMAIN_ID},
        "horizon": HORIZON,
        "connections": [
            {
                "id": conn["connection_id"],
                "site": conn["greenhouse_id"],
                "tuple": {
                    "source": {
                        "ip": conn["five_tuple"]["src_ip"],
                        "port": conn["five_tuple"]["src_port"],
                    },
                    "destination": {
                        "ip": conn["five_tuple"]["dst_ip"],
                        "port": conn["five_tuple"]["dst_port"],
                    },
                    "transport": conn["five_tuple"]["protocol"],
                },
                "flow": conn["direction"],
                "idle_timeout": conn["tracking_timeout"],
            }
            for conn in CONNECTIONS
        ],
        "rules": [
            {
                "name": rule["rule_id"],
                "site": rule["greenhouse_id"],
                "rank": _hierarchical(rule["priority"][0]),
                "match": {
                    "protocol": rule["protocol"],
                    "direction": rule["direction"],
                    "source_cidr": rule["src_cidr"],
                    "destination_port": {
                        "from": rule["dst_port_start"],
                        "to": rule["dst_port_end"],
                    },
                },
                "route": {"pool": rule["pool_id"]},
            }
            for rule in RULES
        ],
        "pools": [
            {
                "name": pool["pool_id"],
                "site": pool["greenhouse_id"],
                "address": pool["external_ip"],
                "port_window": {
                    "from": pool["port_start"],
                    "to": pool["port_end"],
                },
            }
            for pool in POOLS
        ],
        "events": [_v2_event(event) for event in EVENTS],
    }


def build_v3() -> dict[str, Any]:
    return {
        "version": "v3",
        "meta": {"project": PROJECT_ID, "domain": DOMAIN_ID, "horizon": HORIZON},
        "conns": [
            {
                "id": conn["connection_id"],
                "greenhouse": conn["greenhouse_id"],
                "src_ip": conn["five_tuple"]["src_ip"],
                "dst_ip": conn["five_tuple"]["dst_ip"],
                "src_port": _offset_port(conn["five_tuple"]["src_port"], 1000),
                "dst_port": _offset_port(conn["five_tuple"]["dst_port"], 1000),
                "protocol": conn["five_tuple"]["protocol"],
                "direction": conn["direction"],
                "timeout": conn["tracking_timeout"],
            }
            for conn in CONNECTIONS
        ],
        "map_rules": [
            {
                "id": rule["rule_id"],
                "greenhouse": rule["greenhouse_id"],
                "priority_path": _zone_priority(rule["priority"][0]),
                "protocol": rule["protocol"],
                "direction": rule["direction"],
                "src_cidr": rule["src_cidr"],
                "dst_port_base": 8000 if rule["rule_id"] == "r-in-a" else (
                    9100 if rule["rule_id"] == "r-out-b" else 9000
                ),
                "dst_offset_start": (
                    rule["dst_port_start"]
                    - (8000 if rule["rule_id"] == "r-in-a" else (
                        9100 if rule["rule_id"] == "r-out-b" else 9000
                    ))
                ),
                "dst_offset_end": (
                    rule["dst_port_end"]
                    - (8000 if rule["rule_id"] == "r-in-a" else (
                        9100 if rule["rule_id"] == "r-out-b" else 9000
                    ))
                ),
                "pool": rule["pool_id"],
            }
            for rule in RULES
        ],
        "pools": [
            {
                "id": pool["pool_id"],
                "greenhouse": pool["greenhouse_id"],
                "external_ip": pool["external_ip"],
                "port_base": 50000,
                "offset_start": pool["port_start"] - 50000,
                "offset_end": pool["port_end"] - 50000,
            }
            for pool in POOLS
        ],
        "log": [_v3_event(event) for event in EVENTS],
    }


def dump_project(project: dict[str, Any], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(
        json.dumps(project, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def generate_samples(out_dir: str | Path) -> dict[str, str]:
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    projects = {"v1": build_v1(), "v2": build_v2(), "v3": build_v3()}
    paths: dict[str, str] = {}
    for version, project in projects.items():
        path = output / f"{version}.json"
        dump_project(project, path)
        paths[version] = str(path)
    manifest = {
        "project_id": PROJECT_ID,
        "domain_id": DOMAIN_ID,
        "horizon": HORIZON,
        "versions": [{"schema": version, "path": f"{version}.json"} for version in projects],
        "canonical_sha256": __import__("hashlib").sha256(
            canonical_json(canonical_spec()).encode("utf-8")
        ).hexdigest(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return paths

