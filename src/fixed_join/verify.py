from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import digest_bytes
from .errors import EngineError
from .fixed import FixedContext, fixed_comparison, parse_scale_factor
from .hashtable import capacity_for_rows
from .loader import load_dataset, load_query
from .models import Dataset, Query
from .shuffle import fixed_join_key, make_join_key_mode, parse_partition_count, partition_key, shuffle_dataset


def verify_artifacts(
    tables_path: str | Path,
    query_path: str | Path,
    report_paths: list[str | Path],
    manifest_path: str | Path | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    dataset = load_dataset(tables_path)
    query = load_query(query_path, dataset)
    manifest: dict[str, Any] | None = None
    resolved_report_paths = [Path(path) for path in report_paths]
    injection_checks: list[dict[str, Any]] = []

    if manifest_path is not None:
        manifest_file = Path(manifest_path)
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            return _failure("E_VERIFY", "manifest", f"cannot read manifest: {exc}", output_path)
        for item in manifest.get("experiments", []):
            resolved_report_paths.append((manifest_file.parent / item["report"]).resolve())
        injection_checks = manifest.get("injections", [])

    reports: list[tuple[Path, dict[str, Any]]] = []
    for report_path in resolved_report_paths:
        try:
            report = json.loads(Path(report_path).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            return _failure("E_VERIFY", "report", f"cannot read report {report_path}: {exc}", output_path)
        if report.get("status") != "OK":
            return _failure("E_VERIFY", "status", f"report is not successful: {report_path}", output_path)
        reports.append((Path(report_path), report))

    if not reports:
        return _failure("E_VERIFY", "reports", "no successful reports were supplied", output_path)

    verified_reports: list[dict[str, Any]] = []
    verifier_contexts: list[tuple[Path, dict[str, Any], FixedContext, int]] = []
    for report_path, report in reports:
        deviation = _verify_single_report(dataset, query, report_path, report)
        if deviation is not None:
            return _failure(
                deviation["code"],
                deviation["item"],
                deviation["message"],
                output_path,
                details=deviation.get("details", {}),
                report_path=str(report_path),
            )
        fixed = parse_scale_factor(report["config"]["scale_factor"])
        partitions = parse_partition_count(report["config"]["partitions"])
        verifier_contexts.append((report_path, report, fixed, partitions))
        verified_reports.append(
            {
                "report": str(report_path),
                "fingerprint": digest_bytes(report),
                "rows": len(report["rows"]),
                "partition_digest": report["shuffle"]["global_digest"],
            }
        )

    attribution_deviation = _verify_cross_report_attribution(verifier_contexts)
    if attribution_deviation is not None:
        return _failure(
            "E_ATTR",
            attribution_deviation["item"],
            attribution_deviation["message"],
            output_path,
            details=attribution_deviation.get("details", {}),
            report_path=attribution_deviation.get("report_path"),
        )

    injection_results: list[dict[str, Any]] = []
    for check in injection_checks:
        manifest_file = Path(manifest_path) if manifest_path is not None else None
        if manifest_file is None:
            continue
        error_path = (manifest_file.parent / check["error_report"]).resolve()
        expected = check["expected_code"]
        try:
            error_payload = json.loads(error_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            return _failure("E_VERIFY", check.get("name", "injection"), f"cannot read injection report: {exc}", output_path)
        actual = error_payload.get("code")
        if actual != expected:
            return _failure(
                "E_VERIFY",
                check.get("name", "injection"),
                "injection returned an unexpected business code",
                output_path,
                details={"expected": expected, "actual": actual},
                report_path=str(error_path),
            )
        events_path = error_path.with_name("events.jsonl")
        if not events_path.exists():
            return _failure("E_VERIFY", check.get("name", "injection"), "structured event log is missing", output_path, report_path=str(events_path))
        injection_results.append(
            {
                "name": check.get("name"),
                "expected_code": expected,
                "actual_code": actual,
                "error_report": str(error_path),
            }
        )

    result = {
        "schema_version": "1.0",
        "status": "OK",
        "checked_reports": len(verified_reports),
        "reports": verified_reports,
        "injection_checks": injection_results,
        "checks": [
            "partition digest recomputed from canonical source rows",
            "output digest recomputed from canonical row JSON",
            "fixed aggregate recomputed from row aggregate inputs",
            "error bounds and dominant segment recomputed",
            "row attribution recomputed from the global canonical sequence",
            "same-scale attribution compared across partition counts",
            "injection business codes checked",
        ],
    }
    if output_path is not None:
        _write_json(Path(output_path), result)
    return result


def _verify_single_report(
    dataset: Dataset,
    query: Query,
    report_path: Path,
    report: dict[str, Any],
) -> dict[str, Any] | None:
    try:
        fixed = parse_scale_factor(report["config"]["scale_factor"])
        partitions = parse_partition_count(report["config"]["partitions"])
    except (KeyError, EngineError) as exc:
        return _deviation("E_VERIFY", "config", f"invalid report configuration: {exc}")

    left_table = dataset.table(query.left_table)
    right_table = dataset.table(query.right_table)
    expected_fingerprints = {table.name: _table_fingerprint(table) for table in dataset.tables}
    if report.get("config", {}).get("table_fingerprints") != expected_fingerprints:
        return _deviation("E_VERIFY", "table_fingerprints", "source table fingerprints do not match the report")
    expected_query_summary = _query_summary(query)
    if report.get("query_summary") != expected_query_summary:
        return _deviation("E_VERIFY", "query_summary", "query summary differs from the source query file")
    if report.get("config", {}).get("query_fingerprint") != digest_bytes(report.get("query_summary")):
        return _deviation("E_VERIFY", "query_fingerprint", "query fingerprint is not self-consistent")

    shuffle = shuffle_dataset(dataset, query, fixed, partitions)
    report_partitions = report.get("shuffle", {}).get("partition_statistics", [])
    if len(report_partitions) != partitions:
        return _deviation("E_VERIFY", "shuffle.partition_statistics", "partition count does not match the report configuration")
    for expected, actual in zip(shuffle.partitions, report_partitions):
        for field in ("left_rows", "right_rows", "left_null_keys", "right_null_keys", "left_digest", "right_digest", "combined_digest"):
            expected_value = {
                "left_rows": len(expected.left),
                "right_rows": len(expected.right),
                "left_null_keys": expected.left_null_keys,
                "right_null_keys": expected.right_null_keys,
                "left_digest": expected.left_digest,
                "right_digest": expected.right_digest,
                "combined_digest": expected.combined_digest,
            }[field]
            if actual.get(field) != expected_value:
                return _deviation(
                    "E_VERIFY",
                    f"shuffle.partition[{expected.partition}].{field}",
                    "recomputed Shuffle partition evidence differs from the report",
                    {"expected": expected_value, "actual": actual.get(field)},
                )
    if report.get("shuffle", {}).get("global_digest") != shuffle.global_digest:
        return _deviation("E_VERIFY", "shuffle.global_digest", "global Shuffle digest differs from recomputation")

    rows = report.get("rows")
    if not isinstance(rows, list):
        return _deviation("E_VERIFY", "rows", "rows must be an array")
    if report.get("determinism", {}).get("output_digest") != digest_bytes(rows):
        return _deviation("E_VERIFY", "determinism.output_digest", "output digest is not the digest of the serialized rows")
    if report.get("determinism", {}).get("row_count") != len(rows):
        return _deviation("E_VERIFY", "determinism.row_count", "row count disagrees with the rows array")

    row_ids: set[str] = set()
    expected_order: list[tuple[int, int, int]] = []
    for index, row in enumerate(rows):
        row_id = row.get("row_id")
        if not isinstance(row_id, str) or row_id in row_ids:
            return _deviation("E_VERIFY", f"rows[{index}].row_id", "row identifiers must be present and unique")
        row_ids.add(row_id)
        expected_order.append(
            (
                int(row["join_key"]["fixed_lattice"]),
                int(row["left_row_index"]),
                int(row["right_row_index"]),
            )
        )
        for name, value in row.get("projected", {}).items():
            if value.get("type") == "float" and not value.get("null"):
                expected_lattice = fixed.quantize_float(float(value["reference"]))
                if value.get("fixed_lattice") != expected_lattice:
                    return _deviation(
                        "E_VERIFY",
                        f"rows[{index}].projected.{name}.fixed_lattice",
                        "fixed projection does not match floor(reference*S)",
                        {"expected": expected_lattice, "actual": value.get("fixed_lattice")},
                    )
                if value.get("fixed_value") != fixed.to_real(expected_lattice):
                    return _deviation(
                        "E_VERIFY",
                        f"rows[{index}].projected.{name}.fixed_value",
                        "fixed real value does not match the integer lattice value",
                    )
    if expected_order != sorted(expected_order):
        return _deviation("E_VERIFY", "rows", "rows are not in canonical join-key order")

    row_deviation = _verify_rows_against_sources(dataset, query, fixed, partitions, rows)
    if row_deviation is not None:
        return row_deviation

    aggregate_deviation = _verify_aggregate(report, fixed, query, dataset, rows)
    if aggregate_deviation is not None:
        return aggregate_deviation
    partition_aggregate_deviation = _verify_partition_aggregates(report, fixed, query, dataset, rows)
    if partition_aggregate_deviation is not None:
        return partition_aggregate_deviation

    join_stats = report.get("join", {}).get("partition_statistics", [])
    if len(join_stats) != partitions:
        return _deviation("E_VERIFY", "join.partition_statistics", "join statistics partition count differs")
    if sum(item.get("matched_pair_count", 0) for item in join_stats) != len(rows):
        return _deviation("E_VERIFY", "join.partition_statistics.matched_pair_count", "partition match counts do not add up to the output row count")
    collision_list = report.get("join", {}).get("quantized_reference_collisions", [])
    if sum(item.get("quantized_reference_collision_count", 0) for item in join_stats) != len(collision_list):
        return _deviation("E_VERIFY", "join.quantized_reference_collisions", "collision list length differs from partition counters")
    join_deviation = _verify_expected_join(dataset, query, fixed, partitions, shuffle, rows, collision_list, join_stats)
    if join_deviation is not None:
        return join_deviation

    attribution_deviation = _verify_attribution(rows, report, fixed, query, dataset)
    if attribution_deviation is not None:
        return attribution_deviation
    propagation_deviation = _verify_error_propagation(report, rows, dataset, query)
    if propagation_deviation is not None:
        return propagation_deviation
    return None


def _verify_aggregate(
    report: dict[str, Any],
    fixed: FixedContext,
    query: Query,
    dataset: Dataset,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    aggregate_type = dataset.table(query.left_table if query.aggregate.side == "left" else query.right_table).column(query.aggregate.column).type
    nonnull = [row["aggregate_input"] for row in rows if row.get("aggregate_input") is not None]
    nonnull_count = len(nonnull)
    total = len(rows)
    sum_fixed = sum(int(value["fixed_lattice"]) for value in nonnull)
    mean_fixed = _truncate_division(sum_fixed, nonnull_count) if nonnull_count else None
    expected = {
        "count_total": total,
        "count_non_null": nonnull_count,
        "count_null": total - nonnull_count,
        "sum_fixed_lattice": sum_fixed,
        "sum_fixed_value": fixed.to_real(sum_fixed),
        "sum_error_bound_lattice": nonnull_count if aggregate_type == "float" else 0,
        "mean_fixed_lattice": mean_fixed,
        "mean_fixed_value": fixed.to_real(mean_fixed) if mean_fixed is not None else None,
        "mean_error_bound_lattice": (nonnull_count + 1) if nonnull_count and aggregate_type == "float" else (1 if nonnull_count else 0),
    }
    actual = report.get("aggregate", {})
    for field, expected_value in expected.items():
        if actual.get(field) != expected_value:
            return _deviation(
                "E_VERIFY",
                f"aggregate.{field}",
                "fixed aggregate differs from recomputation",
                {"expected": expected_value, "actual": actual.get(field)},
            )
    if actual.get("source") != {"side": query.aggregate.side, "column": query.aggregate.column, "type": aggregate_type}:
        return _deviation("E_VERIFY", "aggregate.source", "aggregate source metadata differs from the query")
    return None


def _verify_rows_against_sources(
    dataset: Dataset,
    query: Query,
    fixed: FixedContext,
    partitions: int,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    left_table = dataset.table(query.left_table)
    right_table = dataset.table(query.right_table)
    mode = make_join_key_mode(left_table, right_table, query)
    aggregate_table = left_table if query.aggregate.side == "left" else right_table
    aggregate_column = aggregate_table.column(query.aggregate.column)
    projection_by_name = {item.name: item for item in query.projection}

    for index, row in enumerate(rows):
        if row.get("output_sequence") != index + 1:
            return _deviation("E_VERIFY", f"rows[{index}].output_sequence", "row sequence is not the canonical global sequence")
        left_index = row.get("left_row_index")
        right_index = row.get("right_row_index")
        if not isinstance(left_index, int) or not (1 <= left_index <= left_table.row_count):
            return _deviation("E_VERIFY", f"rows[{index}].left_row_index", "left row index is out of range")
        if not isinstance(right_index, int) or not (1 <= right_index <= right_table.row_count):
            return _deviation("E_VERIFY", f"rows[{index}].right_row_index", "right row index is out of range")
        left_row = left_table.rows[left_index - 1]
        right_row = right_table.rows[right_index - 1]

        left_key = fixed_join_key(left_table, query.join_key_left, left_row, mode, fixed)
        right_key = fixed_join_key(right_table, query.join_key_right, right_row, mode, fixed)
        if left_key is None or right_key is None or left_key != right_key:
            return _deviation("E_VERIFY", f"rows[{index}].join_key", "reported pair does not satisfy fixed-point key equality")
        if row["join_key"].get("fixed_lattice") != left_key:
            return _deviation("E_VERIFY", f"rows[{index}].join_key.fixed_lattice", "join-key lattice value differs from source rows")
        if row["join_key"].get("left_reference") != left_row[query.join_key_left]:
            return _deviation("E_VERIFY", f"rows[{index}].join_key.left_reference", "left join-key reference differs from its source row")
        if row["join_key"].get("right_reference") != right_row[query.join_key_right]:
            return _deviation("E_VERIFY", f"rows[{index}].join_key.right_reference", "right join-key reference differs from its source row")
        expected_collision = bool(mode.uses_float and left_row[query.join_key_left] != right_row[query.join_key_right])
        if row["join_key"].get("reference_collision") != expected_collision:
            return _deviation("E_VERIFY", f"rows[{index}].join_key.reference_collision", "collision flag differs from the source references")
        expected_partition = partition_key(mode, left_key, partitions)
        if row.get("partition") != expected_partition:
            return _deviation("E_VERIFY", f"rows[{index}].partition", "row partition differs from the stable Shuffle rule")

        if not _row_passes_predicates(left_table, left_row, query.predicates.get("left", tuple()), fixed):
            return _deviation("E_VERIFY", f"rows[{index}].left_predicates", "left source row does not satisfy the fixed predicates")
        if not _row_passes_predicates(right_table, right_row, query.predicates.get("right", tuple()), fixed):
            return _deviation("E_VERIFY", f"rows[{index}].right_predicates", "right source row does not satisfy the fixed predicates")

        projected = row.get("projected")
        if not isinstance(projected, dict) or set(projected) != set(projection_by_name):
            return _deviation("E_VERIFY", f"rows[{index}].projected", "projected aliases do not match the query")
        for name, item in projection_by_name.items():
            table = left_table if item.side == "left" else right_table
            source_row = left_row if item.side == "left" else right_row
            source_value = source_row[item.column]
            actual = projected[name]
            if actual.get("type") != table.column(item.column).type:
                return _deviation("E_VERIFY", f"rows[{index}].projected.{name}.type", "projection type differs from the source schema")
            if source_value is None:
                if actual.get("reference") is not None or actual.get("fixed_lattice") is not None:
                    return _deviation("E_VERIFY", f"rows[{index}].projected.{name}", "null projection is not represented as null")
                continue
            if actual.get("reference") != source_value:
                return _deviation("E_VERIFY", f"rows[{index}].projected.{name}.reference", "projection reference differs from its source row")
            expected_fixed = fixed.quantize_float(float(source_value)) if table.column(item.column).type == "float" else int(source_value)
            if actual.get("fixed_lattice") != expected_fixed:
                return _deviation("E_VERIFY", f"rows[{index}].projected.{name}.fixed_lattice", "projection fixed value differs from the quantization rule")

        aggregate_source_row = left_row if query.aggregate.side == "left" else right_row
        aggregate_source_value = aggregate_source_row[query.aggregate.column]
        aggregate_input = row.get("aggregate_input")
        if aggregate_source_value is None:
            if aggregate_input is not None:
                return _deviation("E_VERIFY", f"rows[{index}].aggregate_input", "null aggregate input must remain null")
        else:
            expected_aggregate_fixed = (
                fixed.quantize_float(float(aggregate_source_value))
                if aggregate_column.type == "float"
                else int(aggregate_source_value) * fixed.scale_factor
            )
            if aggregate_input is None or aggregate_input.get("fixed_lattice") != expected_aggregate_fixed:
                return _deviation("E_VERIFY", f"rows[{index}].aggregate_input.fixed_lattice", "aggregate input differs from source quantization")
    return None


def _verify_expected_join(
    dataset: Dataset,
    query: Query,
    fixed: FixedContext,
    partitions: int,
    shuffle: Any,
    rows: list[dict[str, Any]],
    collision_list: list[dict[str, Any]],
    join_stats: list[dict[str, Any]],
) -> dict[str, Any] | None:
    left_table = dataset.table(query.left_table)
    right_table = dataset.table(query.right_table)
    mode = make_join_key_mode(left_table, right_table, query)
    expected_pairs: set[tuple[int, int, int]] = set()
    expected_collisions: set[tuple[int, int, int]] = set()

    for partition in shuffle.partitions:
        left_rows = [(row_index, row) for row_index, row in partition.left if _row_passes_predicates(left_table, row, query.predicates.get("left", tuple()), fixed)]
        right_rows = [(row_index, row) for row_index, row in partition.right if _row_passes_predicates(right_table, row, query.predicates.get("right", tuple()), fixed)]
        left_by_key: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        right_by_key: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        for row_index, row in left_rows:
            key = fixed_join_key(left_table, query.join_key_left, row, mode, fixed)
            if key is not None:
                left_by_key.setdefault(key, []).append((row_index, row))
        for row_index, row in right_rows:
            key = fixed_join_key(right_table, query.join_key_right, row, mode, fixed)
            if key is not None:
                right_by_key.setdefault(key, []).append((row_index, row))
        for key in set(left_by_key) & set(right_by_key):
            for left_index, left_row in left_by_key[key]:
                for right_index, right_row in right_by_key[key]:
                    expected_pairs.add((partition.partition, left_index, right_index))
                    if mode.uses_float and left_row[query.join_key_left] != right_row[query.join_key_right]:
                        expected_collisions.add((partition.partition, left_index, right_index))

        stats = next((item for item in join_stats if item.get("partition") == partition.partition), None)
        if stats is None:
            return _deviation("E_VERIFY", f"join.partition[{partition.partition}]", "partition join statistics are missing")
        build_rows = right_rows if query.build_side == "right" else left_rows
        expected_build_entries = sum(
            1
            for _, row in build_rows
            if fixed_join_key(
                right_table if query.build_side == "right" else left_table,
                query.join_key_right if query.build_side == "right" else query.join_key_left,
                row,
                mode,
                fixed,
            )
            is not None
        )
        expected_capacity = capacity_for_rows(expected_build_entries)
        if stats.get("bucket_capacity") != expected_capacity:
            return _deviation(
                "E_VERIFY",
                f"join.partition[{partition.partition}].bucket_capacity",
                "bucket capacity differs from the declared row-based derivation",
                {"expected": expected_capacity, "actual": stats.get("bucket_capacity")},
            )

    reported_pairs = {(row["partition"], row["left_row_index"], row["right_row_index"]) for row in rows}
    if reported_pairs != expected_pairs:
        missing = sorted(expected_pairs - reported_pairs)[:10]
        extra = sorted(reported_pairs - expected_pairs)[:10]
        return _deviation(
            "E_VERIFY",
            "join.pairs",
            "fixed-point join result set differs from independent recomputation",
            {"missing": missing, "extra": extra},
        )
    reported_collisions = {
        (item["partition"], item["left_row_index"], item["right_row_index"])
        for item in collision_list
    }
    if reported_collisions != expected_collisions:
        return _deviation(
            "E_VERIFY",
            "join.quantized_reference_collisions",
            "quantized collision set differs from independent recomputation",
            {
                "missing": sorted(expected_collisions - reported_collisions)[:10],
                "extra": sorted(reported_collisions - expected_collisions)[:10],
            },
        )
    return None


def _row_passes_predicates(table: Any, row: dict[str, Any], predicates: tuple[Any, ...], fixed: FixedContext) -> bool:
    for predicate in predicates:
        value = row[predicate.column]
        if value is None:
            return False
        if table.column(predicate.column).type == "float":
            left_fixed = fixed.quantize_float(float(value))
            right_fixed = fixed.quantize_float(float(predicate.value))
        else:
            left_fixed = int(value)
            right_fixed = int(predicate.value)
        if not fixed_comparison(left_fixed, predicate.op, right_fixed):
            return False
    return True


def _verify_partition_aggregates(
    report: dict[str, Any],
    fixed: FixedContext,
    query: Query,
    dataset: Dataset,
    rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    aggregate_type = dataset.table(query.left_table if query.aggregate.side == "left" else query.right_table).column(query.aggregate.column).type
    partitions = report["config"]["partitions"]
    for stats in report.get("join", {}).get("partition_statistics", []):
        partition_rows = [row for row in rows if row.get("partition") == stats.get("partition")]
        nonnull = [row["aggregate_input"] for row in partition_rows if row.get("aggregate_input") is not None]
        sum_fixed = sum(int(value["fixed_lattice"]) for value in nonnull)
        nonnull_count = len(nonnull)
        expected = {
            "count_total": len(partition_rows),
            "count_non_null": nonnull_count,
            "count_null": len(partition_rows) - nonnull_count,
            "sum_fixed_lattice": sum_fixed,
            "mean_fixed_lattice": _truncate_division(sum_fixed, nonnull_count) if nonnull_count else None,
            "sum_error_bound_lattice": nonnull_count if aggregate_type == "float" else 0,
            "mean_error_bound_lattice": (nonnull_count + 1) if nonnull_count and aggregate_type == "float" else (1 if nonnull_count else 0),
        }
        actual = stats.get("aggregate", {})
        for field, expected_value in expected.items():
            if actual.get(field) != expected_value:
                return _deviation(
                    "E_VERIFY",
                    f"partition[{stats.get('partition')}].aggregate.{field}",
                    "partition aggregate differs from recomputation",
                    {"expected": expected_value, "actual": actual.get(field)},
                )
    if sum(item.get("aggregate", {}).get("count_total", 0) for item in report.get("join", {}).get("partition_statistics", [])) != len(rows):
        return _deviation("E_VERIFY", "partition.aggregate.count_total", "partition counts do not add up to the global row count")
    if partitions != len(report.get("join", {}).get("partition_statistics", [])):
        return _deviation("E_VERIFY", "partition.aggregate", "partition statistics are incomplete")
    return None


def _verify_attribution(
    rows: list[dict[str, Any]],
    report: dict[str, Any],
    fixed: FixedContext,
    query: Query,
    dataset: Dataset,
) -> dict[str, Any] | None:
    aggregate_type = dataset.table(query.left_table if query.aggregate.side == "left" else query.right_table).column(query.aggregate.column).type
    key_mode = make_join_key_mode(dataset.table(query.left_table), dataset.table(query.right_table), query)
    prefix = 0
    for index, row in enumerate(rows):
        if row.get("aggregate_input") is not None:
            prefix += 1 if aggregate_type == "float" else 0
        projection_float_count = sum(
            1 for value in row.get("projected", {}).values() if value.get("type") == "float" and not value.get("null")
        )
        quantization_bound = key_mode.left_error_bound + key_mode.right_error_bound + projection_float_count
        if prefix == 0:
            expected_primary = "quantization"
        elif quantization_bound == 0:
            expected_primary = "aggregation"
        elif quantization_bound > prefix:
            expected_primary = "quantization"
        elif prefix > quantization_bound:
            expected_primary = "aggregation"
        else:
            expected_primary = "both"
        actual = row.get("error_attribution", {})
        expected = {
            "primary_source": expected_primary,
            "join_key_quantization_bound_lattice": key_mode.left_error_bound + key_mode.right_error_bound,
            "projection_quantization_bound_lattice": projection_float_count,
            "quantization_bound_lattice": quantization_bound,
            "aggregation_prefix_bound_lattice": prefix,
            "combined_bound_lattice": quantization_bound + prefix,
        }
        for field, expected_value in expected.items():
            if actual.get(field) != expected_value:
                return _deviation(
                    "E_ATTR",
                    f"rows[{index}].error_attribution.{field}",
                    "row attribution differs from the partition-independent recomputation",
                    {"row_id": row.get("row_id"), "expected": expected_value, "actual": actual.get(field)},
                )
    return None


def _verify_error_propagation(
    report: dict[str, Any],
    rows: list[dict[str, Any]],
    dataset: Dataset,
    query: Query,
) -> dict[str, Any] | None:
    propagation = report.get("error_propagation", {})
    segments = propagation.get("segments", [])
    by_name = {segment.get("segment"): segment for segment in segments}
    if set(by_name) != {"shuffle", "bucket_probe", "aggregation"}:
        return _deviation("E_VERIFY", "error_propagation.segments", "three propagation segments are required")
    for name, segment in by_name.items():
        if not isinstance(segment.get("bound_lattice"), int) or segment["bound_lattice"] < 0:
            return _deviation("E_VERIFY", f"error_propagation.{name}.bound_lattice", "error bound must be a non-negative integer")
    join_stats = report.get("join", {}).get("partition_statistics", [])
    collision_count = sum(item.get("quantized_reference_collision_count", 0) for item in join_stats)
    predicate_mismatches = sum(item.get("predicate_boundary_mismatch_count", 0) for item in join_stats)
    predicate_affected_rows = sum(item.get("predicate_boundary_affected_row_count", 0) for item in join_stats)
    mode = make_join_key_mode(dataset.table(query.left_table), dataset.table(query.right_table), query)
    expected_shuffle_bound = len(rows) * (mode.left_error_bound + mode.right_error_bound)
    if by_name["shuffle"]["bound_lattice"] != expected_shuffle_bound:
        return _deviation(
            "E_VERIFY",
            "error_propagation.shuffle.bound_lattice",
            "shuffle segment bound differs from float join-key sides times output rows",
            {"expected": expected_shuffle_bound, "actual": by_name["shuffle"]["bound_lattice"]},
        )
    if by_name["bucket_probe"]["bound_lattice"] != collision_count + predicate_mismatches:
        return _deviation("E_VERIFY", "error_propagation.bucket_probe.bound_lattice", "bucket-probe bound is not collision plus predicate boundary contributions")
    if by_name["aggregation"]["bound_lattice"] != report["aggregate"]["sum_error_bound_lattice"]:
        return _deviation("E_VERIFY", "error_propagation.aggregation.bound_lattice", "aggregation segment differs from the global sum bound")
    total = sum(segment["bound_lattice"] for segment in segments)
    if propagation.get("total_bound_lattice") != total:
        return _deviation("E_VERIFY", "error_propagation.total_bound_lattice", "segment total differs from the reported total")
    for segment in segments:
        expected_share = round(segment["bound_lattice"] / total, 6) if total else 0.0
        if segment.get("bound_share") != expected_share:
            return _deviation("E_VERIFY", f"error_propagation.{segment['segment']}.bound_share", "segment share is not self-consistent")
    expected_bucket_affected = collision_count + predicate_affected_rows
    expected_bucket_denominator = len(rows) + predicate_affected_rows
    if by_name["bucket_probe"].get("affected_rows") != expected_bucket_affected:
        return _deviation("E_VERIFY", "error_propagation.bucket_probe.affected_rows", "bucket-probe affected-row count is inconsistent")
    if by_name["bucket_probe"].get("denominator_rows") != expected_bucket_denominator:
        return _deviation("E_VERIFY", "error_propagation.bucket_probe.denominator_rows", "bucket-probe denominator is inconsistent")
    if by_name["aggregation"].get("affected_rows") != report["aggregate"]["count_non_null"]:
        return _deviation("E_VERIFY", "error_propagation.aggregation.affected_rows", "aggregation affected rows differ from non-null aggregate inputs")
    if by_name["aggregation"].get("denominator_rows") != len(rows):
        return _deviation("E_VERIFY", "error_propagation.aggregation.denominator_rows", "aggregation denominator differs from output row count")
    top = max(segments, key=lambda item: item["bound_lattice"])
    expected_dominant = "none" if total == 0 else (top["segment"] if top["bound_lattice"] / total >= 0.5 else "mixed")
    if propagation.get("dominant_segment") != expected_dominant:
        return _deviation("E_VERIFY", "error_propagation.dominant_segment", "dominant segment does not follow the declared threshold rule")
    expected_dominant_share = round(top["bound_lattice"] / total, 6) if total else 0.0
    if propagation.get("dominant_share") != expected_dominant_share:
        return _deviation("E_VERIFY", "error_propagation.dominant_share", "dominant share differs from the recomputed segment bounds")
    if report.get("quantization", {}).get("report_values", {}).get("sum_error_bound_lattice") != report["aggregate"]["sum_error_bound_lattice"]:
        return _deviation("E_VERIFY", "quantization.report_values.sum_error_bound_lattice", "quantization report does not echo the global sum bound")
    if report.get("quantization", {}).get("report_values", {}).get("mean_error_bound_lattice") != report["aggregate"]["mean_error_bound_lattice"]:
        return _deviation("E_VERIFY", "quantization.report_values.mean_error_bound_lattice", "quantization report does not echo the global mean bound")
    if len(rows) != report.get("aggregate", {}).get("count_total"):
        return _deviation("E_VERIFY", "aggregate.count_total", "aggregate count differs from the row count")
    return None


def _verify_cross_report_attribution(
    contexts: list[tuple[Path, dict[str, Any], FixedContext, int]],
) -> dict[str, Any] | None:
    by_scale: dict[int, list[tuple[Path, dict[str, Any], int]]] = {}
    for report_path, report, fixed, partitions in contexts:
        by_scale.setdefault(fixed.scale_factor, []).append((report_path, report, partitions))
    for scale, group in by_scale.items():
        if len(group) < 2:
            continue
        canonical_attribution: dict[str, dict[str, Any]] = {}
        canonical_row_ids: set[str] | None = None
        for _, report, _ in group:
            row_ids = {row["row_id"] for row in report["rows"]}
            if canonical_row_ids is None:
                canonical_row_ids = row_ids
            elif row_ids != canonical_row_ids:
                return {
                    "code": "E_VERIFY",
                    "item": "cross_report.rows",
                    "message": "same-scale reports produced different matched row sets",
                    "details": {
                        "scale_factor": scale,
                        "missing": sorted((canonical_row_ids or set()) - row_ids)[:10],
                        "extra": sorted(row_ids - (canonical_row_ids or set()))[:10],
                    },
                }
            for row in report["rows"]:
                attribution = row["error_attribution"]
                comparable = {
                    "primary_source": attribution["primary_source"],
                    "quantization_bound_lattice": attribution["quantization_bound_lattice"],
                    "aggregation_prefix_bound_lattice": attribution["aggregation_prefix_bound_lattice"],
                }
                previous = canonical_attribution.get(row["row_id"])
                if previous is not None and previous != comparable:
                    return {
                        "code": "E_ATTR",
                        "item": f"cross_report.attribution.{row['row_id']}",
                        "message": "the same row has different attribution across partition counts",
                        "details": {"previous": previous, "current": comparable},
                        "report_path": str(report_path),
                    }
                canonical_attribution[row["row_id"]] = comparable
    return None


def _table_fingerprint(table: Any) -> str:
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


def _truncate_division(numerator: int, denominator: int) -> int:
    quotient = abs(numerator) // abs(denominator)
    return -quotient if (numerator < 0) != (denominator < 0) else quotient


def _deviation(code: str, item: str, message: str, details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "item": item, "message": message, "details": details or {}}


def _failure(
    code: str,
    item: str,
    message: str,
    output_path: str | Path | None,
    details: dict[str, Any] | None = None,
    report_path: str | None = None,
) -> dict[str, Any]:
    result = {
        "schema_version": "1.0",
        "status": "ERROR",
        "code": code,
        "first_deviation": {
            "item": item,
            "message": message,
            "details": details or {},
            "report_path": report_path,
        },
    }
    if output_path is not None:
        _write_json(Path(output_path), result)
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
