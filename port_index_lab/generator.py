"""Deterministic generation of port operation batches and boundary cases."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any

from .models import Batch, PortRecord


BERTH_IDS = tuple(f"B{index:02d}" for index in range(1, 9))
CARGO_TYPES = ("集装箱", "煤炭", "矿石", "粮食", "滚装", "液体化工")
BASE_TIME = datetime(2026, 7, 1, 6, 0, 0)
TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _iso(value: datetime) -> str:
    return value.strftime(TIME_FORMAT)


def _batch_size(batch_id: int, page_capacity: int) -> int:
    if batch_id == 1:
        # This batch deliberately leaves the first leaf exactly at capacity.
        return page_capacity
    return max(48, page_capacity * 4)


def _make_record(
    *,
    write_seq: int,
    batch_id: int,
    ship_id: str,
    berth_id: str,
    berth_time: datetime,
    duration_minutes: int,
    tonnage: int,
    cargo_type: str,
) -> PortRecord:
    return PortRecord(
        write_seq=write_seq,
        batch_id=batch_id,
        ship_id=ship_id,
        berth_id=berth_id,
        berth_time=_iso(berth_time),
        depart_time=_iso(berth_time + timedelta(minutes=duration_minutes)),
        tonnage=tonnage,
        cargo_type=cargo_type,
    )


def generate_batches(
    seed: int,
    batch_count: int,
    page_capacity: int = 12,
) -> list[Batch]:
    """Generate reproducible batches.

    Batch 1 has exactly ``page_capacity`` records so the first leaf reaches
    the capacity boundary without splitting. Later batches start in later
    schedule windows, resembling append-only ship-plan updates.
    """

    if batch_count < 1:
        raise ValueError("batch_count must be >= 1")
    if page_capacity < 3:
        raise ValueError("page_capacity must be >= 3")

    rng = random.Random(seed)
    batches: list[Batch] = []
    write_seq = 1

    for batch_id in range(1, batch_count + 1):
        size = _batch_size(batch_id, page_capacity)
        window_start = BASE_TIME + timedelta(days=(batch_id - 1) * 2)
        records: list[PortRecord] = []

        if batch_id == 1:
            edge_time = window_start + timedelta(minutes=120)
            edge_rows = (
                ("B03", edge_time, 120, 12800, "集装箱"),
                ("B03", edge_time, 0, 9400, "煤炭"),
                ("B03", edge_time, 180, 15600, "矿石"),
                ("B07", edge_time, 60, 22000, "粮食"),
                ("B01", edge_time + timedelta(minutes=15), 0, 7600, "滚装"),
                ("B01", edge_time + timedelta(minutes=30), 90, 6100, "集装箱"),
            )
            for berth_id, berth_time, duration, tonnage, cargo in edge_rows[:size]:
                records.append(
                    _make_record(
                        write_seq=write_seq,
                        batch_id=batch_id,
                        ship_id=f"SHIP-{write_seq:05d}",
                        berth_id=berth_id,
                        berth_time=berth_time,
                        duration_minutes=duration,
                        tonnage=tonnage,
                        cargo_type=cargo,
                    )
                )
                write_seq += 1

        while len(records) < size:
            day_offset = rng.randrange(0, 2)
            minute_offset = rng.randrange(0, 24 * 4) * 15
            berth_time = (
                window_start
                + timedelta(days=day_offset)
                + timedelta(minutes=minute_offset)
            )
            duration = rng.choice((0, 30, 45, 60, 90, 120, 180, 240, 360))
            records.append(
                _make_record(
                    write_seq=write_seq,
                    batch_id=batch_id,
                    ship_id=f"SHIP-{write_seq:05d}",
                    berth_id=rng.choice(BERTH_IDS),
                    berth_time=berth_time,
                    duration_minutes=duration,
                    tonnage=rng.randrange(1_200, 82_001, 100),
                    cargo_type=rng.choice(CARGO_TYPES),
                )
            )
            write_seq += 1

        batches.append(Batch(batch_id=batch_id, records=tuple(records)))

    return batches


def generate_records(
    seed: int,
    batch_count: int,
    page_capacity: int = 12,
) -> list[PortRecord]:
    return [
        record
        for batch in generate_batches(seed, batch_count, page_capacity)
        for record in batch.records
    ]


def describe_edge_cases(
    batches: list[Batch],
    page_capacity: int,
) -> dict[str, Any]:
    records = [record for batch in batches for record in batch.records]
    same_time: dict[str, list[int]] = {}
    for record in records:
        same_time.setdefault(record.berth_time, []).append(record.write_seq)

    zero_length = [
        record.write_seq
        for record in records
        if record.berth_time == record.depart_time
    ]

    duplicate_runs: list[dict[str, Any]] = []
    run_start = 0
    for index in range(1, len(records) + 1):
        if index == len(records) or records[index].berth_id != records[run_start].berth_id:
            run_length = index - run_start
            if run_length >= 2:
                duplicate_runs.append(
                    {
                        "berth_id": records[run_start].berth_id,
                        "write_seq": [
                            record.write_seq for record in records[run_start:index]
                        ],
                        "length": run_length,
                    }
                )
            run_start = index

    repeated_times = [
        {
            "berth_time": berth_time,
            "write_seq": sequence,
            "count": len(sequence),
        }
        for berth_time, sequence in same_time.items()
        if len(sequence) >= 2
    ]

    first_batch = batches[0]
    return {
        "page_capacity_boundary": {
            "batch_id": first_batch.batch_id,
            "record_count": len(first_batch.records),
            "at_capacity": len(first_batch.records) == page_capacity,
            "note": (
                "首批记录数恰等于叶页容量；下一批首条插入将触发叶页分裂"
            ),
        },
        "same_berth_time_groups": repeated_times,
        "zero_length_intervals": {
            "count": len(zero_length),
            "write_seq": zero_length,
        },
        "consecutive_duplicate_berth_runs": duplicate_runs,
    }
