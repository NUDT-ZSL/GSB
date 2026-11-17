"""Deterministic physical-shard routing algorithms."""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Any, Sequence

from .errors import E_RING, RouterError, sql_error
from .models import (
    ColumnConstraint,
    ConsistentHashAlgorithm,
    ModAlgorithm,
    PredicateInfo,
    RangeAlgorithm,
    TableConfig,
)
from .util import canonical_json, stable_hash_int


@dataclass(frozen=True)
class VirtualNode:
    token: int
    database: int
    table: int
    replica: int
    weight: int
    source_index: int


@dataclass(frozen=True)
class VirtualRing:
    tokens: tuple[int, ...]
    nodes: tuple[VirtualNode, ...]


def physical_target(
    table: TableConfig,
    database_index: int,
    table_index: int,
    *,
    algorithm_bucket: int,
) -> dict[str, Any]:
    physical_slot = database_index * table.tables_per_database + table_index
    return {
        "algorithm_bucket": algorithm_bucket,
        "database": table.database_name(database_index),
        "database_index": database_index,
        "logical_table": table.name,
        "physical_slot": physical_slot,
        "table": table.physical_table_name(table_index),
        "table_index": table_index,
    }


def all_targets(table: TableConfig, *, algorithm_bucket: int = 0) -> tuple[dict[str, Any], ...]:
    targets = []
    for database_index in range(table.database_count):
        for table_index in range(table.tables_per_database):
            targets.append(
                physical_target(
                    table,
                    database_index,
                    table_index,
                    algorithm_bucket=algorithm_bucket,
                )
            )
    return tuple(targets)


def _target_sort_key(target: dict[str, Any]) -> tuple[str, int, int, str, str]:
    return (
        str(target["logical_table"]),
        int(target["database_index"]),
        int(target["table_index"]),
        str(target["database"]),
        str(target["table"]),
    )


def sort_targets(targets: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, int, int], dict[str, Any]] = {}
    for target in targets:
        key = (
            str(target["logical_table"]),
            int(target["database_index"]),
            int(target["table_index"]),
        )
        unique[key] = target
    return sorted(unique.values(), key=_target_sort_key)


def _point_payload(values: Sequence[Any]) -> str:
    return canonical_json(list(values))


def build_virtual_ring(table: TableConfig) -> VirtualRing:
    algorithm = table.algorithm
    if not isinstance(algorithm, ConsistentHashAlgorithm):
        raise TypeError("build_virtual_ring requires consistent_hash")

    seen_physical: dict[tuple[int, int], tuple[int, int]] = {}
    virtual_nodes: list[VirtualNode] = []
    for source_index, node in enumerate(algorithm.nodes):
        physical = (node.database, node.table)
        previous = seen_physical.get(physical)
        if previous is not None:
            previous_index, previous_weight = previous
            raise RouterError(
                code=E_RING,
                message=(
                    "same-weight ring node conflict: physical node "
                    f"{table.database_name(node.database)}.{table.physical_table_name(node.table)} "
                    f"appears more than once (weights {previous_weight} and {node.weight})"
                ),
                field_path="$",
                details={
                    "conflict": "same_weight_node",
                    "first_node_index": previous_index,
                    "second_node_index": source_index,
                    "table": table.name,
                    "weight": node.weight,
                },
            )
        seen_physical[physical] = (source_index, node.weight)
        for replica in range(node.weight):
            token_text = (
                f"{table.name}|{node.database}|{node.table}|{replica}"
            )
            virtual_nodes.append(
                VirtualNode(
                    token=stable_hash_int(token_text),
                    database=node.database,
                    table=node.table,
                    replica=replica,
                    weight=node.weight,
                    source_index=source_index,
                )
            )

    virtual_nodes.sort(
        key=lambda item: (item.token, item.database, item.table, item.replica)
    )
    previous_token: int | None = None
    previous_node: VirtualNode | None = None
    for node in virtual_nodes:
        if previous_token == node.token and previous_node is not None:
            raise RouterError(
                code=E_RING,
                message=f"virtual node token collision on table {table.name}: {node.token}",
                field_path="$",
                details={
                    "conflict": "virtual_node_token",
                    "table": table.name,
                    "token": node.token,
                    "nodes": [
                        {
                            "database": previous_node.database,
                            "table": previous_node.table,
                            "replica": previous_node.replica,
                        },
                        {
                            "database": node.database,
                            "table": node.table,
                            "replica": node.replica,
                        },
                    ],
                },
            )
        previous_token = node.token
        previous_node = node
    if not virtual_nodes:
        raise RouterError(
            code=E_RING,
            message=f"consistent hash ring for table {table.name} has no virtual nodes",
            field_path="$",
            details={"conflict": "empty_ring", "table": table.name},
        )
    return VirtualRing(
        tokens=tuple(node.token for node in virtual_nodes),
        nodes=tuple(virtual_nodes),
    )


def _ring_node_for_key(ring: VirtualRing, values: Sequence[Any]) -> VirtualNode:
    token = stable_hash_int(_point_payload(values))
    index = bisect.bisect_left(ring.tokens, token)
    if index == len(ring.tokens):
        index = 0
    return ring.nodes[index]


def _mod_targets(
    table: TableConfig,
    algorithm: ModAlgorithm,
    point_tuples: Sequence[tuple[Any, ...]],
) -> tuple[list[dict[str, Any]], list[str], str, str]:
    reasons: list[str] = []
    targets: list[dict[str, Any]] = []
    for values in point_tuples:
        payload = _point_payload(values)
        hash_value = stable_hash_int(payload)
        algorithm_bucket = hash_value % algorithm.modulus
        physical_slot = algorithm_bucket % table.total_shards
        database_index, table_index = divmod(physical_slot, table.tables_per_database)
        targets.append(
            physical_target(
                table,
                database_index,
                table_index,
                algorithm_bucket=algorithm_bucket,
            )
        )
        reasons.append(
            f"stable_hash({payload})={hash_value}; {hash_value} % {algorithm.modulus} "
            f"= bucket {algorithm_bucket}; folded to physical slot {physical_slot}"
        )
    return targets, reasons, "point", "MOD_POINT"


def _algorithm_bucket_count(algorithm: RangeAlgorithm) -> int:
    return len(algorithm.boundaries) + 1


def _numeric(value: Any, info: PredicateInfo, column: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise sql_error(
            f"range shard key {column!r} requires numeric literals",
            info.original,
            info.column_positions.get(column, 0),
        )
    return value


def _range_bucket_for(value: int | float, boundaries: Sequence[int | float]) -> int:
    # Lower-inclusive / upper-exclusive: a boundary belongs to the bucket on
    # its right.  bisect_right implements exactly that rule.
    return bisect.bisect_right(boundaries, value)


def _range_buckets_for_constraint(
    table: TableConfig,
    algorithm: RangeAlgorithm,
    constraint: ColumnConstraint,
    info: PredicateInfo,
    column: str,
) -> tuple[list[int], str]:
    boundaries = algorithm.boundaries
    bucket_count = _algorithm_bucket_count(algorithm)
    if constraint.points:
        points = sorted({_numeric(value, info, column) for value in constraint.points})
        buckets = sorted({_range_bucket_for(value, boundaries) for value in points})
        rendered_points = ", ".join(str(value) for value in points)
        return buckets, f"range point(s) {rendered_points} map to bucket(s) {buckets}"

    lower = constraint.lower
    upper = constraint.upper
    if lower is None and upper is None:
        return list(range(bucket_count)), "unbounded range scans every boundary bucket"

    minimum = 0
    if lower is not None:
        lower_value = _numeric(lower.value, info, column)
        minimum = _range_bucket_for(lower_value, boundaries)
    maximum = bucket_count - 1
    if upper is not None:
        upper_value = _numeric(upper.value, info, column)
        maximum = _range_bucket_for(upper_value, boundaries)
        if not upper.inclusive and upper_value in boundaries:
            maximum -= 1
    if maximum < minimum:
        return [], "range interval does not overlap any bucket"
    buckets = list(range(minimum, maximum + 1))
    lower_text = "-infinity" if lower is None else f"{lower.value} ({'inclusive' if lower.inclusive else 'exclusive'})"
    upper_text = "+infinity" if upper is None else f"{upper.value} ({'inclusive' if upper.inclusive else 'exclusive'})"
    return buckets, f"range interval lower={lower_text}, upper={upper_text} overlaps buckets {buckets}"


def _bucket_label(algorithm: RangeAlgorithm, bucket: int) -> str:
    boundaries = algorithm.boundaries
    if bucket == 0:
        lower = "-infinity"
    else:
        lower = str(boundaries[bucket - 1])
    if bucket >= len(boundaries):
        upper = "+infinity"
    else:
        upper = str(boundaries[bucket])
    return f"[{lower}, {upper})"


def _range_targets(
    table: TableConfig,
    algorithm: RangeAlgorithm,
    info: PredicateInfo,
) -> tuple[list[dict[str, Any]], list[str], str, str]:
    column = table.shard_keys[0]
    constraint = info.constraints.get(column.casefold())
    if constraint is None:
        return list(all_targets(table)), ["range shard key is absent; broadcasting"], "full_scan", "RANGE_MISSING_KEY"
    buckets, reason = _range_buckets_for_constraint(table, algorithm, constraint, info, column)
    targets: list[dict[str, Any]] = []
    reasons: list[str] = [reason]
    for bucket in buckets:
        physical_slot = bucket % table.total_shards
        database_index, table_index = divmod(physical_slot, table.tables_per_database)
        targets.append(
            physical_target(
                table,
                database_index,
                table_index,
                algorithm_bucket=bucket,
            )
        )
        reasons.append(
            f"range bucket {bucket} {_bucket_label(algorithm, bucket)} -> physical slot {physical_slot}"
        )
    if not targets:
        return [], reasons, "no_match", "RANGE_NO_MATCH"
    return targets, reasons, "range", "RANGE_INTERVAL"


def _hash_targets(
    table: TableConfig,
    algorithm: ConsistentHashAlgorithm,
    point_tuples: Sequence[tuple[Any, ...]],
) -> tuple[list[dict[str, Any]], list[str], str, str]:
    ring = build_virtual_ring(table)
    reasons: list[str] = []
    targets: list[dict[str, Any]] = []
    for values in point_tuples:
        token = stable_hash_int(_point_payload(values))
        node = _ring_node_for_key(ring, values)
        targets.append(
            physical_target(
                table,
                node.database,
                node.table,
                algorithm_bucket=node.token,
            )
        )
        reasons.append(
            f"key hash {token} clockwise selects token {node.token} at "
            f"{table.database_name(node.database)}.{table.physical_table_name(node.table)} "
            f"(replica {node.replica}, weight {node.weight})"
        )
    return targets, reasons, "point", "CONSISTENT_HASH_POINT"


def _point_capable(constraints: dict[str, ColumnConstraint], shard_keys: Sequence[str]) -> bool:
    for key in shard_keys:
        constraint = constraints.get(key.casefold())
        if constraint is None or not constraint.points:
            return False
    return True


def route_table(table: TableConfig, info: PredicateInfo) -> dict[str, Any]:
    """Compile one normalized predicate into a stable routing plan."""

    shard_key_columns = list(table.shard_keys)
    broadcast = not info.has_complete_shard_key
    if broadcast:
        targets = list(all_targets(table))
        reasons = [
            "predicate does not constrain every shard key; broadcast to all physical shards",
            f"missing shard keys: {', '.join(info.missing_shard_keys)}",
        ]
        coverage = "broadcast"
        reason_code = "BROADCAST_MISSING_SHARD_KEYS"
        algorithm_bucket_info = {
            "algorithm": table.algorithm.type,
            "ring_version": table.ring_version,
            "rule_version": table.version,
        }
    elif isinstance(table.algorithm, RangeAlgorithm):
        targets, reasons, coverage, reason_code = _range_targets(table, table.algorithm, info)
        algorithm_bucket_info = {
            "algorithm": table.algorithm.type,
            "ring_version": table.ring_version,
            "rule_version": table.version,
        }
    else:
        point_tuples = info.point_tuples(table.shard_keys)
        if point_tuples:
            if isinstance(table.algorithm, ModAlgorithm):
                targets, reasons, coverage, reason_code = _mod_targets(
                    table, table.algorithm, point_tuples
                )
            elif isinstance(table.algorithm, ConsistentHashAlgorithm):
                targets, reasons, coverage, reason_code = _hash_targets(
                    table, table.algorithm, point_tuples
                )
            else:  # pragma: no cover - exhaustive model union
                raise AssertionError("unsupported algorithm")
            algorithm_bucket_info = {
                "algorithm": table.algorithm.type,
                "ring_version": table.ring_version,
                "rule_version": table.version,
            }
        else:
            constraints = dict(info.constraints)
            if _point_capable(constraints, table.shard_keys):
                targets = []
                reasons = ["point constraints have no common value; no physical shard matches"]
                coverage = "no_match"
                reason_code = "KEY_NO_MATCH"
            else:
                targets = list(all_targets(table))
                reasons = [
                    "complete shard key is present, but a range predicate cannot be enumerated",
                    "scan every physical shard while keeping broadcast=false",
                ]
                coverage = "full_scan"
                reason_code = "COMPLETE_KEY_RANGE_SCAN"
            algorithm_bucket_info = {
                "algorithm": table.algorithm.type,
                "ring_version": table.ring_version,
                "rule_version": table.version,
            }

    ordered_targets = sort_targets(targets)
    return {
        "predicate": info.to_dict(),
        "routing": {
            "algorithm": algorithm_bucket_info["algorithm"],
            "broadcast": broadcast,
            "coverage": coverage,
            "parameter_version": table.parameter_version,
            "reason_code": reason_code,
            "reasons": reasons,
            "ring_version": algorithm_bucket_info["ring_version"],
            "rule_version": algorithm_bucket_info["rule_version"],
            "target_count": len(ordered_targets),
            "targets": ordered_targets,
        },
        "schema": "shardrouter.plan.v1",
        "shard_key": {
            "columns": shard_key_columns,
            "complete": info.has_complete_shard_key,
            "missing": list(info.missing_shard_keys),
        },
        "table": table.name,
    }


def route_point_for_teaching(table: TableConfig, shard_key_value: Any) -> dict[str, Any]:
    """Convenience helper used by tests: route a single shard-key point."""

    if len(table.shard_keys) != 1:
        raise ValueError("route_point_for_teaching requires a single-column shard key")
    # Construct a tiny normalized predicate without going through SQL when the
    # value is a string.  This helper is intentionally not part of the CLI.
    from .predicates import normalize_predicate

    if isinstance(shard_key_value, str):
        literal = "'" + shard_key_value.replace("'", "''") + "'"
    elif shard_key_value is None:
        literal = "NULL"
    elif isinstance(shard_key_value, bool):
        literal = "TRUE" if shard_key_value else "FALSE"
    else:
        literal = str(shard_key_value)
    return route_table(
        table,
        normalize_predicate(table, f"{table.shard_keys[0]} = {literal}"),
    )
