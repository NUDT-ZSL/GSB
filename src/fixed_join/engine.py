from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import digest_bytes
from .errors import EngineError
from .fixed import FixedContext, fixed_comparison, parse_scale_factor, reference_comparison
from .hashtable import OpenAddressingMultiMap, capacity_for_rows
from .loader import load_dataset, load_query
from .models import Column, Dataset, Predicate, Query, Table
from .shuffle import (
    MAX_PARTITIONS,
    PartitionRows,
    fixed_join_key,
    make_join_key_mode,
    parse_partition_count,
    shuffle_dataset,
)


@dataclass(frozen=True)
class EngineConfig:
    tables_path: str | Path
    query_path: str | Path
    partitions: int | str
    scale_factor: int | str
    injection: str | None = None


@dataclass
class _MatchedRow:
    left_index: int
    right_index: int
    partition: int
    left_row: dict[str, Any]
    right_row: dict[str, Any]
    key_lattice: int
    left_key_reference: int | float
    right_key_reference: int | float
    reference_collision: bool


def execute(config: EngineConfig) -> dict[str, Any]:
    events: list[dict[str, Any]] = []

    def record(event: str, **fields: Any) -> None:
        events.append({"seq": len(events) + 1, "event": event, **fields})

    fixed = parse_scale_factor(config.scale_factor)
    record(
        "SCALE_VALIDATED",
        scale_factor=fixed.scale_factor,
        scale_exponent=fixed.scale_exponent,
        rounding="floor_toward_negative_infinity",
    )
    partitions = parse_partition_count(config.partitions)
    record("PARTITIONS_VALIDATED", partitions=partitions, maximum=MAX_PARTITIONS)

    dataset = load_dataset(config.tables_path)
    for table in dataset.tables:
        record("TABLE_LOADED", table=table.name, rows=table.row_count, columns=len(table.columns))
    query = load_query(config.query_path, dataset)
    record("QUERY_VALIDATED", join_type=query.join_type, build_side=query.build_side)

    left_table = dataset.table(query.left_table)
    right_table = dataset.table(query.right_table)
    key_mode = make_join_key_mode(left_table, right_table, query)
    shuffle_result = shuffle_dataset(dataset, query, fixed, partitions)
    record("SHUFFLE_COMPLETED", partitions=partitions, global_digest=shuffle_result.global_digest)

    matched_rows: list[_MatchedRow] = []
    partition_stats: list[dict[str, Any]] = []
    partition_records: dict[int, list[_MatchedRow]] = {partition: [] for partition in range(partitions)}
    total_predicate_boundary_mismatches = 0
    total_predicate_affected_rows = 0

    for partition in shuffle_result.partitions:
        partition_matches, stats = _join_partition(
            partition=partition,
            dataset=dataset,
            query=query,
            fixed=fixed,
        )
        matched_rows.extend(partition_matches)
        partition_records[partition.partition] = partition_matches
        total_predicate_boundary_mismatches += stats["predicate_boundary_mismatch_count"]
        total_predicate_affected_rows += stats["predicate_boundary_affected_row_count"]
        partition_stats.append(stats)
        record(
            "PARTITION_JOINED",
            partition=partition.partition,
            matches=stats["matched_pair_count"],
            reference_collisions=stats["quantized_reference_collision_count"],
        )

    matched_rows.sort(key=lambda item: (item.key_lattice, item.left_index, item.right_index))
    report_rows, aggregate_value_by_row = _project_rows(matched_rows, query, dataset, fixed)
    _assign_row_attribution(report_rows, aggregate_value_by_row, query, dataset, fixed, key_mode)
    _assert_attribution_consistency(report_rows)
    record("ATTRIBUTION_COMPLETED", rows=len(report_rows), rule="partition-independent global sequence")

    aggregate = _aggregate_report(report_rows, query, dataset, fixed)
    partition_aggregates = {
        partition: _aggregate_report(
            [row for row in report_rows if row["partition"] == partition],
            query,
            dataset,
            fixed,
        )
        for partition in range(partitions)
    }
    for stats in partition_stats:
        stats["aggregate"] = partition_aggregates[stats["partition"]]

    collisions = [
        {
            "left_row_index": row.left_index,
            "right_row_index": row.right_index,
            "partition": row.partition,
            "join_key_fixed_lattice": row.key_lattice,
            "left_reference": row.left_key_reference,
            "right_reference": row.right_key_reference,
            "handling": "included_by_fixed_point_equality_and_explicitly_reported",
        }
        for row in matched_rows
        if row.reference_collision
    ]

    error_propagation = _error_propagation(
        report_rows=report_rows,
        aggregate=aggregate,
        key_mode=key_mode,
        collision_count=len(collisions),
        predicate_mismatch_count=total_predicate_boundary_mismatches,
        predicate_affected_rows=total_predicate_affected_rows,
        source_row_count=left_table.row_count + right_table.row_count,
        float_key_source_rows=shuffle_result.left_float_key_rows + shuffle_result.right_float_key_rows,
    )

    output_digest = digest_bytes(report_rows)
    table_summary = [
        {
            "name": table.name,
            "row_count": table.row_count,
            "columns": [
                {
                    "name": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                    "min_value": column.min_value,
                    "max_value": column.max_value,
                }
                for column in table.columns
            ],
            "null_counts": {
                column.name: sum(table.null_bitmap[column.name])
                for column in table.columns
                if column.nullable
            },
            "fingerprint": _table_fingerprint(table),
        }
        for table in dataset.tables
    ]
    partition_shuffle_stats = [
        {
            "partition": partition.partition,
            "left_rows": len(partition.left),
            "right_rows": len(partition.right),
            "left_null_keys": partition.left_null_keys,
            "right_null_keys": partition.right_null_keys,
            "left_digest": partition.left_digest,
            "right_digest": partition.right_digest,
            "combined_digest": partition.combined_digest,
        }
        for partition in shuffle_result.partitions
    ]

    record(
        "EXECUTION_COMPLETED",
        output_rows=len(report_rows),
        output_digest=output_digest,
        dominant_segment=error_propagation["dominant_segment"],
    )
    return {
        "schema_version": "1.0",
        "status": "OK",
        "injection": config.injection,
        "config": {
            "tables_file": str(config.tables_path),
            "query_file": str(config.query_path),
            "partitions": partitions,
            "partition_limit": MAX_PARTITIONS,
            "scale_factor": fixed.scale_factor,
            "scale_exponent": fixed.scale_exponent,
            "build_side": query.build_side,
            "join_type": query.join_type,
            "table_fingerprints": {table.name: _table_fingerprint(table) for table in dataset.tables},
            "query_fingerprint": digest_bytes(_query_summary(query)),
        },
        "table_summary": table_summary,
        "query_summary": _query_summary(query),
        "quantization": {
            "scale_factor": fixed.scale_factor,
            "scale_exponent": fixed.scale_exponent,
            "integer_lattice_unit": {"numerator": 1, "denominator": fixed.scale_factor},
            "input_rounding_rule": "floor_toward_negative_infinity",
            "mean_rounding_rule": "truncate_toward_zero",
            "multiplication": "integer_lattice_values_are_used_for_hash_join_compare_and_sum",
            "division": "integer_division_only",
            "bounds": {
                "single_value": {
                    "bound_lattice": 1,
                    "formula": "x_hat=floor(x*S)/S; x_hat-x in (-1/S,0]; |x_hat-x|<1/S",
                },
                "comparison": {
                    "bound_lattice": 1,
                    "formula": "two floor-quantized values in one lattice cell differ by less than 1/S; equality is uncertain within one lattice unit",
                },
                "sum_across_n_inputs": {
                    "bound_lattice": "n for float inputs; 0 for integer inputs",
                    "formula": "sum_i(x_hat_i-x_i) in (-n/S,0]; conservative integer bound n",
                },
                "mean_across_n_inputs": {
                    "bound_lattice": "n+1 for float inputs; 1 for integer-only inputs",
                    "formula": "sum bound plus at most one lattice unit from truncation toward zero",
                },
            },
            "report_values": {
                "sum_error_bound_lattice": aggregate["sum_error_bound_lattice"],
                "mean_error_bound_lattice": aggregate["mean_error_bound_lattice"],
            },
        },
        "shuffle": {
            "hash_rule": {
                "algorithm": "sha256 over ASCII 'fixed-join-v1|<domain>|<signed_lattice_value>'",
                "integer_key_domain": "i64",
                "float_key_domain": "f64q",
                "cross_type_rule": "if either join key is float, integer-side values are multiplied by S before hashing",
                "partition_rule": "first_8_sha256_bytes_as_big_endian_uint64 modulo partition_count",
                "null_key_domain_suffix": "_null",
                "row_order_rule": "partition digest sorts rows by SHA-256 of canonical row JSON",
            },
            "partitions": partitions,
            "global_digest": shuffle_result.global_digest,
            "partition_statistics": partition_shuffle_stats,
        },
        "join": {
            "left_table": left_table.name,
            "right_table": right_table.name,
            "join_key": {"left": query.join_key_left, "right": query.join_key_right},
            "key_domain": key_mode.domain,
            "quantized_reference_collision_count": len(collisions),
            "quantized_reference_collisions": collisions,
            "partition_statistics": partition_stats,
        },
        "aggregate": aggregate,
        "error_propagation": error_propagation,
        "rows": report_rows,
        "determinism": {
            "output_digest": output_digest,
            "row_count": len(report_rows),
            "row_sort_rule": "join_key_fixed_lattice, left_row_index, right_row_index",
            "partition_digest_rule": "canonical JSON SHA-256 over row-content-sorted partitions",
        },
        "events": events,
    }


def _join_partition(
    partition: PartitionRows,
    dataset: Dataset,
    query: Query,
    fixed: FixedContext,
) -> tuple[list[_MatchedRow], dict[str, Any]]:
    left_table = dataset.table(query.left_table)
    right_table = dataset.table(query.right_table)
    mode = make_join_key_mode(left_table, right_table, query)

    left_rows, left_filter = _filter_rows(left_table, partition.left, query.predicates["left"], fixed)
    right_rows, right_filter = _filter_rows(right_table, partition.right, query.predicates["right"], fixed)

    if query.build_side == "right":
        build_table, build_rows = right_table, right_rows
        probe_table, probe_rows = left_table, left_rows
        build_column, probe_column = query.join_key_right, query.join_key_left
    else:
        build_table, build_rows = left_table, left_rows
        probe_table, probe_rows = right_table, right_rows
        build_column, probe_column = query.join_key_left, query.join_key_right

    build_keys = [
        (row_index, fixed_join_key(build_table, build_column, row, mode, fixed))
        for row_index, row in build_rows
    ]
    build_nonnull = [(row_index, key) for row_index, key in build_keys if key is not None]
    capacity = capacity_for_rows(len(build_nonnull))
    table_map = OpenAddressingMultiMap(mode.domain, capacity=capacity, probe_limit=8)
    for row_index, key in build_nonnull:
        table_map.insert(key, row_index)

    build_by_index = {row_index: row for row_index, row in build_rows}
    matches: list[_MatchedRow] = []
    probe_nonnull_count = 0
    probe_null_count = 0
    collisions = 0
    for probe_index, probe_row in probe_rows:
        probe_key = fixed_join_key(probe_table, probe_column, probe_row, mode, fixed)
        if probe_key is None:
            probe_null_count += 1
            continue
        probe_nonnull_count += 1
        build_indices, _, _ = table_map.lookup(probe_key)
        for build_index in build_indices:
            build_row = build_by_index[build_index]
            if query.build_side == "right":
                left_index, right_index = probe_index, build_index
                left_row, right_row = probe_row, build_row
            else:
                left_index, right_index = build_index, probe_index
                left_row, right_row = build_row, probe_row
            left_reference = left_row[query.join_key_left]
            right_reference = right_row[query.join_key_right]
            collision = bool(mode.uses_float and left_reference != right_reference)
            if collision:
                collisions += 1
            matches.append(
                _MatchedRow(
                    left_index=left_index,
                    right_index=right_index,
                    partition=partition.partition,
                    left_row=left_row,
                    right_row=right_row,
                    key_lattice=probe_key,
                    left_key_reference=left_reference,
                    right_key_reference=right_reference,
                    reference_collision=collision,
                )
            )

    stats = {
        "partition": partition.partition,
        "left_input_rows": len(partition.left),
        "right_input_rows": len(partition.right),
        "left_null_join_keys": partition.left_null_keys,
        "right_null_join_keys": partition.right_null_keys,
        "left_rows_after_predicates": len(left_rows),
        "right_rows_after_predicates": len(right_rows),
        "left_predicate_boundary_mismatch_count": left_filter["mismatch_count"],
        "right_predicate_boundary_mismatch_count": right_filter["mismatch_count"],
        "predicate_boundary_mismatch_count": left_filter["mismatch_count"] + right_filter["mismatch_count"],
        "left_predicate_boundary_affected_rows": left_filter["affected_rows"],
        "right_predicate_boundary_affected_rows": right_filter["affected_rows"],
        "predicate_boundary_affected_row_count": left_filter["affected_rows"] + right_filter["affected_rows"],
        "build_side": query.build_side,
        "build_rows_considered": len(build_rows),
        "probe_rows_considered": len(probe_rows),
        "build_null_keys_skipped": len(build_rows) - len(build_nonnull),
        "probe_null_keys_skipped": probe_null_count,
        "probe_non_null_keys": probe_nonnull_count,
        "bucket_capacity": capacity,
        "occupied_buckets": table_map.occupied_buckets,
        "load_factor": round(len(build_nonnull) / capacity, 6),
        "load_factor_numerator": len(build_nonnull),
        "load_factor_denominator": capacity,
        "bucket_probe_count": table_map.build_probe_count + table_map.lookup_probe_count,
        "build_probe_count": table_map.build_probe_count,
        "lookup_probe_count": table_map.lookup_probe_count,
        "overflow_entry_count": len(table_map.overflow),
        "overflow_probe_count": table_map.overflow_access_count,
        "overflow_area_access_count": table_map.overflow_access_count,
        "max_probe_length": table_map.max_probe_length,
        "matched_pair_count": len(matches),
        "quantized_reference_collision_count": collisions,
    }
    return matches, stats


def _filter_rows(
    table: Table,
    rows: tuple[tuple[int, dict[str, Any]], ...],
    predicates: tuple[Predicate, ...],
    fixed: FixedContext,
) -> tuple[list[tuple[int, dict[str, Any]]], dict[str, int]]:
    if not predicates:
        return list(rows), {"mismatch_count": 0, "affected_rows": 0}
    eligible: list[tuple[int, dict[str, Any]]] = []
    mismatch_count = 0
    affected_rows = 0
    for row_index, row in rows:
        passed = True
        row_mismatch = False
        for predicate in predicates:
            column = table.column(predicate.column)
            value = row[predicate.column]
            if value is None:
                passed = False
                break
            if column.type == "float":
                actual = float(value)
                constant = float(predicate.value)
                fixed_actual = fixed.quantize_float(actual)
                fixed_constant = fixed.quantize_float(constant)
                fixed_decision = fixed_comparison(fixed_actual, predicate.op, fixed_constant)
                reference_decision = reference_comparison(actual, predicate.op, constant)
            else:
                actual = int(value)
                constant = int(predicate.value)
                fixed_decision = fixed_comparison(actual, predicate.op, constant)
                reference_decision = fixed_decision
            if fixed_decision != reference_decision:
                mismatch_count += 1
                row_mismatch = True
            if not fixed_decision:
                passed = False
                break
        if row_mismatch:
            affected_rows += 1
        if passed:
            eligible.append((row_index, row))
    return eligible, {"mismatch_count": mismatch_count, "affected_rows": affected_rows}


def _project_rows(
    matched_rows: list[_MatchedRow],
    query: Query,
    dataset: Dataset,
    fixed: FixedContext,
) -> tuple[list[dict[str, Any]], list[dict[str, Any] | None]]:
    left_table = dataset.table(query.left_table)
    right_table = dataset.table(query.right_table)
    aggregate_table = left_table if query.aggregate.side == "left" else right_table
    aggregate_column = aggregate_table.column(query.aggregate.column)
    projection_rows: list[dict[str, Any]] = []
    aggregate_values: list[dict[str, Any] | None] = []

    for sequence, match in enumerate(matched_rows, start=1):
        projected: dict[str, Any] = {}
        for item in query.projection:
            table = left_table if item.side == "left" else right_table
            source_row = match.left_row if item.side == "left" else match.right_row
            column = table.column(item.column)
            projected[item.name] = _project_value(column, source_row[item.column], fixed)

        aggregate_row = match.left_row if query.aggregate.side == "left" else match.right_row
        aggregate_raw = aggregate_row[query.aggregate.column]
        aggregate_value = _aggregate_input(aggregate_column, aggregate_raw, fixed)
        aggregate_values.append(aggregate_value)
        projection_rows.append(
            {
                "row_id": f"L{match.left_index:06d}:R{match.right_index:06d}",
                "output_sequence": sequence,
                "partition": match.partition,
                "left_row_index": match.left_index,
                "right_row_index": match.right_index,
                "join_key": {
                    "fixed_lattice": match.key_lattice,
                    "fixed_value": fixed.to_real(match.key_lattice) if _join_uses_float_scale(dataset, query) else match.key_lattice,
                    "left_reference": match.left_key_reference,
                    "right_reference": match.right_key_reference,
                    "reference_collision": match.reference_collision,
                },
                "projected": projected,
                "aggregate_input": aggregate_value,
            }
        )
    return projection_rows, aggregate_values


def _join_uses_float_scale(dataset: Dataset, query: Query) -> bool:
    left_table = dataset.table(query.left_table)
    right_table = dataset.table(query.right_table)
    return left_table.column(query.join_key_left).type == "float" or right_table.column(query.join_key_right).type == "float"


def _project_value(column: Column, value: Any, fixed: FixedContext) -> dict[str, Any]:
    if value is None:
        return {
            "type": column.type,
            "null": True,
            "reference": None,
            "fixed_lattice": None,
            "fixed_value": None,
        }
    if column.type == "int":
        integer = int(value)
        return {
            "type": "int",
            "null": False,
            "reference": integer,
            "fixed_lattice": integer,
            "fixed_value": integer,
            "quantization_error_bound_lattice": 0,
        }
    lattice_value = fixed.quantize_float(float(value))
    return {
        "type": "float",
        "null": False,
        "reference": float(value),
        "fixed_lattice": lattice_value,
        "fixed_value": fixed.to_real(lattice_value),
        "quantization_error_bound_lattice": 1,
    }


def _aggregate_input(column: Column, value: Any, fixed: FixedContext) -> dict[str, Any] | None:
    if value is None:
        return None
    if column.type == "float":
        lattice_value = fixed.quantize_float(float(value))
        return {
            "type": "float",
            "reference": float(value),
            "fixed_lattice": lattice_value,
            "error_unit_bound_lattice": 1,
        }
    lattice_value = int(value) * fixed.scale_factor
    return {
        "type": "int",
        "reference": int(value),
        "fixed_lattice": lattice_value,
        "error_unit_bound_lattice": 0,
    }


def _assign_row_attribution(
    rows: list[dict[str, Any]],
    aggregate_values: list[dict[str, Any] | None],
    query: Query,
    dataset: Dataset,
    fixed: FixedContext,
    key_mode: Any,
) -> None:
    left_table = dataset.table(query.left_table)
    right_table = dataset.table(query.right_table)
    aggregate_type = (left_table if query.aggregate.side == "left" else right_table).column(query.aggregate.column).type
    aggregate_prefix_bound = 0
    for row, aggregate_value in zip(rows, aggregate_values):
        if aggregate_value is not None:
            aggregate_prefix_bound += 1 if aggregate_type == "float" else 0
        projection_float_count = sum(
            1
            for value in row["projected"].values()
            if value["type"] == "float" and not value["null"]
        )
        key_bound = key_mode.left_error_bound + key_mode.right_error_bound
        quantization_bound = key_bound + projection_float_count
        if aggregate_prefix_bound == 0:
            primary = "quantization"
        elif quantization_bound == 0:
            primary = "aggregation"
        elif quantization_bound > aggregate_prefix_bound:
            primary = "quantization"
        elif aggregate_prefix_bound > quantization_bound:
            primary = "aggregation"
        else:
            primary = "both"
        row["error_attribution"] = {
            "primary_source": primary,
            "join_key_quantization_bound_lattice": key_bound,
            "projection_quantization_bound_lattice": projection_float_count,
            "quantization_bound_lattice": quantization_bound,
            "aggregation_prefix_bound_lattice": aggregate_prefix_bound,
            "combined_bound_lattice": quantization_bound + aggregate_prefix_bound,
            "rule": "global canonical row sequence; compare quantization bound with cumulative float aggregation bound",
            "partition_independent": True,
        }


def _assert_attribution_consistency(rows: list[dict[str, Any]]) -> None:
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        row_id = row["row_id"]
        attribution = row["error_attribution"]
        previous = seen.get(row_id)
        if previous is not None and previous != attribution:
            raise EngineError(
                "E_ATTR",
                "the same output row received inconsistent error attribution",
                row=row["left_row_index"],
                column="error_attribution",
                details={"row_id": row_id},
            )
        seen[row_id] = attribution


def _aggregate_report(
    rows: list[dict[str, Any]],
    query: Query,
    dataset: Dataset,
    fixed: FixedContext,
) -> dict[str, Any]:
    aggregate_table = dataset.table(query.left_table if query.aggregate.side == "left" else query.right_table)
    aggregate_type = aggregate_table.column(query.aggregate.column).type
    total_count = len(rows)
    nonnull_values = [row["aggregate_input"] for row in rows if row["aggregate_input"] is not None]
    nonnull_count = len(nonnull_values)
    null_count = total_count - nonnull_count
    sum_fixed = sum(int(value["fixed_lattice"]) for value in nonnull_values)
    mean_fixed = _truncate_division(sum_fixed, nonnull_count) if nonnull_count else None
    sum_bound = nonnull_count if aggregate_type == "float" else 0
    mean_bound = (sum_bound + 1) if nonnull_count else 0
    return {
        "source": {"side": query.aggregate.side, "column": query.aggregate.column, "type": aggregate_type},
        "count_total": total_count,
        "count_non_null": nonnull_count,
        "count_null": null_count,
        "sum_fixed_lattice": sum_fixed,
        "sum_fixed_value": fixed.to_real(sum_fixed),
        "sum_error_bound_lattice": sum_bound,
        "mean_fixed_lattice": mean_fixed,
        "mean_fixed_value": fixed.to_real(mean_fixed) if mean_fixed is not None else None,
        "mean_error_bound_lattice": mean_bound,
        "mean_rounding": "truncate_toward_zero",
        "null_policy": "null inputs are excluded from sum and mean and counted in count_null",
    }


def _truncate_division(numerator: int, denominator: int) -> int:
    if denominator == 0:
        raise ZeroDivisionError
    quotient = abs(numerator) // abs(denominator)
    if (numerator < 0) != (denominator < 0):
        return -quotient
    return quotient


def _error_propagation(
    report_rows: list[dict[str, Any]],
    aggregate: dict[str, Any],
    key_mode: Any,
    collision_count: int,
    predicate_mismatch_count: int,
    predicate_affected_rows: int,
    source_row_count: int,
    float_key_source_rows: int,
) -> dict[str, Any]:
    output_count = len(report_rows)
    shuffle_bound = output_count * (key_mode.left_error_bound + key_mode.right_error_bound)
    bucket_bound = collision_count + predicate_mismatch_count
    aggregation_bound = aggregate["sum_error_bound_lattice"]
    segments = [
        {
            "segment": "shuffle",
            "bound_lattice": shuffle_bound,
            "affected_rows": float_key_source_rows,
            "denominator_rows": source_row_count,
            "affected_row_ratio": _ratio(float_key_source_rows, source_row_count),
            "method": "one lattice unit per float join-key side for each output pair",
        },
        {
            "segment": "bucket_probe",
            "bound_lattice": bucket_bound,
            "affected_rows": collision_count + predicate_affected_rows,
            "denominator_rows": output_count + predicate_affected_rows,
            "affected_row_ratio": _ratio(collision_count + predicate_affected_rows, output_count + predicate_affected_rows),
            "method": "one lattice unit per quantized reference collision and predicate boundary mismatch",
            "quantized_reference_collision_count": collision_count,
            "predicate_boundary_mismatch_count": predicate_mismatch_count,
        },
        {
            "segment": "aggregation",
            "bound_lattice": aggregation_bound,
            "affected_rows": aggregate["count_non_null"],
            "denominator_rows": output_count,
            "affected_row_ratio": _ratio(aggregate["count_non_null"], output_count),
            "method": "one lattice unit per non-null float aggregate input",
        },
    ]
    total = sum(segment["bound_lattice"] for segment in segments)
    for segment in segments:
        segment["bound_share"] = round(segment["bound_lattice"] / total, 6) if total else 0.0
    top = max(segments, key=lambda segment: segment["bound_lattice"])
    if total == 0:
        dominant = "none"
        dominant_share = 0.0
    else:
        dominant_share = top["bound_lattice"] / total
        dominant = top["segment"] if dominant_share >= 0.5 else "mixed"
    return {
        "segments": segments,
        "total_bound_lattice": total,
        "dominant_segment": dominant,
        "dominant_share": round(dominant_share, 6),
        "threshold_rule": {
            "rule": "a segment is dominant only when its bound is at least 50% of the three-segment total; otherwise mixed",
            "threshold_ratio": 0.5,
        },
    }


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    if denominator <= 0:
        return {"numerator": numerator, "denominator": denominator, "percent": 0.0}
    return {
        "numerator": numerator,
        "denominator": denominator,
        "percent": round(100.0 * numerator / denominator, 6),
    }


def _table_fingerprint(table: Table) -> str:
    return digest_bytes(
        {
            "name": table.name,
            "columns": [
                {
                    "name": column.name,
                    "type": column.type,
                    "nullable": column.nullable,
                    "min_value": column.min_value,
                    "max_value": column.max_value,
                }
                for column in table.columns
            ],
            "null_bitmap": {name: list(bits) for name, bits in table.null_bitmap.items()},
            "rows": list(table.rows),
        }
    )


def _query_summary(query: Query) -> dict[str, Any]:
    return {
        "left_table": query.left_table,
        "right_table": query.right_table,
        "join_type": query.join_type,
        "join_key": {"left": query.join_key_left, "right": query.join_key_right},
        "build_side": query.build_side,
        "predicates": {
            side: [
                {"column": predicate.column, "op": predicate.op, "value": predicate.value}
                for predicate in predicates
            ]
            for side, predicates in query.predicates.items()
        },
        "projection": [
            {"name": item.name, "side": item.side, "column": item.column}
            for item in query.projection
        ],
        "aggregate": {"side": query.aggregate.side, "column": query.aggregate.column},
    }
