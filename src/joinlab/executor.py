"""Plan-driven local executor with deterministic result materialization."""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

from .catalog import Catalog, ColumnDef, TableData
from .cost import Plan, PlanStep
from .errors import PlanError
from .query import ColumnRef, Literal, Predicate, Query
from .typesys import canonical_value, compare_values, json_dumps_stable, sort_key


@dataclass(frozen=True)
class ExecutionResult:
    query_id: str
    plan_id: str
    columns: tuple[ColumnRef, ...]
    rows: tuple[tuple[Any, ...], ...]
    encoded: bytes
    digest: str
    stats: tuple[dict[str, Any], ...]
    elapsed_total_ms: float
    stats_fingerprint: str
    attribution_chain: tuple[dict[str, Any], ...]
    estimated_rows: int
    timings_ms: tuple[tuple[str, float], ...] = ()

    @property
    def row_count(self) -> int:
        return len(self.rows)

    def to_dict(self, *, include_rows: bool = True) -> dict[str, Any]:
        return {
            "query_id": self.query_id,
            "plan_id": self.plan_id,
            "columns": [column.render() for column in self.columns],
            "row_count": self.row_count,
            "result_digest": self.digest,
            "stats_fingerprint": self.stats_fingerprint,
            "estimated_rows": self.estimated_rows,
            "stats": list(self.stats),
            "attribution_chain": list(self.attribution_chain),
            "timings": {
                "per_operator_ms": {operator_id: round(value, 6) for operator_id, value in self.timings_ms},
                "elapsed_total_ms": round(self.elapsed_total_ms, 6),
                "observational": True,
                "excluded_from_decisions": True,
                "excluded_from_stats_fingerprint": True,
            },
            **({"rows": [[canonical_value(value) for value in row] for row in self.rows]} if include_rows else {}),
        }


@dataclass(frozen=True)
class ConsistencyReport:
    consistent: bool
    selected_plan_id: str
    fallback_plan_id: str
    selected_digest: str
    fallback_digest: str
    selected_rows: int
    fallback_rows: int
    distinct_plans: bool
    first_difference: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "consistent": self.consistent,
            "selected_plan_id": self.selected_plan_id,
            "fallback_plan_id": self.fallback_plan_id,
            "selected_digest": self.selected_digest,
            "fallback_digest": self.fallback_digest,
            "selected_rows": self.selected_rows,
            "fallback_rows": self.fallback_rows,
            "distinct_plans": self.distinct_plans,
            "first_difference": self.first_difference,
        }


@dataclass(frozen=True)
class ExecutionBundle:
    selected: ExecutionResult
    fallback: ExecutionResult
    consistency: ConsistencyReport


def _column_type(catalog: Catalog, ref: ColumnRef) -> str:
    table = catalog.table(ref.table)
    column = table.column(ref.column) if table else None
    if column is None:
        raise PlanError(f"unknown column in plan: {ref.render()}", "execution.columns")
    return column.type


def _row_value(row: tuple[Any, ...], column_index: dict[tuple[str, str], int], ref: ColumnRef) -> Any:
    try:
        return row[column_index[ref.key]]
    except KeyError as exc:
        raise PlanError(f"column {ref.render()} is not available", "execution.columns") from exc


def _evaluate_predicates(
    row: tuple[Any, ...],
    column_index: dict[tuple[str, str], int],
    predicates: Iterable[Predicate],
    catalog: Catalog,
) -> tuple[bool, int]:
    matched = True
    comparisons = 0
    for predicate in predicates:
        left_value = _row_value(row, column_index, predicate.left)
        left_type = _column_type(catalog, predicate.left)
        if isinstance(predicate.right, ColumnRef):
            right_value = _row_value(row, column_index, predicate.right)
            right_type = _column_type(catalog, predicate.right)
        else:
            right_value = predicate.right.value
            right_type = predicate.right.type
        compared = compare_values(
            left_value,
            right_value,
            predicate.op,
            left_type,
            right_type,
            "execution.predicate",
        )
        comparisons += 1
        if not compared:
            matched = False
    return matched, comparisons


def _filter_rows(
    rows: tuple[tuple[Any, ...], ...],
    columns: tuple[ColumnRef, ...],
    predicates: tuple[Predicate, ...],
    catalog: Catalog,
) -> tuple[tuple[tuple[Any, ...], ...], int]:
    if not predicates:
        return rows, 0
    index = {ref.key: position for position, ref in enumerate(columns)}
    result = []
    comparisons = 0
    for row in rows:
        matched, count = _evaluate_predicates(row, index, predicates, catalog)
        comparisons += count
        if matched:
            result.append(row)
    return tuple(result), comparisons


def _hash_value(value: Any, type_name: str) -> Any:
    if value is None:
        return None
    if type_name in {"integer", "real"}:
        return Decimal(str(value))
    if type_name == "boolean":
        return bool(value)
    return str(value)


def _equality_specs(
    predicates: Iterable[Predicate],
    current_tables: set[str],
    right_table: str,
) -> list[tuple[ColumnRef, ColumnRef]]:
    specs: list[tuple[ColumnRef, ColumnRef]] = []
    for predicate in predicates:
        if predicate.op != "=" or not isinstance(predicate.right, ColumnRef):
            continue
        left, right = predicate.left, predicate.right
        if left.table in current_tables and right.table == right_table:
            specs.append((left, right))
        elif right.table in current_tables and left.table == right_table:
            specs.append((right, left))
    return specs


def _join_rows(
    left_rows: tuple[tuple[Any, ...], ...],
    left_columns: tuple[ColumnRef, ...],
    right_rows: tuple[tuple[Any, ...], ...],
    right_columns: tuple[ColumnRef, ...],
    step: PlanStep,
    catalog: Catalog,
) -> tuple[tuple[tuple[Any, ...], ...], int, int]:
    combined_columns = left_columns + right_columns
    index = {ref.key: position for position, ref in enumerate(combined_columns)}
    right_width = len(right_columns)
    null_right = (None,) * right_width
    output: list[tuple[Any, ...]] = []
    comparisons = 0
    match_pairs = 0

    if step.operator == "nested_loop":
        for left_row in left_rows:
            matched_any = False
            for right_row in right_rows:
                combined = left_row + right_row
                matched, count = _evaluate_predicates(combined, index, step.predicates, catalog)
                comparisons += count
                match_pairs += 1
                if matched:
                    matched_any = True
                    output.append(combined)
            if step.join_kind == "left" and not matched_any:
                output.append(left_row + null_right)
    elif step.operator == "hash":
        current_tables = {ref.table for ref in left_columns}
        specs = _equality_specs(step.predicates, current_tables, step.right_table)
        if not specs:
            raise PlanError("hash plan has no usable equality key", "execution.hash")
        right_index = {ref.key: position for position, ref in enumerate(right_columns)}
        left_index = {ref.key: position for position, ref in enumerate(left_columns)}
        buckets: dict[tuple[Any, ...], list[tuple[Any, ...]]] = {}
        for right_row in right_rows:
            key = tuple(
                _hash_value(
                    right_row[right_index[right_ref.key]],
                    _column_type(catalog, right_ref),
                )
                for _, right_ref in specs
            )
            if any(part is None for part in key):
                continue
            buckets.setdefault(key, []).append(right_row)
        for left_row in left_rows:
            key = tuple(
                _hash_value(
                    left_row[left_index[left_ref.key]],
                    _column_type(catalog, left_ref),
                )
                for left_ref, _ in specs
            )
            comparisons += 1  # one logical hash probe
            matched_any = False
            if not any(part is None for part in key):
                for right_row in buckets.get(key, ()):
                    combined = left_row + right_row
                    matched, count = _evaluate_predicates(combined, index, step.predicates, catalog)
                    comparisons += count
                    match_pairs += 1
                    if matched:
                        matched_any = True
                        output.append(combined)
            if step.join_kind == "left" and not matched_any:
                output.append(left_row + null_right)
    else:
        raise PlanError(f"unknown join operator {step.operator!r}", "execution.operator")
    return tuple(output), comparisons, match_pairs


def _stable_stat_fingerprint(stats: Iterable[dict[str, Any]]) -> str:
    stable = []
    for stat in stats:
        stable.append(
            {
                key: value
                for key, value in stat.items()
                if key not in {"elapsed_ms", "elapsed_seconds", "timing"}
            }
        )
    raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _attribution_chain(
    query: Query,
    plan: Plan,
    stats: tuple[dict[str, Any], ...],
    catalog: Catalog,
) -> tuple[dict[str, Any], ...]:
    by_id = {stat["operator_id"]: stat for stat in stats}
    chain: list[dict[str, Any]] = []
    for scan in plan.scans:
        stat = by_id.get(f"scan:{scan.table}")
        actual = stat["output_rows"] if stat else None
        causes: list[str] = []
        if actual is not None and actual != scan.output_rows:
            causes.append("filter_selectivity_estimate" if scan.filter_count else "sample_scale_view")
        chain.append(
            {
                "stage": f"scan:{scan.table}",
                "estimated_rows": scan.output_rows,
                "actual_rows": actual,
                "delta": None if actual is None else actual - scan.output_rows,
                "causes": causes,
            }
        )
    for step in plan.steps:
        stat = by_id.get(f"join:{step.index}:{step.right_table}")
        actual = stat["output_rows"] if stat else None
        causes = []
        if actual is not None and actual != step.output_rows:
            if step.join_kind == "left":
                causes.append("outer_join_padding_or_match_distribution")
            table = catalog.table(step.right_table)
            if table is not None and table.skew:
                causes.append("join_key_skew")
            causes.append("join_selectivity_estimate")
        chain.append(
            {
                "stage": f"join:{step.index}:{step.right_table}",
                "operator": step.operator,
                "estimated_rows": step.output_rows,
                "actual_rows": actual,
                "delta": None if actual is None else actual - step.output_rows,
                "causes": causes,
            }
        )
    post = by_id.get("post_filter")
    causes = []
    if post is not None and post["output_rows"] != plan.post_filter_output_rows:
        causes.append("where_selectivity_estimate")
    chain.append(
        {
            "stage": "post_filter",
            "estimated_rows": plan.post_filter_output_rows,
            "actual_rows": post["output_rows"] if post else None,
            "delta": None if post is None else post["output_rows"] - plan.post_filter_output_rows,
            "causes": causes,
        }
    )
    return tuple(chain)


def execute_query(
    query: Query,
    catalog: Catalog,
    plan: Plan,
    data_by_key: dict[str, TableData],
) -> ExecutionResult:
    started_total = time.perf_counter_ns()
    table_map = catalog.table_by_key
    stats: list[dict[str, Any]] = []
    timings: list[tuple[str, float]] = []
    scan_rows: dict[str, tuple[tuple[Any, ...], ...]] = {}
    scan_columns: dict[str, tuple[ColumnRef, ...]] = {}
    estimated_scan = {scan.table: scan.output_rows for scan in plan.scans}

    for table_key in plan.relation_order:
        table = table_map.get(table_key)
        data = data_by_key.get(table_key)
        if table is None or data is None:
            raise PlanError(f"missing table data for {table_key!r}", "execution.scan")
        columns = tuple(ColumnRef(table.key, column.key) for column in table.columns)
        predicates = query.filters_for(table_key)
        began = time.perf_counter_ns()
        filtered, comparisons = _filter_rows(data.rows, columns, predicates, catalog)
        elapsed = (time.perf_counter_ns() - began) / 1_000_000.0
        scan_rows[table_key] = filtered
        scan_columns[table_key] = columns
        operator_id = f"scan:{table_key}"
        stats.append(
            {
                "operator_id": operator_id,
                "operator": "scan",
                "table": table_key,
                "input_rows": len(data.rows),
                "output_rows": len(filtered),
                "comparisons": comparisons,
                "estimated_rows": estimated_scan.get(table_key),
            }
        )
        timings.append((operator_id, elapsed))

    current_table = plan.relation_order[0]
    current_rows = scan_rows[current_table]
    current_columns = scan_columns[current_table]
    current_tables = {current_table}

    for step in plan.steps:
        right_rows = scan_rows[step.right_table]
        right_columns = scan_columns[step.right_table]
        began = time.perf_counter_ns()
        current_rows, comparisons, _ = _join_rows(
            current_rows,
            current_columns,
            right_rows,
            right_columns,
            step,
            catalog,
        )
        elapsed = (time.perf_counter_ns() - began) / 1_000_000.0
        current_columns = current_columns + right_columns
        current_tables.add(step.right_table)
        operator_id = f"join:{step.index}:{step.right_table}"
        stats.append(
            {
                "operator_id": operator_id,
                "operator": "hash_join" if step.operator == "hash" else "nested_loop_join",
                "join_kind": step.join_kind,
                "right_table": step.right_table,
                "input_left_rows": step.input_left_rows,
                "input_right_rows": step.input_right_rows,
                "output_rows": len(current_rows),
                "comparisons": comparisons,
                "estimated_rows": step.output_rows,
            }
        )
        timings.append((operator_id, elapsed))

    # Post-join WHERE predicates are deliberately applied uniformly.  This is
    # required for LEFT JOIN correctness and gives INNER queries the same set
    # semantics regardless of where the parser could have pushed a predicate.
    began = time.perf_counter_ns()
    post_input_rows = len(current_rows)
    current_rows, post_comparisons = _filter_rows(current_rows, current_columns, query.post_filters, catalog)
    post_elapsed = (time.perf_counter_ns() - began) / 1_000_000.0
    stats.append(
        {
            "operator_id": "post_filter",
            "operator": "filter",
            "input_rows": post_input_rows,
            "output_rows": len(current_rows),
            "comparisons": post_comparisons,
            "filter_count": len(query.post_filters),
            "estimated_rows": plan.post_filter_output_rows,
        }
    )
    timings.append(("post_filter", post_elapsed))
    index = {ref.key: position for position, ref in enumerate(current_columns)}
    projection = query.projection
    try:
        projection_index = [index[ref.key] for ref in projection]
    except KeyError as exc:
        raise PlanError(f"projection column {exc.args[0]!r} is unavailable", "execution.projection") from exc
    projected = tuple(tuple(row[position] for position in projection_index) for row in current_rows)
    output_defs = query.output_columns(catalog)
    began = time.perf_counter_ns()
    sorted_rows = tuple(
        sorted(
            projected,
            key=lambda row: tuple(
                sort_key(row[index], output_defs[index].type)
                for index in range(len(output_defs))
            ),
        )
    )
    sort_elapsed = (time.perf_counter_ns() - began) / 1_000_000.0
    n = len(sorted_rows)
    sort_comparisons = 0 if n <= 1 else n * max(1, int(math.ceil(math.log2(n))))
    stats.append(
        {
            "operator_id": "canonical_sort",
            "operator": "sort",
            "input_rows": len(projected),
            "output_rows": n,
            "comparisons": sort_comparisons,
            "ordering": "all output columns ascending; NULL last",
            "estimated_rows": plan.post_filter_output_rows,
        }
    )
    timings.append(("canonical_sort", sort_elapsed))

    encoded_rows: list[str] = []
    for row in sorted_rows:
        encoded_rows.append(
            json.dumps(
                [canonical_value(value) for value in row],
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        )
    encoded = ("\n".join(encoded_rows) + ("\n" if encoded_rows else "")).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    total_elapsed = (time.perf_counter_ns() - started_total) / 1_000_000.0
    fingerprint = _stable_stat_fingerprint(stats)
    attribution = _attribution_chain(query, plan, tuple(stats), catalog)
    return ExecutionResult(
        query_id=query.query_id,
        plan_id=plan.plan_id,
        columns=projection,
        rows=sorted_rows,
        encoded=encoded,
        digest=digest,
        stats=tuple(stats),
        elapsed_total_ms=total_elapsed,
        stats_fingerprint=fingerprint,
        attribution_chain=attribution,
        estimated_rows=plan.post_filter_output_rows,
        timings_ms=tuple(timings),
    )


def compare_results(selected: ExecutionResult, fallback: ExecutionResult) -> ConsistencyReport:
    consistent = selected.encoded == fallback.encoded
    first_difference: dict[str, Any] | None = None
    if not consistent:
        limit = max(len(selected.rows), len(fallback.rows))
        for index in range(limit):
            selected_row = selected.rows[index] if index < len(selected.rows) else None
            fallback_row = fallback.rows[index] if index < len(fallback.rows) else None
            if selected_row == fallback_row:
                continue
            selected_text = (
                json.dumps([canonical_value(value) for value in selected_row], ensure_ascii=False, separators=(",", ":"))
                if selected_row is not None
                else None
            )
            fallback_text = (
                json.dumps([canonical_value(value) for value in fallback_row], ensure_ascii=False, separators=(",", ":"))
                if fallback_row is not None
                else None
            )
            first_difference = {
                "row_index": index,
                "selected_row_key": None
                if selected_text is None
                else hashlib.sha256(selected_text.encode("utf-8")).hexdigest()[:24],
                "fallback_row_key": None
                if fallback_text is None
                else hashlib.sha256(fallback_text.encode("utf-8")).hexdigest()[:24],
                "selected_row": selected_text,
                "fallback_row": fallback_text,
                "operator": "canonical_sort",
            }
            break
    return ConsistencyReport(
        consistent=consistent,
        selected_plan_id=selected.plan_id,
        fallback_plan_id=fallback.plan_id,
        selected_digest=selected.digest,
        fallback_digest=fallback.digest,
        selected_rows=selected.row_count,
        fallback_rows=fallback.row_count,
        distinct_plans=selected.plan_id != fallback.plan_id,
        first_difference=first_difference,
    )


def execute_and_verify(
    query: Query,
    catalog: Catalog,
    selected_plan: Plan,
    fallback_plan: Plan,
    data_by_key: dict[str, TableData],
) -> ExecutionBundle:
    selected = execute_query(query, catalog, selected_plan, data_by_key)
    fallback = execute_query(query, catalog, fallback_plan, data_by_key)
    report = compare_results(selected, fallback)
    if not report.consistent:
        raise PlanError(
            "selected and fallback plans produced different result bytes",
            "execution.consistency",
            consistency=report.to_dict(),
        )
    return ExecutionBundle(selected=selected, fallback=fallback, consistency=report)
