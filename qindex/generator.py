"""Deterministic workload generator for offline replay."""

from __future__ import annotations

import json
import random
from typing import Any


DEFAULT_SEED = 20261128

PROCESSES = (
    ("冲压", "CY"),
    ("焊装", "HZ"),
    ("涂装", "TZ"),
    ("总装", "ZZ"),
)
SHIFTS = ("早班", "中班")


def default_config() -> dict[str, Any]:
    return {
        "flush_threshold": 4,
        "merge_invalid_ratio": 0.5,
        "partition_max_bytes": 4200,
        "levels": [
            {"name": "L0", "max_segments": 1, "max_bytes": 4000},
            {"name": "L1", "max_segments": 2, "max_bytes": 8000},
            {"name": "L2", "max_segments": 3, "max_bytes": 16000},
        ],
    }


def generate_workload(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    rng = random.Random(seed)
    partitions = [
        (process, process_code, shift)
        for process, process_code in PROCESSES
        for shift in SHIFTS
    ]
    event_seq = 1
    query_seq = 1
    invalidation_seq = 1
    timeline: list[dict[str, Any]] = []

    metric_by_process = {
        "冲压": ("轮廓尺寸", "板料厚度"),
        "焊装": ("焊点强度", "定位孔偏差"),
        "涂装": ("膜厚", "色差"),
        "总装": ("扭矩", "间隙阶差"),
    }

    def make_event(
        process: str,
        process_code: str,
        shift: str,
        time: int,
        batch_number: int,
        station_suffix: int,
    ) -> dict[str, Any]:
        nonlocal event_seq
        series = "00" if time <= 8 else "1"
        batch_prefix = f"LOT-{process_code}-{series}{batch_number:02d}"
        if (
            process_code == "HZ"
            and time >= 13
            and station_suffix == 2
        ):
            station_suffix = 4
        station = f"ST-{process_code}-{station_suffix:02d}"
        metric = metric_by_process[process][(time + station_suffix) % 2]
        value = round(rng.uniform(0.0, 10.0), 3)
        result = "超差" if value >= 8.5 else "合格"
        event = {
            "event_id": f"EVT-{event_seq:06d}",
            "process": process,
            "shift": shift,
            "batch_prefix": batch_prefix,
            "logical_time": time,
            "station": station,
            "metric": metric,
            "value": value,
            "result": result,
        }
        event_seq += 1
        return event

    for time in range(1, 25):
        writes: list[dict[str, Any]] = []
        for offset in range(4):
            partition_index = (time * 3 + offset) % len(partitions)
            process, process_code, shift = partitions[partition_index]
            batch_number = ((time + offset) % 3) + 1
            writes.append(
                make_event(
                    process,
                    process_code,
                    shift,
                    time,
                    batch_number,
                    ((time + offset) % 4) + 1,
                )
            )

        if time == 6:
            process, process_code, shift = partitions[0]
            for burst_index in range(5):
                writes.append(
                    make_event(
                        process,
                        process_code,
                        shift,
                        time,
                        (burst_index % 3) + 1,
                        (burst_index % 4) + 1,
                    )
                )
        if time == 14:
            process, process_code, shift = partitions[5]
            for burst_index in range(4):
                writes.append(
                    make_event(
                        process,
                        process_code,
                        shift,
                        time,
                        (burst_index % 3) + 1,
                        ((burst_index + 1) % 4) + 1,
                    )
                )

        invalidations: list[dict[str, Any]] = []
        if time == 9:
            invalidations.append(
                {
                    "invalidation_id": f"INV-{invalidation_seq:06d}",
                    "kind": "archive_batch",
                    "batch_prefix": "LOT-CY-00",
                    "reason": "冲压早期批次归档",
                }
            )
            invalidation_seq += 1
        if time == 13:
            invalidations.append(
                {
                    "invalidation_id": f"INV-{invalidation_seq:06d}",
                    "kind": "retire_station",
                    "station": "ST-HZ-02",
                    "reason": "焊装产线改造淘汰工位",
                }
            )
            invalidation_seq += 1
        if time == 17:
            invalidations.append(
                {
                    "invalidation_id": f"INV-{invalidation_seq:06d}",
                    "kind": "archive_batch",
                    "batch_prefix": "LOT-TZ-00",
                    "reason": "涂装早期批次归档",
                }
            )
            invalidation_seq += 1

        queries: list[dict[str, Any]] = []

        def add_query(
            process: str,
            process_code: str,
            shift: str,
            prefix: str,
            purpose: str,
        ) -> None:
            nonlocal query_seq
            queries.append(
                {
                    "query_id": f"QRY-{query_seq:06d}",
                    "process": process,
                    "shift": shift,
                    "batch_prefix": prefix,
                    "purpose": purpose,
                }
            )
            query_seq += 1

        if time % 2 == 1:
            process, process_code, shift = partitions[
                (time // 2) % len(partitions)
            ]
            add_query(
                process,
                process_code,
                shift,
                f"LOT-{process_code}-",
                "完整批次空间跨段有序性",
            )
        else:
            process, process_code, shift = partitions[
                (time * 3) % len(partitions)
            ]
            series = "00" if time <= 8 else "1"
            add_query(
                process,
                process_code,
                shift,
                f"LOT-{process_code}-{series}",
                "批次前缀命中与跨段顺序",
            )

        if time == 9:
            process, process_code, shift = partitions[0]
            add_query(
                process,
                process_code,
                shift,
                "LOT-CY-00",
                "归档后旧前缀不得命中",
            )
        if time == 13:
            process, process_code, shift = partitions[2]
            add_query(
                process,
                process_code,
                shift,
                "LOT-HZ-",
                "工位淘汰后其余焊装记录仍可追溯",
            )
        if time == 17:
            process, process_code, shift = partitions[4]
            add_query(
                process,
                process_code,
                shift,
                "LOT-TZ-00",
                "归档后旧前缀不得命中",
            )
        if time == 24:
            for process, process_code, shift in partitions[:4]:
                add_query(
                    process,
                    process_code,
                    shift,
                    f"LOT-{process_code}-1",
                    "最终跨层归并核对",
                )

        timeline.append(
            {
                "time": time,
                "writes": writes,
                "invalidations": invalidations,
                "queries": queries,
            }
        )

    return {
        "format": "quality-index-workload/v1",
        "seed": seed,
        "description": (
            "确定性冲压-焊装-涂装-总装质量事件流，包含同刻突发、"
            "跨前缀检索、批次归档、工位淘汰、分层晋升与空间回收。"
        ),
        "config": default_config(),
        "timeline": timeline,
    }


def dumps_workload(workload: dict[str, Any]) -> str:
    return json.dumps(workload, ensure_ascii=False, indent=2) + "\n"
