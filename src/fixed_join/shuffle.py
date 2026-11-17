from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .canonical import digest_bytes, partition_for_key
from .errors import EngineError
from .fixed import FixedContext
from .models import Dataset, JoinKeyMode, Query, Table


MAX_PARTITIONS = 64


@dataclass(frozen=True)
class PartitionRows:
    partition: int
    left: tuple[tuple[int, dict[str, Any]], ...]
    right: tuple[tuple[int, dict[str, Any]], ...]
    left_digest: str
    right_digest: str
    combined_digest: str
    left_null_keys: int
    right_null_keys: int


@dataclass(frozen=True)
class ShuffleResult:
    partitions: tuple[PartitionRows, ...]
    key_mode: JoinKeyMode
    global_digest: str
    left_float_key_rows: int
    right_float_key_rows: int


def parse_partition_count(raw: Any) -> int:
    if isinstance(raw, bool):
        raise EngineError("E_PART", "partition count must be a positive integer")
    if isinstance(raw, int):
        partitions = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text or not text.isdigit():
            raise EngineError(
                "E_PART",
                "partition count must be a positive decimal integer",
                details={"received": raw},
            )
        partitions = int(text)
    else:
        raise EngineError(
            "E_PART",
            "partition count must be an integer",
            details={"received_type": type(raw).__name__, "received": str(raw)},
        )
    if partitions <= 0:
        raise EngineError("E_PART", "partition count must be positive", details={"received": partitions})
    if partitions > MAX_PARTITIONS:
        raise EngineError(
            "E_PART",
            "partition count exceeds the supported maximum",
            details={"received": partitions, "maximum": MAX_PARTITIONS},
        )
    return partitions


def make_join_key_mode(left_table: Table, right_table: Table, query: Query) -> JoinKeyMode:
    left_type = left_table.column(query.join_key_left).type
    right_type = right_table.column(query.join_key_right).type
    uses_float = left_type == "float" or right_type == "float"
    return JoinKeyMode(
        domain="f64q" if uses_float else "i64",
        uses_float=uses_float,
        left_error_bound=1 if left_type == "float" else 0,
        right_error_bound=1 if right_type == "float" else 0,
    )


def fixed_join_key(table: Table, column_name: str, row: dict[str, Any], mode: JoinKeyMode, fixed: FixedContext) -> int | None:
    value = row[column_name]
    if value is None:
        return None
    if mode.uses_float:
        if table.column(column_name).type == "float":
            return fixed.quantize_float(float(value))
        return int(value) * fixed.scale_factor
    return int(value)


def partition_key(mode: JoinKeyMode, lattice_value: int | None, partitions: int) -> int:
    if lattice_value is None:
        return partition_for_key(f"{mode.domain}_null", 0, partitions)
    return partition_for_key(mode.domain, lattice_value, partitions)


def shuffle_dataset(dataset: Dataset, query: Query, fixed: FixedContext, partitions: int) -> ShuffleResult:
    left_table = dataset.table(query.left_table)
    right_table = dataset.table(query.right_table)
    mode = make_join_key_mode(left_table, right_table, query)
    buckets: list[dict[str, list[tuple[int, dict[str, Any]]]]] = [
        {"left": [], "right": []} for _ in range(partitions)
    ]
    null_counts = [{"left": 0, "right": 0} for _ in range(partitions)]
    left_float_key_rows = 0
    right_float_key_rows = 0

    for side, table, column_name in (
        ("left", left_table, query.join_key_left),
        ("right", right_table, query.join_key_right),
    ):
        column_type = table.column(column_name).type
        for row_index, row in enumerate(table.rows, start=1):
            lattice_value = fixed_join_key(table, column_name, row, mode, fixed)
            target = partition_key(mode, lattice_value, partitions)
            buckets[target][side].append((row_index, row))
            if lattice_value is None:
                null_counts[target][side] += 1
            if column_type == "float" and lattice_value is not None:
                if side == "left":
                    left_float_key_rows += 1
                else:
                    right_float_key_rows += 1

    partition_rows: list[PartitionRows] = []
    for partition_index, bucket in enumerate(buckets):
        left_rows = tuple(sorted(bucket["left"], key=_row_sort_key))
        right_rows = tuple(sorted(bucket["right"], key=_row_sort_key))
        left_digest = _partition_digest(left_table, left_rows)
        right_digest = _partition_digest(right_table, right_rows)
        combined_digest = digest_bytes(
            {
                "partition": partition_index,
                "left_table": left_table.name,
                "right_table": right_table.name,
                "left_digest": left_digest,
                "right_digest": right_digest,
            }
        )
        partition_rows.append(
            PartitionRows(
                partition=partition_index,
                left=left_rows,
                right=right_rows,
                left_digest=left_digest,
                right_digest=right_digest,
                combined_digest=combined_digest,
                left_null_keys=null_counts[partition_index]["left"],
                right_null_keys=null_counts[partition_index]["right"],
            )
        )

    global_digest = digest_bytes(
        {
            "partitions": partitions,
            "partition_digests": [partition.combined_digest for partition in partition_rows],
        }
    )
    return ShuffleResult(
        partitions=tuple(partition_rows),
        key_mode=mode,
        global_digest=global_digest,
        left_float_key_rows=left_float_key_rows,
        right_float_key_rows=right_float_key_rows,
    )


def _row_sort_key(item: tuple[int, dict[str, Any]]) -> bytes:
    return digest_bytes(item[1]).encode("ascii")


def _partition_digest(table: Table, rows: tuple[tuple[int, dict[str, Any]], ...]) -> str:
    ordered_payload = [row for _, row in rows]
    return digest_bytes(
        {
            "table": table.name,
            "columns": list(table.column_names),
            "rows": ordered_payload,
        }
    )
