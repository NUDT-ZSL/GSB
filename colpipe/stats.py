"""Statistics snapshots and integer fixed-point selectivity estimation."""

from __future__ import annotations

from collections import Counter
from typing import Any, Iterable

from .catalog import Column, Dataset, Table
from .query import BoundRef, Predicate
from .util import canonical_json, scalar_key, sha256_json

SCALE = 1_000_000
FREQUENCY_LIMIT = 256


def _numpy_capability() -> dict[str, Any]:
    try:
        import numpy  # type: ignore

        return {"available": True, "version": str(numpy.__version__)}
    except Exception:
        return {"available": False, "version": None}


def _numeric_min_max(column: Column) -> tuple[Any, Any]:
    present = [value for value in column.values if value is not None]
    if not present:
        return None, None
    try:
        import numpy as np  # type: ignore

        dtype = np.int64 if column.type == "int64" else np.float64
        array = np.asarray(present, dtype=dtype)
        return array.min().item(), array.max().item()
    except Exception:
        return min(present), max(present)


def build_statistics(dataset: Dataset) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for table in dataset.tables:
        column_stats: dict[str, Any] = {}
        for column in table.columns:
            counter = Counter(value for value in column.values if value is not None)
            frequencies = [
                {"value": value, "count": counter[value]}
                for value in sorted(counter, key=scalar_key)[:FREQUENCY_LIMIT]
            ]
            if column.type in {"int64", "float64"}:
                minimum, maximum = _numeric_min_max(column)
            else:
                present = [value for value in column.values if value is not None]
                minimum = min(present) if present else None
                maximum = max(present) if present else None
            column_stats[column.name] = {
                "type": column.type,
                "null_count": column.null_count,
                "non_null_count": table.row_count - column.null_count,
                "distinct_count": len(counter),
                "min": minimum,
                "max": maximum,
                "frequencies": frequencies,
                "frequency_truncated": len(counter) > FREQUENCY_LIMIT,
            }
        tables[table.name] = {
            "schema_version": table.schema_version,
            "row_count": table.row_count,
            "data_digest": sha256_json(table.data_dict()),
            "columns": column_stats,
        }
    core: dict[str, Any] = {
        "method": "deterministic-frequency-and-range-v1",
        "fixed_point_scale": SCALE,
        "dataset_data_digest": dataset.data_digest,
        "schema_fingerprint": dataset.schema_fingerprint,
        "tables": tables,
    }
    payload: dict[str, Any] = dict(core)
    payload["runtime"] = {"numpy": _numpy_capability()}
    payload["statistics_sha256"] = sha256_json(core)
    return payload


def _clamp_ppm(value: int) -> int:
    return max(0, min(SCALE, int(value)))


def _eq_ppm(column_stats: dict[str, Any], row_count: int, value: Any) -> int:
    if row_count <= 0:
        return 0
    frequencies = column_stats.get("frequencies", [])
    match = next((item["count"] for item in frequencies if item["value"] == value), None)
    if match is not None:
        exact = match * SCALE // row_count
        if column_stats.get("frequency_truncated") and match * SCALE % row_count:
            exact += 1
        return _clamp_ppm(exact)
    non_null = int(column_stats.get("non_null_count", 0))
    distinct = max(1, int(column_stats.get("distinct_count", 1)))
    return _clamp_ppm((non_null * SCALE) // (row_count * distinct))


def _range_ppm(column_stats: dict[str, Any], row_count: int, op: str, value: Any) -> int:
    non_null = int(column_stats.get("non_null_count", 0))
    if row_count <= 0 or non_null <= 0:
        return 0
    minimum = column_stats.get("min")
    maximum = column_stats.get("max")
    if minimum is None or maximum is None:
        return 0
    if op in {"=", "!="}:
        eq = _eq_ppm(column_stats, row_count, value)
        if op == "=":
            return eq
        return _clamp_ppm((non_null * SCALE // row_count) - eq)
    try:
        if value < minimum:
            fraction = 0.0 if op in {"<", "<="} else 1.0
        elif value > maximum:
            fraction = 1.0 if op in {"<", "<="} else 0.0
        else:
            if isinstance(value, (int, float)) and maximum != minimum:
                position = (float(value) - float(minimum)) / (float(maximum) - float(minimum))
            elif maximum == minimum:
                position = 0.5
            else:
                position = 0.5
            # Treat the domain as uniform.  Boundaries are intentionally simple
            # and deterministic; the report carries their uncertainty bound.
            if op == "<":
                fraction = position
            elif op == "<=":
                fraction = min(1.0, position + 1.0 / max(1, int(column_stats.get("distinct_count", 1))))
            elif op == ">":
                fraction = 1.0 - position
            else:
                fraction = max(0.0, 1.0 - position)
        return _clamp_ppm(round(fraction * non_null * SCALE / row_count))
    except (TypeError, ValueError):
        # Lexicographic domains without a numerical span use a conservative
        # half-domain estimate.
        fraction = 0.5
        if op in {"<", ">", "<=", ">="}:
            return _clamp_ppm(round(fraction * non_null * SCALE / row_count))
        return 0


def estimate_predicate(predicate: Predicate, table_stats: dict[str, Any]) -> dict[str, Any]:
    column_stats = table_stats["columns"][predicate.ref.column]
    row_count = int(table_stats["row_count"])
    null_count = int(column_stats["null_count"])
    op = predicate.op
    if op == "is_null":
        ppm = 0 if row_count == 0 else null_count * SCALE // row_count
    elif op == "is_not_null":
        ppm = 0 if row_count == 0 else (row_count - null_count) * SCALE // row_count
    elif op == "in":
        ppm = sum(_eq_ppm(column_stats, row_count, value) for value in predicate.values)
    elif op == "not_in":
        included = sum(_eq_ppm(column_stats, row_count, value) for value in predicate.values)
        ppm = (row_count - null_count) * SCALE // row_count - included if row_count else 0
    elif op == "between":
        low, high = predicate.values
        greater = _range_ppm(column_stats, row_count, ">=", low)
        greater_high = _range_ppm(column_stats, row_count, ">", high)
        ppm = max(0, greater - greater_high)
    elif op == "not_between":
        low, high = predicate.values
        greater = _range_ppm(column_stats, row_count, ">=", low)
        greater_high = _range_ppm(column_stats, row_count, ">", high)
        within = max(0, greater - greater_high)
        ppm = (row_count - null_count) * SCALE // row_count - within if row_count else 0
    else:
        ppm = _eq_ppm(column_stats, row_count, predicate.values[0]) if op == "=" else _range_ppm(
            column_stats, row_count, op, predicate.values[0]
        )
    ppm = _clamp_ppm(ppm)
    if row_count and op not in {"is_null"} and ppm == 0:
        ppm = 1
    return {
        "condition": predicate.to_dict(),
        "selectivity_ppm": ppm,
        "selectivity": ppm / SCALE,
    }


def estimate_scan(table: Table, predicates: Iterable[Predicate], statistics: dict[str, Any]) -> dict[str, Any]:
    table_stats = statistics["tables"][table.name]
    predicate_reports = [estimate_predicate(predicate, table_stats) for predicate in predicates]
    combined = SCALE
    for report in predicate_reports:
        combined = (combined * int(report["selectivity_ppm"]) + SCALE // 2) // SCALE
    combined = _clamp_ppm(combined)
    row_count = table.row_count
    estimated_rows = (row_count * combined + SCALE // 2) // SCALE
    # ppm rounding contributes at most n/(2*SCALE) rows, final row rounding at
    # most 0.5 row.  The integer ceiling is a conservative upper bound.
    quantization_error = 0 if row_count == 0 else (row_count + 3 * SCALE - 1) // (2 * SCALE)
    return {
        "row_count": row_count,
        "predicates": predicate_reports,
        "selectivity_ppm": combined,
        "selectivity": combined / SCALE,
        "estimated_output_rows": estimated_rows,
        "quantization_error_rows_upper_bound": quantization_error,
        "fixed_point_scale": SCALE,
    }


def estimate_scans(
    dataset: Dataset,
    optimized_plan: dict[str, Any],
    statistics: dict[str, Any],
    *,
    parallelism: int,
    min_rows_per_task: int,
) -> dict[str, Any]:
    """Estimate each scan and derive deterministic contiguous task ranges."""

    scan_nodes = collect_scan_nodes(optimized_plan)
    scan_reports: dict[str, Any] = {}
    task_order: list[str] = []
    total_tasks = 0
    for node in scan_nodes:
        alias = node["attrs"]["alias"]
        table_name = node["attrs"]["table"]
        table = dataset.table(table_name)
        predicates = [
            Predicate(
                BoundRef(
                    predicate["column"].split(".", 1)[0],
                    predicate["column"].split(".", 1)[1],
                    predicate["type"],
                ),
                predicate["op"],
                tuple(predicate["values"]),
            )
            for predicate in node["attrs"].get("pushed_filters", [])
        ]
        report = estimate_scan(table, predicates, statistics)
        estimated = int(report["estimated_output_rows"])
        task_count = min(parallelism, max(1, (estimated + min_rows_per_task - 1) // min_rows_per_task))
        row_count = table.row_count
        chunk_rows = (row_count + task_count - 1) // task_count if row_count else 0
        tasks: list[dict[str, Any]] = []
        for part in range(task_count):
            start = part * chunk_rows
            end = min(row_count, start + chunk_rows)
            tasks.append(
                {
                    "task_id": f"scan:{alias}:{part}",
                    "part": part,
                    "input_range": [start, end],
                    "input_rows": max(0, end - start),
                }
            )
            total_tasks += 1
            task_order.append(tasks[-1]["task_id"])
        scan_reports[alias] = {
            "alias": alias,
            "table": table_name,
            "estimate": report,
            "task_count": task_count,
            "rows_per_task_target": chunk_rows,
            "tasks": tasks,
        }
    payload: dict[str, Any] = {
        "fixed_point_scale": SCALE,
        "parallelism_budget": parallelism,
        "min_rows_per_task": min_rows_per_task,
        "split_policy": "task_count=min(parallelism,ceil(estimated_output_rows/min_rows_per_task)); contiguous ranges=ceil(row_count/task_count)",
        "scans": scan_reports,
        "task_order": task_order,
        "total_tasks": total_tasks,
    }
    payload["split_sha256"] = sha256_json(payload)
    return payload


def collect_scan_nodes(node: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if node.get("op") == "Scan":
        result.append(node)
    for child in node.get("children", []):
        result.extend(collect_scan_nodes(child))
    return result


def statistics_without_hash(statistics: dict[str, Any]) -> dict[str, Any]:
    copy = dict(statistics)
    copy.pop("statistics_sha256", None)
    copy.pop("runtime", None)
    return copy


def statistics_core_sha256(statistics: dict[str, Any]) -> str:
    return sha256_json(statistics_without_hash(statistics))
