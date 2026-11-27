"""Deterministic query generation and independent full-table baselines."""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Iterable

from .indexes import IndexDefinition
from .models import PortRecord, QuerySpec


TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"


def _parse(value: str) -> datetime:
    return datetime.strptime(value, TIME_FORMAT)


def _format(value: datetime) -> str:
    return value.strftime(TIME_FORMAT)


def generate_queries(
    records: list[PortRecord],
    seed: int,
    count: int = 10,
) -> list[QuerySpec]:
    if not records:
        raise ValueError("at least one record is required to generate queries")
    if count < 2:
        raise ValueError("query count must be >= 2")

    rng = random.Random(seed ^ 0x5EED5EED)
    queries: list[QuerySpec] = []

    for index in range(count):
        record = records[rng.randrange(len(records))]
        berth_time = _parse(record.berth_time)
        depart_time = _parse(record.depart_time)

        if index % 2 == 0:
            before = rng.choice((0, 15, 30, 60, 120))
            start = berth_time - timedelta(minutes=before)
            span = max(0, int((depart_time - berth_time).total_seconds() // 60))
            after = rng.choice((0, 30, 60, 120, 180))
            end = berth_time + timedelta(minutes=span + after)
            if index % 6 == 0:
                end = depart_time
            if index % 10 == 0 and index > 0:
                start = depart_time
            queries.append(
                QuerySpec(
                    query_id=f"Q{index + 1:03d}",
                    kind="berth_overlap",
                    start=_format(start),
                    end=_format(max(start, end)),
                    berth_id=record.berth_id,
                    description=(
                        "同一泊位的占用区间与查询区间闭区间相交；"
                        "部分请求将端点精确设为靠泊或离泊时刻"
                    ),
                )
            )
        else:
            delta_before = rng.choice((0, 15, 30, 60, 120))
            delta_after = rng.choice((0, 15, 30, 60, 120))
            start = berth_time - timedelta(minutes=delta_before)
            end = berth_time + timedelta(minutes=delta_after)
            if index % 5 == 1:
                start = berth_time
            queries.append(
                QuerySpec(
                    query_id=f"Q{index + 1:03d}",
                    kind="berth_time_closed",
                    start=_format(start),
                    end=_format(end),
                    berth_id=None,
                    description="靠泊时刻落在闭区间 [start, end] 内的全部记录",
                )
            )

    # Include an explicitly empty query to verify empty-result handling too.
    queries.append(
        QuerySpec(
            query_id=f"Q{count + 1:03d}",
            kind="berth_overlap",
            start="2027-01-01T00:00:00",
            end="2027-01-02T00:00:00",
            berth_id="B99",
            description="不存在的泊位，用于验证空结果与落点行为",
        )
    )
    return queries


def full_table_baseline(
    records: Iterable[PortRecord],
    query: QuerySpec,
    index_definition: IndexDefinition,
) -> list[PortRecord]:
    """Scan every table row and then use the index's stable key order.

    Sorting happens only after the predicate has been evaluated by a physical
    table scan. It makes the independent baseline directly comparable with the
    index's stable key-order output without using any tree page.
    """

    matched = [record for record in records if query.matches(record)]
    return sorted(matched, key=index_definition.key_for_record)
