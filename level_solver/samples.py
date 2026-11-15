"""Deterministic local fixtures used by the demo and tests."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def demo_spec_payload() -> dict[str, Any]:
    blocked = []
    for y in list(range(0, 4)) + list(range(16, 20)):
        blocked.append([10, y])
        blocked.append([11, y])
    blocked.extend(
        [
            [4, 5],
            [5, 5],
            [20, 5],
            [21, 5],
            [20, 14],
            [21, 14],
            [7, 10],
            [8, 10],
            [22, 9],
            [23, 9],
        ]
    )
    return {
        "width": 30,
        "height": 20,
        "blocked": blocked,
        "seed": 20261115,
        "rollback_limit": 12,
        "attempt_limit": 16,
        "concurrency": 4,
        "memory_budget_kb": 512,
        "constraints": [
            {"id": "rooms", "type": "min_rooms", "count": 7},
            {"id": "area", "type": "max_area", "max_area": 12},
            {"id": "spacing", "type": "room_spacing", "min_spacing": 1},
            {"id": "connectivity", "type": "connectivity"},
            {
                "id": "reachability",
                "type": "start_end_reachable",
                "start": [0, 0],
                "end": [29, 19],
            },
        ],
    }


def conflict_spec_payload() -> dict[str, Any]:
    return {
        "width": 2,
        "height": 1,
        "blocked": [],
        "seed": 7,
        "rollback_limit": 0,
        "attempt_limit": 1,
        "concurrency": 2,
        "memory_budget_kb": 64,
        "constraints": [
            {"id": "rooms", "type": "min_rooms", "count": 2},
            {"id": "spacing", "type": "room_spacing", "min_spacing": 1},
        ],
    }


def copy_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(payload)
