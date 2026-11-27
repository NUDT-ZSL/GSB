"""Deterministic workload generator based on a local 64-bit LCG."""

from __future__ import annotations

from typing import Any

from .model import (
    CATEGORIES,
    CATEGORY_CODES,
    DIMENSIONS,
    SCHEMA_VERSION,
    validate_workload,
)

GENERATOR_NAME = "publication-pressure-sample"
GENERATOR_VERSION = "1.0"
_MASK_64 = (1 << 64) - 1


class _DeterministicRandom:
    """Tiny reproducible PRNG that does not depend on Python's random module."""

    def __init__(self, seed: int) -> None:
        self.state = (int(seed) ^ 0x9E3779B97F4A7C15) & _MASK_64
        if self.state == 0:
            self.state = 0x2545F4914F6CDD1D

    def next_u64(self) -> int:
        self.state = (
            self.state * 6364136223846793005 + 1442695040888963407
        ) & _MASK_64
        return self.state

    def randint(self, low: int, high: int) -> int:
        if high < low:
            raise ValueError("high 必须大于等于 low")
        return low + self.next_u64() % (high - low + 1)

    def chance(self, numerator: int, denominator: int = 100) -> bool:
        return self.randint(1, denominator) <= numerator


_JOB_BLUEPRINTS = (
    ("章节渲染", 5),
    ("章节渲染", 4),
    ("章节渲染", 5),
    ("章节渲染", 3),
    ("章节渲染", 4),
    ("音视频转码", 4),
    ("音视频转码", 3),
    ("音视频转码", 4),
    ("音视频转码", 5),
    ("索引重建", 1),
    ("索引重建", 2),
    ("索引重建", 1),
    ("索引重建", 2),
    ("预热预取", 1),
    ("预热预取", 2),
    ("预热预取", 1),
    ("预热预取", 3),
)

_DEMAND_RANGES = {
    "章节渲染": {"cpu": (2, 5), "memory": (4, 8), "io": (2, 5)},
    "音视频转码": {"cpu": (2, 4), "memory": (4, 7), "io": (5, 10)},
    "索引重建": {"cpu": (3, 6), "memory": (5, 10), "io": (4, 9)},
    "预热预取": {"cpu": (1, 2), "memory": (2, 4), "io": (2, 4)},
}

_DURATION_RANGES = {
    "章节渲染": (4, 8),
    "音视频转码": (6, 11),
    "索引重建": (7, 13),
    "预热预取": (3, 7),
}

_DEADLINE_WINDOWS = {
    "章节渲染": (10, 16),
    "音视频转码": (12, 19),
    "索引重建": (16, 26),
    "预热预取": (8, 14),
}


def _make_nodes(rng: _DeterministicRandom) -> list[dict[str, Any]]:
    return [
        {
            "node_id": "node-a",
            "capacity": {
                "cpu": 12 + rng.randint(0, 2),
                "memory": 24 + rng.randint(0, 2),
                "io": 30 + rng.randint(0, 3),
            },
            "categories": ["章节渲染", "预热预取"],
        },
        {
            "node_id": "node-b",
            "capacity": {
                "cpu": 10 + rng.randint(0, 2),
                "memory": 20 + rng.randint(0, 2),
                "io": 24 + rng.randint(0, 3),
            },
            "categories": ["音视频转码", "预热预取"],
        },
        {
            "node_id": "node-c",
            "capacity": {
                "cpu": 14 + rng.randint(0, 2),
                "memory": 22 + rng.randint(0, 2),
                "io": 26 + rng.randint(0, 3),
            },
            "categories": list(CATEGORIES),
        },
    ]


def _make_demand(
    category: str, priority: int, rng: _DeterministicRandom
) -> dict[str, int]:
    demand = {
        dimension: rng.randint(*_DEMAND_RANGES[category][dimension])
        for dimension in DIMENSIONS
    }
    if priority >= 5:
        demand["cpu"] = max(1, demand["cpu"] - rng.randint(0, 1))
    return demand


def _make_downgrade_plan(
    demand: dict[str, int],
    preemptible: bool,
    rng: _DeterministicRandom,
    job_index: int,
) -> dict[str, Any] | None:
    if not preemptible or not rng.chance(58):
        return None

    release = {dimension: 0 for dimension in DIMENSIONS}
    release["cpu"] = min(
        demand["cpu"],
        max(1, demand["cpu"] // 2) if rng.chance(82) else 0,
    )
    release["memory"] = min(
        demand["memory"],
        max(1, demand["memory"] // 2) if rng.chance(62) else 0,
    )
    release["io"] = min(
        demand["io"],
        max(1, demand["io"] // 3) if rng.chance(48) else 0,
    )
    if not any(release.values()):
        release["cpu"] = 1

    demoted = {
        dimension: demand[dimension] - release[dimension]
        for dimension in DIMENSIONS
    }
    return {
        "profile": f"eco-{job_index // 2 + 1}",
        "demand": demoted,
        "release": release,
    }


def generate_workload(seed: int) -> dict[str, Any]:
    """Generate a deterministic, reference-reconstructable workload."""

    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError("seed 必须是整数")
    rng = _DeterministicRandom(seed)
    nodes = _make_nodes(rng)

    # A fixed staggered arrival pattern makes the sample exercise contention
    # without depending on wall-clock order or process scheduling.
    arrival_pattern = (
        0,
        0,
        1,
        2,
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        13,
        14,
        15,
    )
    mutex_assignments = {
        0: "edition-2026-11-a",
        5: "edition-2026-11-a",
        9: "index-shard-17",
        13: "index-shard-17",
    }

    jobs: list[dict[str, Any]] = []
    cancel_job_ids: list[str] = []
    for index, (category, priority) in enumerate(_JOB_BLUEPRINTS):
        job_id = f"job-{index + 1:03d}"
        submit_time = arrival_pattern[index]
        demand = _make_demand(category, priority, rng)
        duration = rng.randint(*_DURATION_RANGES[category])
        deadline_window = rng.randint(*_DEADLINE_WINDOWS[category])
        preemptible = rng.chance(78)
        if priority <= 2:
            preemptible = True
        if category == "预热预取" and rng.chance(25):
            preemptible = False
        plan = _make_downgrade_plan(demand, preemptible, rng, index)
        jobs.append(
            {
                "job_id": job_id,
                "name": f"{CATEGORY_CODES[category]}-{index + 1:03d}",
                "category": category,
                "category_code": CATEGORY_CODES[category],
                "priority": priority,
                "preemptible": preemptible,
                "demand": demand,
                "duration": duration,
                "deadline": submit_time + deadline_window,
                "submit_time": submit_time,
                "mutex_group": mutex_assignments.get(index),
                "downgrade_plan": plan,
            }
        )

    # Keep at least one cancellation in every generated sample; choose only
    # non-mutex jobs so cancellation remains easy to inspect independently.
    candidates = [
        job["job_id"]
        for job in jobs
        if job["mutex_group"] is None and job["category"] in ("索引重建", "预热预取")
    ]
    if candidates:
        first = candidates[rng.randint(0, len(candidates) - 1)]
        cancel_job_ids.append(first)
        if len(candidates) > 2 and rng.chance(45):
            second_pool = [item for item in candidates if item != first]
            cancel_job_ids.append(second_pool[rng.randint(0, len(second_pool) - 1)])

    job_by_id = {job["job_id"]: job for job in jobs}
    event_parts: list[tuple[int, int, str, str]] = []
    for job in jobs:
        event_parts.append(
            (job["submit_time"], 1, "submit", job["job_id"])
        )
    for job_id in cancel_job_ids:
        job = job_by_id[job_id]
        cancel_at = job["submit_time"] + rng.randint(1, 3)
        event_parts.append((cancel_at, 0, "cancel", job_id))
    event_parts.sort(key=lambda item: (item[0], item[1], item[3]))

    events: list[dict[str, Any]] = []
    for sequence, (at, _, event_type, job_id) in enumerate(event_parts):
        events.append(
            {
                "event_id": f"event-{sequence + 1:04d}",
                "at": at,
                "sequence": sequence,
                "type": event_type,
                "job_id": job_id,
            }
        )

    workload = {
        "schema_version": SCHEMA_VERSION,
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
            "seed": seed,
            "rng": "lcg-6364136223846793005-1442695040888963407",
        },
        "seed": seed,
        "nodes": nodes,
        "jobs": jobs,
        "events": events,
    }
    return validate_workload(workload)

