"""Fixed-seed synthetic project generation.

No real institution, patient, or network endpoint is used. Every value is
derived locally from the supplied integer seed.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any


CATEGORIES = (
    "生化",
    "免疫",
    "微生物",
    "分子",
    "血常规",
)


def generate_project(seed: int = 20261128) -> dict[str, Any]:
    rng = random.Random(seed)
    institutions = [
        {
            "id": "HOSP-A",
            "name": "甲院（合成）",
            "required_compliance_level": 1,
            "max_latency_ms": 160,
        },
        {
            "id": "HOSP-B",
            "name": "乙院（合成）",
            "required_compliance_level": 2,
            "max_latency_ms": 120,
        },
        {
            "id": "LAB-C",
            "name": "独立检验中心丙（合成）",
            "required_compliance_level": 3,
            "max_latency_ms": 90,
        },
    ]
    regions = [
        {
            "id": "R-LOCAL",
            "name": "本地机房",
            "capacity_bytes": 180_000,
            "used_bytes": 25_000,
            "health": "healthy",
            "latency_ms": 4,
            "compliance_level": 3,
        },
        {
            "id": "R-EAST",
            "name": "异地灾备东区",
            "capacity_bytes": 120_000,
            "used_bytes": 15_000,
            "health": "healthy",
            "latency_ms": 30,
            "compliance_level": 2,
        },
        {
            "id": "R-WEST",
            "name": "异地近线西区",
            "capacity_bytes": 150_000,
            "used_bytes": 20_000,
            "health": "healthy",
            "latency_ms": 75,
            "compliance_level": 1,
        },
    ]

    institution_sequence = [
        "HOSP-A",
        "HOSP-A",
        "HOSP-B",
        "LAB-C",
        "HOSP-A",
        "HOSP-B",
        "HOSP-B",
        "LAB-C",
        "HOSP-A",
        "HOSP-B",
        "LAB-C",
        "HOSP-A",
        "HOSP-B",
        "LAB-C",
    ]
    urgency_sequence = [
        "routine",
        "urgent",
        "critical",
        "routine",
        "urgent",
        "routine",
        "critical",
        "urgent",
        "routine",
        "routine",
        "urgent",
        "critical",
        "routine",
        "urgent",
    ]
    size_sequence = [
        10_000,
        12_000,
        8_000,
        9_000,
        11_000,
        7_000,
        13_000,
        6_000,
        10_000,
        9_000,
        8_000,
        12_000,
        7_000,
        10_000,
    ]

    objects = []
    events = []
    for index, (institution_id, urgency, size) in enumerate(
        zip(institution_sequence, urgency_sequence, size_sequence), start=1
    ):
        object_id = f"EX-{index:03d}"
        category = CATEGORIES[(index + rng.randrange(len(CATEGORIES))) % len(CATEGORIES)]
        created_at = float(rng.choice([0, 0, 1, 2]))
        expires_at = float(9 + index % 4)
        reference_count = rng.choice([1, 1, 1, 2])
        digest_input = (
            f"{seed}|{object_id}|{institution_id}|{category}|"
            f"{urgency}|{size}|{created_at}|{expires_at}"
        )
        digest = "sha256:" + hashlib.sha256(
            digest_input.encode("utf-8")
        ).hexdigest()
        objects.append(
            {
                "id": object_id,
                "institution_id": institution_id,
                "test_category": category,
                "urgency": urgency,
                "bytes": size,
                "created_at": created_at,
                "expires_at": expires_at,
                "digest": digest,
                "reference_count": reference_count,
            }
        )

        if index % 4 == 0:
            events.append(
                {
                    "at": 2,
                    "type": "access",
                    "object_id": object_id,
                    "delta": 1,
                }
            )
        if reference_count == 2:
            events.append(
                {
                    "at": 6,
                    "type": "expire",
                    "object_id": object_id,
                    "delta": 1,
                }
            )
        if index % 3 == 0:
            events.append(
                {
                    "at": 10,
                    "type": "expire",
                    "object_id": object_id,
                    "delta": 1,
                }
            )

    events.sort(key=lambda event: (event["at"], _event_type_rank(event["type"])))
    incidents = [
        {
            "at": 5,
            "type": "health_degraded",
            "region_id": "R-EAST",
            "health": "degraded",
        },
        {
            "at": 5,
            "type": "health_degraded",
            "region_id": "R-WEST",
            "health": "degraded",
            "after_migration_step": 1,
        },
        {
            "at": 7,
            "type": "health_change",
            "region_id": "R-EAST",
            "health": "healthy",
        },
        {
            "at": 8,
            "type": "watermark_change",
            "region_id": "R-LOCAL",
            "watermark_ratio": 0.62,
        },
    ]

    return {
        "schema_version": 1,
        "project_id": f"synthetic-regional-exchange-{seed}",
        "seed": seed,
        "metadata": {
            "description": "固定种子生成的无真实机构区域医疗交换样本",
            "time_unit": "logical_tick",
            "generator": "exchange_storage.sample",
        },
        "policy": {
            "watermark_ratio": 0.8,
            "max_latency_ms": 180,
            "urgency_weights": {
                "routine": 1.0,
                "urgent": 2.0,
                "critical": 4.0,
            },
        },
        "institutions": institutions,
        "regions": regions,
        "objects": objects,
        "events": events,
        "incidents": incidents,
    }


def _event_type_rank(event_type: str) -> int:
    return 0 if event_type == "expire" else 1
