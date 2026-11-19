from __future__ import annotations

import hashlib
import math
from typing import Any, Iterator

import numpy as np

from .canonical import canonical_bytes, key_sort_value, key_token
from .models import TableSpec
from .partitioning import partition_for_key


_MASK64 = (1 << 64) - 1


def _word(seed: int, table: str, row_id: int, tag: str = "") -> int:
    text = f"{seed}|{table}|{row_id}|{tag}".encode("utf-8")
    return int.from_bytes(hashlib.blake2b(text, digest_size=8).digest(), "big")


def _ratio(word: int) -> float:
    return word / float(1 << 64)


def _domain_key(column_type: str, index: int) -> Any:
    if column_type == "int":
        return index
    return f"k{index}"


def _string_value(column_name: str, index: int, width: int) -> str:
    prefix = f"{column_name}:{index}:"
    if width <= len(prefix):
        return prefix[:width]
    need = width - len(prefix)
    block = hashlib.blake2b(
        f"{column_name}|{index}".encode("utf-8"), digest_size=max(16, need // 2 + 1)
    ).hexdigest()
    value = (prefix + block)[:width]
    if len(value) < width:
        value += ("0" * width)[: width - len(value)]
    return value


def _column_value(
    seed: int,
    table_name: str,
    row_id: int,
    column_name: str,
    column_type: str,
    ndv: int,
    width: int,
    null_fraction: float,
) -> Any:
    if _ratio(_word(seed, table_name, row_id, f"{column_name}|null")) < null_fraction:
        return None
    index = _word(seed, table_name, row_id, f"{column_name}|value") % ndv
    if column_type == "int":
        return int(index)
    return _string_value(column_name, index, width)


def generate_table_rows(
    table: TableSpec,
    seed: int,
    *,
    rows_override: int | None = None,
) -> Iterator[dict[str, Any]]:
    row_count = rows_override if rows_override is not None else table.rows
    key_column = table.column_map[table.join_key]
    for row_id in range(row_count):
        key: Any
        skew_word = _word(seed, table.name, row_id, f"{table.join_key}|skew")
        if table.skew is not None and _ratio(skew_word) < table.skew.share:
            key = table.skew.key
        elif (
            _ratio(_word(seed, table.name, row_id, f"{table.join_key}|null"))
            < key_column.null_fraction
        ):
            key = None
        else:
            key_index = _word(seed, table.name, row_id, f"{table.join_key}|value") % key_column.ndv
            key = _domain_key(key_column.data_type, int(key_index))
        values: dict[str, Any] = {}
        for column in table.columns:
            if column.name == table.join_key:
                values[column.name] = key
            else:
                values[column.name] = _column_value(
                    seed,
                    table.name,
                    row_id,
                    column.name,
                    column.data_type,
                    column.ndv,
                    column.width_bytes,
                    column.null_fraction,
                )
        yield {"row_id": row_id, "values": values}


def observed_key_counts(keys: list[Any]) -> list[dict[str, Any]]:
    non_null = [key for key in keys if key is not None]
    if not non_null:
        return []
    if all(isinstance(key, int) and not isinstance(key, bool) for key in non_null):
        values = np.asarray(non_null, dtype=np.int64)
        unique, counts = np.unique(values, return_counts=True)
        pairs = sorted(
            ((int(key), int(count)) for key, count in zip(unique.tolist(), counts.tolist())),
            key=lambda item: item[0],
        )
    else:
        counter: dict[Any, int] = {}
        for key in non_null:
            counter[key] = counter.get(key, 0) + 1
        pairs = sorted(counter.items(), key=lambda item: key_sort_value(item[0]))
    return [{"key": key, "count": count} for key, count in pairs]


def build_table_stats(
    table: TableSpec,
    seed: int,
    partitions: int,
    *,
    rows_override: int | None = None,
    skew_threshold: float = 0.25,
) -> dict[str, Any]:
    row_count = rows_override if rows_override is not None else table.rows
    digest = hashlib.sha256()
    keys: list[Any] = []
    null_key_count = 0
    total_bytes = 0
    partition_counts = [0 for _ in range(partitions)]
    for row in generate_table_rows(table, seed, rows_override=row_count):
        encoded = canonical_bytes(row)
        digest.update(encoded)
        digest.update(b"\n")
        total_bytes += len(encoded) + 1
        key = row["values"][table.join_key]
        keys.append(key)
        if key is None:
            null_key_count += 1
        partition_counts[partition_for_key(key, partitions)] += 1
    key_counts = observed_key_counts(keys)
    sorted_tokens = sorted(key_token(key) for key in keys)
    key_digest = hashlib.sha256()
    for token in sorted_tokens:
        key_digest.update(token.encode("utf-8"))
        key_digest.update(b"\n")
    distinct = len(key_counts)
    top = sorted(key_counts, key=lambda item: (-item["count"], key_sort_value(item["key"])))[:5]
    max_key_count = max((item["count"] for item in key_counts), default=0)
    observed_skew_share = (max_key_count / row_count) if row_count else 0.0
    average_row_bytes = total_bytes / row_count if row_count else 0.0
    max_partition_rows = max(partition_counts, default=0)
    return {
        "table": table.name,
        "rows": row_count,
        "join_key": table.join_key,
        "key_type": table.column_map[table.join_key].data_type,
        "generated_rows_digest": digest.hexdigest(),
        "key_distribution_digest": key_digest.hexdigest(),
        "null_key_count": null_key_count,
        "distinct_non_null_keys": distinct,
        "key_counts": key_counts,
        "top_keys": top,
        "observed_max_key_count": max_key_count,
        "observed_skew_share": observed_skew_share,
        "skew_threshold": skew_threshold,
        "skew_detected": observed_skew_share >= skew_threshold,
        "declared_row_width_bytes": table.row_width_bytes,
        "total_serialized_row_bytes": total_bytes,
        "average_row_bytes": average_row_bytes,
        "estimated_total_bytes": total_bytes,
        "partition_row_counts": partition_counts,
        "max_partition_rows": max_partition_rows,
        "estimated_max_partition_bytes": int(math.ceil(max_partition_rows * average_row_bytes))
        if max_partition_rows
        else 0,
    }


def stats_summary(stats: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "table",
        "rows",
        "join_key",
        "key_type",
        "generated_rows_digest",
        "key_distribution_digest",
        "null_key_count",
        "distinct_non_null_keys",
        "observed_max_key_count",
        "observed_skew_share",
        "skew_detected",
        "declared_row_width_bytes",
        "average_row_bytes",
        "estimated_total_bytes",
        "partition_row_counts",
        "max_partition_rows",
        "estimated_max_partition_bytes",
        "top_keys",
    ]
    return {key: stats[key] for key in keys}
