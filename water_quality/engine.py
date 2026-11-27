"""Partitioned predicate pushdown execution and incremental verification."""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .audit import compute_manual_reviews, compute_violations
from .db import (
    canonical_json,
    connect,
    data_fingerprint,
    metadata_get,
    sha256_text,
    source_data_fingerprint,
)
from .joins import (
    NO_PIPE_PARTITION,
    NO_POINT_PARTITION,
    JoinTaskResult,
    all_join_task_keys,
    execute_nested_loop_join,
    execute_selective_join,
    flatten_task_results,
)
from .planner import (
    POINT_SELECT_SQL,
    PIPE_SELECT_SQL,
    ExecutionPlan,
    PlanPartition,
    build_execution_plan,
    load_queries,
    persist_plan,
)
from .predicates import (
    Predicate,
    PredicateSet,
    QuerySpec,
    compile_side_predicate,
    truth_label,
)
from .reference import execute_central_reference, sort_rows
from .rules import PartitionRules, activate_partition_rules, get_active_rules


class EngineError(RuntimeError):
    pass


class PartitionVersionMismatchError(EngineError):
    pass


class ExecutionInvariantError(EngineError):
    pass


@dataclass
class FilterExecution:
    rows: list[dict[str, Any]]
    null_verdicts: list[dict[str, Any]]
    result_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:8]}"


def _point_eval_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["pipe_id"] = row.get("point_pipe_id")
    result["location"] = row.get("point_location")
    result["metric_types"] = row.get("point_metric_types")
    result["partition_id"] = row.get("point_partition_id")
    return result


def _pipe_eval_row(row: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(row)
    result["row_id"] = row.get("pipe_row_id")
    result["partition_id"] = row.get("pipe_partition_id")
    return result


def _predicate_unknowns(
    predicate_set: PredicateSet, row: Mapping[str, Any], source_type: str
) -> list[dict[str, Any]]:
    _, unknowns = predicate_set.evaluate(row)
    result: list[dict[str, Any]] = []
    for predicate, _group in unknowns:
        result.append(
            {
                "source_type": source_type,
                "predicate": predicate,
                "threshold_text": predicate.display_value,
                "predicate_sql": predicate.sql(),
            }
        )
    return result


def _null_verdict_rows(
    *,
    query: QuerySpec,
    partition_id: str,
    point_eval_rows: Iterable[Mapping[str, Any]] = (),
    pipe_eval_rows: Iterable[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    point_set = compile_side_predicate(query.point_predicates)
    pipe_set = compile_side_predicate(query.pipe_predicates)
    verdicts: list[dict[str, Any]] = []

    for row in point_eval_rows:
        for item in _predicate_unknowns(point_set, row, "point"):
            predicate: Predicate = item["predicate"]
            verdicts.append(
                {
                    "source_type": "point",
                    "partition_id": partition_id,
                    "row_key": f"point:{row.get('point_id')}",
                    "point_id": row.get("point_id"),
                    "sampled_at": None,
                    "predicate_id": predicate.predicate_id,
                    "field_name": predicate.field,
                    "operator": predicate.operator,
                    "threshold_text": item["threshold_text"],
                    "predicate_sql": item["predicate_sql"],
                }
            )
        for item in _predicate_unknowns(
            query.reading_predicates, row, "reading"
        ):
            predicate = item["predicate"]
            verdicts.append(
                {
                    "source_type": "reading",
                    "partition_id": partition_id,
                    "row_key": f"reading:{row.get('reading_id')}",
                    "point_id": row.get("point_id"),
                    "sampled_at": row.get("sampled_at"),
                    "predicate_id": predicate.predicate_id,
                    "field_name": predicate.field,
                    "operator": predicate.operator,
                    "threshold_text": item["threshold_text"],
                    "predicate_sql": item["predicate_sql"],
                }
            )

    for row in pipe_eval_rows:
        for item in _predicate_unknowns(pipe_set, row, "pipe"):
            predicate = item["predicate"]
            verdicts.append(
                {
                    "source_type": "pipe",
                    "partition_id": partition_id,
                    "row_key": f"pipe-row:{row.get('pipe_row_id')}",
                    "point_id": None,
                    "sampled_at": None,
                    "predicate_id": predicate.predicate_id,
                    "field_name": predicate.field,
                    "operator": predicate.operator,
                    "threshold_text": item["threshold_text"],
                    "predicate_sql": item["predicate_sql"],
                }
            )
    return verdicts


class PushdownEngine:
    def __init__(
        self,
        db_path: str,
        *,
        rules: PartitionRules | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.conn = connect(self.db_path)
        if rules is not None:
            try:
                active = get_active_rules(self.conn)
            except RuntimeError:
                activate_partition_rules(self.conn, rules)
            else:
                if active.to_dict() != rules.to_dict():
                    activate_partition_rules(self.conn, rules)
        self.rules = get_active_rules(self.conn)
        self._ensure_data_exists()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "PushdownEngine":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_data_exists(self) -> None:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()
        if row["n"] == 0:
            raise EngineError(
                "no readings found; run the deterministic generator before execution"
            )

    def _begin_run(
        self,
        *,
        run_id: str,
        mode: str,
        rules: PartitionRules,
        source_run_id: str | None,
        snapshot_id: str | None,
    ) -> None:
        self._reset_run(run_id)
        seed = metadata_get(self.conn, "generator_seed")
        self.conn.execute(
            """
            INSERT INTO runs(
                run_id, mode, seed, partition_rules_version,
                partition_rules_json, source_run_id, snapshot_id,
                data_fingerprint, source_data_fingerprint, created_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')
            """,
            (
                run_id,
                mode,
                int(seed) if seed is not None else None,
                rules.version,
                canonical_json(rules.to_dict()),
                source_run_id,
                snapshot_id,
                data_fingerprint(self.conn),
                source_data_fingerprint(self.conn),
                _now(),
            ),
        )
        self.conn.execute(
            """
            INSERT INTO run_partition_assignments(
                run_id, entity_type, entity_key, entity_id, partition_id
            )
            SELECT ?, entity_type, entity_key, entity_id, partition_id
            FROM partition_assignments
            """,
            (run_id,),
        )

    def _reset_run(self, run_id: str) -> None:
        for table in (
            "query_plans",
            "query_plan_partitions",
            "run_pipe_hits",
            "run_point_hits",
            "run_null_verdicts",
            "run_join_results",
            "run_query_results",
            "consistency_reports",
            "run_violations",
            "run_manual_reviews",
            "run_incremental_checks",
            "run_partition_assignments",
            "runs",
        ):
            self.conn.execute(f"DELETE FROM {table} WHERE run_id = ?", (run_id,))

    def central_reference(self, query: QuerySpec) -> list[dict[str, Any]]:
        return execute_central_reference(self.conn, query)

    def plan_query(
        self, query: str | QuerySpec | Mapping[str, Any]
    ) -> ExecutionPlan:
        spec = self._coerce_query(query)
        return build_execution_plan(self.conn, spec, self.rules)

    def _coerce_query(
        self, query: str | QuerySpec | Mapping[str, Any]
    ) -> QuerySpec:
        if isinstance(query, QuerySpec):
            return query
        if isinstance(query, Mapping):
            return QuerySpec.from_dict(query)
        row = self.conn.execute(
            "SELECT request_json FROM query_requests WHERE query_id = ?", (query,)
        ).fetchone()
        if row is None:
            raise EngineError(f"unknown query_id: {query}")
        return QuerySpec.from_json(row["request_json"])

    def execute_pushdown_query(
        self, query: str | QuerySpec | Mapping[str, Any]
    ) -> dict[str, Any]:
        """Execute one request without persisting a full run."""

        spec = self._coerce_query(query)
        plan = build_execution_plan(self.conn, spec, self.rules)
        point_rows, point_execs = self._execute_all_point_partitions(plan)
        pipe_rows, pipe_execs = self._execute_all_pipe_partitions(plan)
        join_tasks = execute_nested_loop_join(
            point_rows, pipe_rows, spec.join_type
        )
        rows = flatten_task_results(join_tasks.values())
        central = execute_central_reference(self.conn, spec)
        report = self._compare_results(spec, plan, rows, central)
        null_verdicts = [
            verdict
            for execution in point_execs.values()
            for verdict in execution.null_verdicts
        ] + [
            verdict
            for execution in pipe_execs.values()
            for verdict in execution.null_verdicts
        ]
        return {
            "query_id": spec.query_id,
            "rows": rows,
            "central_rows": central,
            "null_verdicts": null_verdicts,
            "consistency": report,
            "plan": json.loads(plan.plan_json),
        }

    def _execute_point_partition(
        self, plan: ExecutionPlan, partition: PlanPartition
    ) -> FilterExecution:
        query = plan.query
        hit_rows = [
            dict(row)
            for row in self.conn.execute(
                partition.execution_sql, (partition.partition_id,)
            ).fetchall()
        ]
        candidate_rows = [
            dict(row)
            for row in self.conn.execute(
                POINT_SELECT_SQL
                + " WHERE p.partition_id = ? ORDER BY r.point_id, r.sampled_at, r.reading_id",
                (partition.partition_id,),
            ).fetchall()
        ]
        point_set = compile_side_predicate(query.point_predicates)
        eval_rows = [_point_eval_row(row) for row in candidate_rows]
        evaluated_hits: set[int] = set()
        for row in eval_rows:
            point_truth, _ = point_set.evaluate(row)
            reading_truth, _ = query.reading_predicates.evaluate(row)
            if point_truth is True and reading_truth is True:
                evaluated_hits.add(row["reading_id"])
        sql_hits = {row["reading_id"] for row in hit_rows}
        if evaluated_hits != sql_hits:
            raise ExecutionInvariantError(
                f"point partition {partition.partition_id} SQL/Python truth mismatch: "
                f"only_sql={sorted(sql_hits - evaluated_hits)[:10]}, "
                f"only_python={sorted(evaluated_hits - sql_hits)[:10]}"
            )
        verdicts = _null_verdict_rows(
            query=query,
            partition_id=partition.partition_id,
            point_eval_rows=eval_rows,
        )
        return FilterExecution(
            rows=hit_rows, null_verdicts=verdicts, result_count=len(hit_rows)
        )

    def _execute_pipe_partition(
        self, plan: ExecutionPlan, partition: PlanPartition
    ) -> FilterExecution:
        query = plan.query
        hit_rows = [
            dict(row)
            for row in self.conn.execute(
                partition.execution_sql, (partition.partition_id,)
            ).fetchall()
        ]
        candidate_rows = [
            dict(row)
            for row in self.conn.execute(
                PIPE_SELECT_SQL
                + " WHERE pi.partition_id = ? ORDER BY pi.row_id",
                (partition.partition_id,),
            ).fetchall()
        ]
        pipe_set = compile_side_predicate(query.pipe_predicates)
        eval_rows = [_pipe_eval_row(row) for row in candidate_rows]
        evaluated_hits = {
            row["pipe_row_id"]
            for row in eval_rows
            if pipe_set.evaluate(row)[0] is True
        }
        sql_hits = {row["pipe_row_id"] for row in hit_rows}
        if evaluated_hits != sql_hits:
            raise ExecutionInvariantError(
                f"pipe partition {partition.partition_id} SQL/Python truth mismatch"
            )
        verdicts = _null_verdict_rows(
            query=query,
            partition_id=partition.partition_id,
            pipe_eval_rows=eval_rows,
        )
        return FilterExecution(
            rows=hit_rows, null_verdicts=verdicts, result_count=len(hit_rows)
        )

    def _execute_all_point_partitions(
        self, plan: ExecutionPlan
    ) -> tuple[list[dict[str, Any]], dict[str, FilterExecution]]:
        executions: dict[str, FilterExecution] = {}
        rows: list[dict[str, Any]] = []
        for partition in plan.point_partitions:
            execution = self._execute_point_partition(plan, partition)
            executions[partition.partition_id] = execution
            rows.extend(execution.rows)
        return rows, executions

    def _execute_all_pipe_partitions(
        self, plan: ExecutionPlan
    ) -> tuple[list[dict[str, Any]], dict[str, FilterExecution]]:
        executions: dict[str, FilterExecution] = {}
        rows: list[dict[str, Any]] = []
        for partition in plan.pipe_partitions:
            execution = self._execute_pipe_partition(plan, partition)
            executions[partition.partition_id] = execution
            rows.extend(execution.rows)
        return rows, executions

    def _persist_point_hits(
        self,
        run_id: str,
        query: QuerySpec,
        partition_id: str,
        rows: Iterable[dict[str, Any]],
    ) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO run_point_hits(
                run_id, query_id, partition_id, reading_id,
                point_id, sampled_at, row_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    query.query_id,
                    partition_id,
                    row["reading_id"],
                    row["point_id"],
                    row["sampled_at"],
                    canonical_json(row),
                )
                for row in rows
            ],
        )

    def _persist_pipe_hits(
        self,
        run_id: str,
        query: QuerySpec,
        partition_id: str,
        rows: Iterable[dict[str, Any]],
    ) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO run_pipe_hits(
                run_id, query_id, partition_id, pipe_row_id, pipe_id, row_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    query.query_id,
                    partition_id,
                    row["pipe_row_id"],
                    row["pipe_id"],
                    canonical_json(row),
                )
                for row in rows
            ],
        )

    def _persist_null_verdicts(
        self,
        run_id: str,
        query: QuerySpec,
        verdicts: Iterable[dict[str, Any]],
    ) -> None:
        self.conn.executemany(
            """
            INSERT INTO run_null_verdicts(
                run_id, query_id, source_type, partition_id, row_key,
                point_id, sampled_at, predicate_id, field_name, operator,
                threshold_text, predicate_sql, verdict
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'UNKNOWN')
            """,
            [
                (
                    run_id,
                    query.query_id,
                    row["source_type"],
                    row["partition_id"],
                    row["row_key"],
                    row["point_id"],
                    row["sampled_at"],
                    row["predicate_id"],
                    row["field_name"],
                    row["operator"],
                    row["threshold_text"],
                    row["predicate_sql"],
                )
                for row in verdicts
            ],
        )

    def _persist_hits_and_verdicts(
        self,
        run_id: str,
        plan: ExecutionPlan,
        point_executions: Mapping[str, FilterExecution],
        pipe_executions: Mapping[str, FilterExecution],
    ) -> None:
        for partition_id, execution in point_executions.items():
            self._persist_point_hits(
                run_id, plan.query, partition_id, execution.rows
            )
            self._persist_null_verdicts(
                run_id, plan.query, execution.null_verdicts
            )
        for partition_id, execution in pipe_executions.items():
            self._persist_pipe_hits(
                run_id, plan.query, partition_id, execution.rows
            )
            self._persist_null_verdicts(
                run_id, plan.query, execution.null_verdicts
            )

    def _persist_join_tasks(
        self,
        run_id: str,
        query: QuerySpec,
        tasks: Iterable[JoinTaskResult],
    ) -> None:
        for task in sorted(tasks, key=lambda item: item.join_task_key):
            self.conn.executemany(
                """
                INSERT OR REPLACE INTO run_join_results(
                    run_id, query_id, join_task_key,
                    pipe_partition_id, point_partition_id,
                    row_index, row_json, row_signature
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        query.query_id,
                        task.join_task_key,
                        task.pipe_partition_id,
                        task.point_partition_id,
                        index,
                        canonical_json(row),
                        sha256_text(canonical_json(row)),
                    )
                    for index, row in enumerate(task.rows)
                ],
            )

    def _persist_query_results(
        self,
        run_id: str,
        query: QuerySpec,
        rows: list[dict[str, Any]],
    ) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO run_query_results(
                run_id, query_id, row_index, row_json, row_signature
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    query.query_id,
                    index,
                    canonical_json(row),
                    sha256_text(canonical_json(row)),
                )
                for index, row in enumerate(rows)
            ],
        )

    def _compare_results(
        self,
        query: QuerySpec,
        plan: ExecutionPlan,
        downpush_rows: list[dict[str, Any]],
        central_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        downpush = sort_rows(downpush_rows)
        central = sort_rows(central_rows)

        def identity(row: Mapping[str, Any]) -> tuple[str, Any]:
            if row.get("reading_id") is not None:
                return ("reading", row["reading_id"])
            if row.get("pipe_row_id") is not None:
                return ("pipe-row", row["pipe_row_id"])
            return ("signature", sha256_text(canonical_json(row)))

        down_map = {identity(row): row for row in downpush}
        central_map = {identity(row): row for row in central}
        downpush_duplicates = len(downpush) - len(down_map)
        central_duplicates = len(central) - len(central_map)
        missing_in_pushdown = [
            {
                "identity": list(key),
                "row": central_map[key],
            }
            for key in sorted(central_map.keys() - down_map.keys(), key=str)
        ]
        missing_in_central = [
            {
                "identity": list(key),
                "row": down_map[key],
            }
            for key in sorted(down_map.keys() - central_map.keys(), key=str)
        ]
        marker_mismatches: list[dict[str, Any]] = []
        row_signature_mismatches: list[dict[str, Any]] = []
        for key in sorted(down_map.keys() & central_map.keys(), key=str):
            down = down_map[key]
            ref = central_map[key]
            if down.get("join_null_source") != ref.get("join_null_source"):
                marker_mismatches.append(
                    {
                        "identity": list(key),
                        "pushdown_marker": down.get("join_null_source"),
                        "central_marker": ref.get("join_null_source"),
                        "point_partition_id": down.get("point_partition_id")
                        or ref.get("point_partition_id"),
                        "pipe_partition_id": down.get("pipe_partition_id")
                        or ref.get("pipe_partition_id"),
                    }
                )
            if canonical_json(down) != canonical_json(ref):
                row_signature_mismatches.append(
                    {
                        "identity": list(key),
                        "pushdown_row": down,
                        "central_row": ref,
                    }
                )

        down_order = [identity(row) for row in downpush]
        central_order = [identity(row) for row in central]
        order_mismatches: list[dict[str, Any]] = []
        max_len = max(len(down_order), len(central_order))
        for index in range(max_len):
            left = down_order[index] if index < len(down_order) else None
            right = central_order[index] if index < len(central_order) else None
            if left != right:
                order_mismatches.append(
                    {
                        "index": index,
                        "pushdown_identity": list(left) if left else None,
                        "central_identity": list(right) if right else None,
                    }
                )

        is_consistent = not (
            missing_in_pushdown
            or missing_in_central
            or marker_mismatches
            or row_signature_mismatches
            or order_mismatches
            or downpush_duplicates
            or central_duplicates
        )
        divergence_partition = None
        divergence_predicate = None
        if not is_consistent:
            order_candidate = None
            if order_mismatches:
                first_order = order_mismatches[0]
                if first_order["pushdown_identity"] is not None:
                    order_candidate = down_map.get(
                        tuple(first_order["pushdown_identity"])
                    )
                if order_candidate is None and first_order["central_identity"] is not None:
                    order_candidate = central_map.get(
                        tuple(first_order["central_identity"])
                    )
            candidate = (
                (missing_in_pushdown[0]["row"] if missing_in_pushdown else None)
                or (missing_in_central[0]["row"] if missing_in_central else None)
                or (marker_mismatches[0] if marker_mismatches else None)
                or (
                    row_signature_mismatches[0]["pushdown_row"]
                    if row_signature_mismatches
                    else None
                )
                or order_candidate
                or {}
            )
            divergence_partition = (
                f"point={candidate.get('point_partition_id')},"
                f"pipe={candidate.get('pipe_partition_id')}"
            )
            if marker_mismatches:
                divergence_predicate = "JOIN p.pipe_id = pi.pipe_id (NULL != NULL)"
            else:
                divergence_predicate = (
                    f"pipe:{compile_side_predicate(query.pipe_predicates).text()} | "
                    f"point:{compile_side_predicate(query.point_predicates).text()} | "
                    f"reading:{query.reading_predicates.text()}"
                )
        details = {
            "missing_in_pushdown_count": len(missing_in_pushdown),
            "missing_in_central_count": len(missing_in_central),
            "marker_mismatch_count": len(marker_mismatches),
            "row_signature_mismatch_count": len(row_signature_mismatches),
            "order_mismatch_count": len(order_mismatches),
            "downpush_duplicate_identity_count": downpush_duplicates,
            "central_duplicate_identity_count": central_duplicates,
            "row_signature_mismatches": row_signature_mismatches[:20],
            "join_null_rules": {
                "inner": "either join key NULL -> no match",
                "left": "preserve points; point NULL key -> point_key_null",
                "right": "preserve pipes; pipe NULL key -> pipe_key_null",
                "full": "preserve both sides; NULL never equals NULL",
            },
        }
        return {
            "run_id": None,
            "query_id": query.query_id,
            "is_consistent": is_consistent,
            "downpush_count": len(downpush),
            "central_count": len(central),
            "missing_in_pushdown": missing_in_pushdown,
            "missing_in_central": missing_in_central,
            "marker_mismatches": marker_mismatches,
            "order_mismatches": order_mismatches,
            "divergence_partition": divergence_partition,
            "divergence_predicate": divergence_predicate,
            "details": details,
        }

    def _persist_consistency(
        self, run_id: str, report: Mapping[str, Any]
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO consistency_reports(
                run_id, query_id, is_consistent, downpush_count, central_count,
                missing_in_pushdown_json, missing_in_central_json,
                marker_mismatches_json, order_mismatches_json,
                divergence_partition, divergence_predicate, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                report["query_id"],
                1 if report["is_consistent"] else 0,
                report["downpush_count"],
                report["central_count"],
                canonical_json(report["missing_in_pushdown"]),
                canonical_json(report["missing_in_central"]),
                canonical_json(report["marker_mismatches"]),
                canonical_json(report["order_mismatches"]),
                report["divergence_partition"],
                report["divergence_predicate"],
                canonical_json(report["details"]),
            ),
        )

    def _persist_audit(self, run_id: str) -> dict[str, Any]:
        violations = compute_violations(self.conn)
        reviews = compute_manual_reviews(self.conn)
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO run_violations(
                run_id, id, point_id, sampled_at, metric, value,
                rule_name, threshold_low, threshold_high,
                point_partition_id, pipe_partition_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    index,
                    item["point_id"],
                    item["sampled_at"],
                    item["metric"],
                    item["value"],
                    item["rule_name"],
                    item["threshold_low"],
                    item["threshold_high"],
                    item["point_partition_id"],
                    item["pipe_partition_id"],
                )
                for index, item in enumerate(violations, start=1)
            ],
        )
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO run_manual_reviews(
                run_id, id, point_id, metric, interval_start,
                interval_end, missing_count, point_partition_id,
                pipe_partition_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    run_id,
                    index,
                    item["point_id"],
                    item["metric"],
                    item["interval_start"],
                    item["interval_end"],
                    item["missing_count"],
                    item["point_partition_id"],
                    item["pipe_partition_id"],
                )
                for index, item in enumerate(reviews, start=1)
            ],
        )
        truth_violations = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT point_id, sampled_at, metric, value, rule_name,
                       threshold_low, threshold_high,
                       point_partition_id, pipe_partition_id
                FROM truth_violations
                ORDER BY point_id, sampled_at, metric, rule_name
                """
            ).fetchall()
        ]
        truth_reviews = [
            dict(row)
            for row in self.conn.execute(
                """
                SELECT point_id, metric, interval_start, interval_end,
                       missing_count, point_partition_id, pipe_partition_id
                FROM truth_manual_reviews
                ORDER BY point_id, metric, interval_start
                """
            ).fetchall()
        ]

        def semantic(a: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[Any, ...]:
            return tuple(a.get(key) for key in keys)

        violation_keys = (
            "point_id",
            "sampled_at",
            "metric",
            "value",
            "rule_name",
            "threshold_low",
            "threshold_high",
        )
        review_keys = (
            "point_id",
            "metric",
            "interval_start",
            "interval_end",
            "missing_count",
        )
        violations_match = sorted(
            semantic(item, violation_keys) for item in violations
        ) == sorted(semantic(item, violation_keys) for item in truth_violations)
        reviews_match = sorted(
            semantic(item, review_keys) for item in reviews
        ) == sorted(semantic(item, review_keys) for item in truth_reviews)
        violation_partitions_match = sorted(
            (
                item["point_id"],
                item["sampled_at"],
                item["metric"],
                item["point_partition_id"],
                item["pipe_partition_id"],
            )
            for item in violations
        ) == sorted(
            (
                item["point_id"],
                item["sampled_at"],
                item["metric"],
                item["point_partition_id"],
                item["pipe_partition_id"],
            )
            for item in truth_violations
        )
        review_partitions_match = sorted(
            (
                item["point_id"],
                item["metric"],
                item["interval_start"],
                item["interval_end"],
                item["point_partition_id"],
                item["pipe_partition_id"],
            )
            for item in reviews
        ) == sorted(
            (
                item["point_id"],
                item["metric"],
                item["interval_start"],
                item["interval_end"],
                item["point_partition_id"],
                item["pipe_partition_id"],
            )
            for item in truth_reviews
        )
        return {
            "violations_match_truth": violations_match,
            "reviews_match_truth": reviews_match,
            "violation_partition_ids_match_truth": violation_partitions_match,
            "review_partition_ids_match_truth": review_partitions_match,
            "violation_count": len(violations),
            "review_count": len(reviews),
            "violations": violations,
            "reviews": reviews,
        }

    def run_all(self, *, run_id: str | None = None) -> dict[str, Any]:
        run_id = run_id or _new_run_id("full")
        self._begin_run(
            run_id=run_id,
            mode="full",
            rules=self.rules,
            source_run_id=None,
            snapshot_id=None,
        )
        query_reports: list[dict[str, Any]] = []
        try:
            for query in load_queries(self.conn):
                plan = build_execution_plan(self.conn, query, self.rules)
                point_rows, point_executions = self._execute_all_point_partitions(
                    plan
                )
                pipe_rows, pipe_executions = self._execute_all_pipe_partitions(
                    plan
                )
                self._persist_hits_and_verdicts(
                    run_id, plan, point_executions, pipe_executions
                )
                tasks = execute_nested_loop_join(
                    point_rows, pipe_rows, query.join_type
                )
                self._persist_join_tasks(run_id, query, tasks.values())
                final_rows = flatten_task_results(tasks.values())
                self._persist_query_results(run_id, query, final_rows)
                central = execute_central_reference(self.conn, query)
                report = self._compare_results(query, plan, final_rows, central)
                report["run_id"] = run_id
                self._persist_consistency(run_id, report)
                persist_plan(
                    self.conn,
                    run_id,
                    plan,
                    point_result_counts={
                        key: value.result_count
                        for key, value in point_executions.items()
                    },
                    pipe_result_counts={
                        key: value.result_count
                        for key, value in pipe_executions.items()
                    },
                    join_result_counts={
                        task.join_task_key: len(task.rows)
                        for task in tasks.values()
                    },
                )
                query_reports.append(report)

            audit = self._persist_audit(run_id)
            all_consistent = all(item["is_consistent"] for item in query_reports)
            self.conn.execute(
                "UPDATE runs SET status = 'complete' WHERE run_id = ?", (run_id,)
            )
            self.conn.commit()
            return {
                "run_id": run_id,
                "mode": "full",
                "partition_rules_version": self.rules.version,
                "query_count": len(query_reports),
                "all_consistent": all_consistent,
                "query_reports": query_reports,
                "audit": audit,
                "all_truth_matched": (
                    audit["violations_match_truth"]
                    and audit["reviews_match_truth"]
                ),
            }
        except Exception:
            self.conn.rollback()
            raise

    def _apply_query_changes(
        self, query_changes: Mapping[str, Mapping[str, Any]] | None
    ) -> None:
        if not query_changes:
            return
        now = _now()
        for query_id, payload in query_changes.items():
            data = dict(payload)
            data.setdefault("query_id", query_id)
            if data["query_id"] != query_id:
                raise EngineError(
                    f"query change key {query_id!r} != payload query_id "
                    f"{data['query_id']!r}"
                )
            QuerySpec.from_dict(data)
            self.conn.execute(
                """
                INSERT INTO query_requests(
                    query_id, description, join_type, request_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(query_id) DO UPDATE SET
                    description = excluded.description,
                    join_type = excluded.join_type,
                    request_json = excluded.request_json,
                    created_at = excluded.created_at
                """,
                (
                    query_id,
                    data.get("description", ""),
                    data.get("join_type", "inner"),
                    canonical_json(data),
                    now,
                ),
            )

    def _previous_queries(self, run_id: str) -> dict[str, QuerySpec]:
        result: dict[str, QuerySpec] = {}
        for row in self.conn.execute(
            "SELECT query_json FROM query_plans WHERE run_id = ?", (run_id,)
        ).fetchall():
            query = QuerySpec.from_dict(json.loads(row["query_json"]))
            result[query.query_id] = query
        return result

    def _previous_plan_partitions(
        self, run_id: str
    ) -> dict[tuple[str, str, str], int]:
        result: dict[tuple[str, str, str], int] = {}
        for row in self.conn.execute(
            """
            SELECT query_id, part_type, partition_id, result_count
            FROM query_plan_partitions
            WHERE run_id = ?
            """,
            (run_id,),
        ):
            result[
                (row["query_id"], row["part_type"], row["partition_id"])
            ] = row["result_count"]
        return result

    def _previous_hits(
        self, run_id: str, table: str
    ) -> dict[tuple[str, str], list[dict[str, Any]]]:
        if table not in {"run_point_hits", "run_pipe_hits"}:
            raise ValueError(table)
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in self.conn.execute(
            f"""
            SELECT query_id, partition_id, row_json
            FROM {table}
            WHERE run_id = ?
            ORDER BY partition_id, row_json
            """,
            (run_id,),
        ):
            grouped[(row["query_id"], row["partition_id"])].append(
                json.loads(row["row_json"])
            )
        return dict(grouped)

    def _previous_join_tasks(
        self, run_id: str
    ) -> dict[str, dict[str, list[dict[str, Any]]]]:
        grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in self.conn.execute(
            """
            SELECT query_id, join_task_key, row_json
            FROM run_join_results
            WHERE run_id = ?
            ORDER BY query_id, join_task_key, row_index
            """,
            (run_id,),
        ):
            grouped[row["query_id"]][row["join_task_key"]].append(
                json.loads(row["row_json"])
            )
        return {
            query_id: dict(tasks) for query_id, tasks in grouped.items()
        }

    def _copy_null_verdicts(
        self,
        previous_run_id: str,
        run_id: str,
        query_id: str,
        partition_id: str,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO run_null_verdicts(
                run_id, query_id, source_type, partition_id, row_key,
                point_id, sampled_at, predicate_id, field_name, operator,
                threshold_text, predicate_sql, verdict
            )
            SELECT ?, query_id, source_type, partition_id, row_key,
                   point_id, sampled_at, predicate_id, field_name, operator,
                   threshold_text, predicate_sql, verdict
            FROM run_null_verdicts
            WHERE run_id = ? AND query_id = ? AND partition_id = ?
            """,
            (run_id, previous_run_id, query_id, partition_id),
        )

    def _filter_difference(
        self,
        old_query: QuerySpec | None,
        new_query: QuerySpec,
        *,
        current_point_partitions: set[str],
        current_pipe_partitions: set[str],
    ) -> dict[str, Any]:
        if old_query is None:
            return {
                "point_partitions": set(current_point_partitions),
                "pipe_partitions": set(current_pipe_partitions),
                "point_filters_changed": True,
                "pipe_filters_changed": True,
                "join_type_changed": True,
            }
        old_point_sql = compile_side_predicate(old_query.point_predicates).sql()
        new_point_sql = compile_side_predicate(new_query.point_predicates).sql()
        old_pipe_sql = compile_side_predicate(old_query.pipe_predicates).sql()
        new_pipe_sql = compile_side_predicate(new_query.pipe_predicates).sql()
        old_reading_sql = old_query.reading_predicates.sql()
        new_reading_sql = new_query.reading_predicates.sql()
        point_changed = old_point_sql != new_point_sql or old_reading_sql != new_reading_sql
        pipe_changed = old_pipe_sql != new_pipe_sql

        affected_points: set[str] = set()
        affected_pipes: set[str] = set()
        changed_pipe_ids: set[str] = set()
        point_rows = [
            dict(row)
            for row in self.conn.execute(
                POINT_SELECT_SQL + " ORDER BY r.point_id, r.sampled_at, r.reading_id"
            ).fetchall()
        ]
        point_eval_rows = [_point_eval_row(row) for row in point_rows]
        pipe_rows = [
            dict(row)
            for row in self.conn.execute(
                PIPE_SELECT_SQL + " ORDER BY pi.row_id"
            ).fetchall()
        ]
        pipe_eval_rows = [_pipe_eval_row(row) for row in pipe_rows]

        if point_changed:
            old_point_set = compile_side_predicate(old_query.point_predicates)
            new_point_set = compile_side_predicate(new_query.point_predicates)
            for row in point_eval_rows:
                old_hit = (
                    old_point_set.evaluate(row)[0] is True
                    and old_query.reading_predicates.evaluate(row)[0] is True
                )
                new_hit = (
                    new_point_set.evaluate(row)[0] is True
                    and new_query.reading_predicates.evaluate(row)[0] is True
                )
                old_nulls = {
                    (
                        item["predicate"].predicate_id,
                        item["predicate"].field,
                        item["predicate"].operator,
                        item["threshold_text"],
                        item["predicate_sql"],
                    )
                    for item in (
                        _predicate_unknowns(old_point_set, row, "point")
                        + _predicate_unknowns(
                            old_query.reading_predicates, row, "reading"
                        )
                    )
                }
                new_nulls = {
                    (
                        item["predicate"].predicate_id,
                        item["predicate"].field,
                        item["predicate"].operator,
                        item["threshold_text"],
                        item["predicate_sql"],
                    )
                    for item in (
                        _predicate_unknowns(new_point_set, row, "point")
                        + _predicate_unknowns(
                            new_query.reading_predicates, row, "reading"
                        )
                    )
                }
                if old_hit != new_hit or old_nulls != new_nulls:
                    affected_points.add(row["partition_id"])

        if pipe_changed:
            old_pipe_set = compile_side_predicate(old_query.pipe_predicates)
            new_pipe_set = compile_side_predicate(new_query.pipe_predicates)
            for row in pipe_eval_rows:
                old_hit = old_pipe_set.evaluate(row)[0] is True
                new_hit = new_pipe_set.evaluate(row)[0] is True
                old_nulls = {
                    (
                        item["predicate"].predicate_id,
                        item["predicate"].field,
                        item["predicate"].operator,
                        item["threshold_text"],
                        item["predicate_sql"],
                    )
                    for item in _predicate_unknowns(old_pipe_set, row, "pipe")
                }
                new_nulls = {
                    (
                        item["predicate"].predicate_id,
                        item["predicate"].field,
                        item["predicate"].operator,
                        item["threshold_text"],
                        item["predicate_sql"],
                    )
                    for item in _predicate_unknowns(new_pipe_set, row, "pipe")
                }
                if old_hit != new_hit or old_nulls != new_nulls:
                    affected_pipes.add(row["partition_id"])
                    if row.get("pipe_id") is not None:
                        changed_pipe_ids.add(row["pipe_id"])
            if changed_pipe_ids:
                for row in point_eval_rows:
                    if row.get("point_pipe_id") in changed_pipe_ids:
                        affected_points.add(row["partition_id"])

        return {
            "point_partitions": affected_points,
            "pipe_partitions": affected_pipes,
            "point_filters_changed": point_changed,
            "pipe_filters_changed": pipe_changed,
            "join_type_changed": old_query.join_type != new_query.join_type,
        }

    def _rules_changed_partitions(
        self, previous_run_id: str
    ) -> tuple[set[str], set[str]]:
        old = {
            (row["entity_type"], row["entity_key"]): row["partition_id"]
            for row in self.conn.execute(
                """
                SELECT entity_type, entity_key, partition_id
                FROM run_partition_assignments
                WHERE run_id = ?
                """,
                (previous_run_id,),
            )
        }
        new = {
            (row["entity_type"], row["entity_key"]): row["partition_id"]
            for row in self.conn.execute(
                """
                SELECT entity_type, entity_key, partition_id
                FROM partition_assignments
                """
            )
        }
        point_parts: set[str] = set()
        pipe_parts: set[str] = set()
        for key in set(old) | set(new):
            old_partition = old.get(key)
            new_partition = new.get(key)
            if old_partition == new_partition:
                continue
            target = point_parts if key[0] == "point" else pipe_parts
            if old_partition is not None:
                target.add(old_partition)
            if new_partition is not None:
                target.add(new_partition)
        return point_parts, pipe_parts

    def _copy_reused_partition(
        self,
        *,
        previous_run_id: str,
        run_id: str,
        query: QuerySpec,
        partition: PlanPartition,
        previous_hits: Mapping[tuple[str, str], list[dict[str, Any]]],
        side: str,
    ) -> FilterExecution:
        rows = list(previous_hits.get((query.query_id, partition.partition_id), []))
        self._copy_null_verdicts(
            previous_run_id,
            run_id,
            query.query_id,
            partition.partition_id,
        )
        if side == "point":
            self._persist_point_hits(
                run_id, query, partition.partition_id, rows
            )
        else:
            self._persist_pipe_hits(
                run_id, query, partition.partition_id, rows
            )
        return FilterExecution(rows=rows, null_verdicts=[], result_count=len(rows))

    def _compute_full_downpush(
        self, plan: ExecutionPlan
    ) -> list[dict[str, Any]]:
        point_rows, _ = self._execute_all_point_partitions(plan)
        pipe_rows, _ = self._execute_all_pipe_partitions(plan)
        tasks = execute_nested_loop_join(
            point_rows, pipe_rows, plan.query.join_type
        )
        return flatten_task_results(tasks.values())

    def _persist_incremental_check(
        self,
        run_id: str,
        report: Mapping[str, Any],
        *,
        reused_join_tasks: int,
        recomputed_join_tasks: int,
        recomputed_partition_ids: Iterable[str],
    ) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO run_incremental_checks(
                run_id, query_id, is_consistent,
                reused_join_task_count, recomputed_join_task_count,
                recomputed_partition_ids_json, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                report["query_id"],
                1 if report["is_consistent"] else 0,
                reused_join_tasks,
                recomputed_join_tasks,
                canonical_json(sorted(set(recomputed_partition_ids))),
                canonical_json(
                    {
                        "downpush_count": report["downpush_count"],
                        "central_count": report["central_count"],
                        "missing_in_pushdown": report["missing_in_pushdown"][:20],
                        "missing_in_central": report["missing_in_central"][:20],
                        "marker_mismatches": report["marker_mismatches"][:20],
                        "order_mismatches": report["order_mismatches"][:20],
                        "divergence_partition": report["divergence_partition"],
                        "divergence_predicate": report["divergence_predicate"],
                    }
                ),
            ),
        )

    def run_incremental(
        self,
        previous_run_id: str,
        *,
        query_changes: Mapping[str, Mapping[str, Any]] | None = None,
        new_rules: PartitionRules | None = None,
        run_id: str | None = None,
        snapshot_id: str | None = None,
        verify_against_full: bool = True,
    ) -> dict[str, Any]:
        previous_run = self.conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (previous_run_id,)
        ).fetchone()
        if previous_run is None:
            raise EngineError(f"unknown previous run: {previous_run_id}")
        previous_queries = self._previous_queries(previous_run_id)
        previous_parts = self._previous_plan_partitions(previous_run_id)
        previous_point_hits = self._previous_hits(previous_run_id, "run_point_hits")
        previous_pipe_hits = self._previous_hits(previous_run_id, "run_pipe_hits")
        previous_join_tasks = self._previous_join_tasks(previous_run_id)

        old_source_fingerprint = previous_run["source_data_fingerprint"]
        self._apply_query_changes(query_changes)
        if new_rules is not None:
            active_rules = get_active_rules(self.conn)
            if active_rules.to_dict() != new_rules.to_dict():
                activate_partition_rules(self.conn, new_rules)
            self.rules = new_rules
        else:
            self.rules = get_active_rules(self.conn)
        old_point_rules, old_pipe_rules = self._rules_changed_partitions(
            previous_run_id
        )
        current_source_fingerprint = source_data_fingerprint(self.conn)
        data_changed = old_source_fingerprint != current_source_fingerprint
        if data_changed:
            old_point_rules = set(old_point_rules)
            old_pipe_rules = set(old_pipe_rules)

        run_id = run_id or _new_run_id("incremental")
        self._begin_run(
            run_id=run_id,
            mode="incremental",
            rules=self.rules,
            source_run_id=previous_run_id,
            snapshot_id=snapshot_id,
        )
        query_reports: list[dict[str, Any]] = []
        incremental_checks: list[dict[str, Any]] = []
        try:
            queries = load_queries(self.conn)
            for query in queries:
                old_query = previous_queries.get(query.query_id)
                plan = build_execution_plan(self.conn, query, self.rules)
                current_point_parts = {
                    item.partition_id for item in plan.point_partitions
                }
                current_pipe_parts = {
                    item.partition_id for item in plan.pipe_partitions
                }
                differences = self._filter_difference(
                    old_query,
                    query,
                    current_point_partitions=current_point_parts,
                    current_pipe_partitions=current_pipe_parts,
                )
                affected_points = set(differences["point_partitions"])
                affected_pipes = set(differences["pipe_partitions"])
                affected_points |= old_point_rules
                affected_pipes |= old_pipe_rules
                if data_changed:
                    affected_points |= current_point_parts
                    affected_pipes |= current_pipe_parts

                # New or newly materialized partitions have no reusable prior
                # rows even when the query expression is unchanged.
                for partition in plan.point_partitions:
                    if (
                        query.query_id,
                        "point_filter",
                        partition.partition_id,
                    ) not in previous_parts:
                        affected_points.add(partition.partition_id)
                for partition in plan.pipe_partitions:
                    if (
                        query.query_id,
                        "pipe_filter",
                        partition.partition_id,
                    ) not in previous_parts:
                        affected_pipes.add(partition.partition_id)

                point_rows: list[dict[str, Any]] = []
                pipe_rows: list[dict[str, Any]] = []
                point_executions: dict[str, FilterExecution] = {}
                pipe_executions: dict[str, FilterExecution] = {}

                for partition in plan.point_partitions:
                    has_previous = (
                        query.query_id,
                        "point_filter",
                        partition.partition_id,
                    ) in previous_parts
                    if (
                        old_query is not None
                        and partition.partition_id not in affected_points
                        and has_previous
                    ):
                        execution = self._copy_reused_partition(
                            previous_run_id=previous_run_id,
                            run_id=run_id,
                            query=query,
                            partition=partition,
                            previous_hits=previous_point_hits,
                            side="point",
                        )
                    else:
                        execution = self._execute_point_partition(plan, partition)
                        self._persist_point_hits(
                            run_id, query, partition.partition_id, execution.rows
                        )
                        self._persist_null_verdicts(
                            run_id, query, execution.null_verdicts
                        )
                    point_executions[partition.partition_id] = execution
                    point_rows.extend(execution.rows)

                for partition in plan.pipe_partitions:
                    has_previous = (
                        query.query_id,
                        "pipe_filter",
                        partition.partition_id,
                    ) in previous_parts
                    if (
                        old_query is not None
                        and partition.partition_id not in affected_pipes
                        and has_previous
                    ):
                        execution = self._copy_reused_partition(
                            previous_run_id=previous_run_id,
                            run_id=run_id,
                            query=query,
                            partition=partition,
                            previous_hits=previous_pipe_hits,
                            side="pipe",
                        )
                    else:
                        execution = self._execute_pipe_partition(plan, partition)
                        self._persist_pipe_hits(
                            run_id, query, partition.partition_id, execution.rows
                        )
                        self._persist_null_verdicts(
                            run_id, query, execution.null_verdicts
                        )
                    pipe_executions[partition.partition_id] = execution
                    pipe_rows.extend(execution.rows)

                force_join_all = (
                    old_query is None
                    or differences["join_type_changed"]
                    or data_changed
                )
                join_affected_points = set(affected_points)
                join_affected_pipes = set(affected_pipes)
                # In RIGHT/FULL, a changed point hit can change whether a pipe
                # is unmatched, so pipe-only tasks must be reconsidered.
                if (
                    not force_join_all
                    and differences["point_filters_changed"]
                    and query.join_type in {"right", "full"}
                    and affected_points
                ):
                    join_affected_pipes |= current_pipe_parts
                if force_join_all:
                    join_affected_points = set(current_point_parts)
                    join_affected_pipes = set(current_pipe_parts)

                previous_tasks_for_query = previous_join_tasks.get(query.query_id, {})
                plan_task_map = {
                    task.join_task_key: task for task in plan.join_tasks
                }
                reused_tasks: dict[str, JoinTaskResult] = {}
                if old_query is not None:
                    for key, rows in previous_tasks_for_query.items():
                        task = plan_task_map.get(key)
                        if task is None:
                            continue
                        point_touched = (
                            task.point_partition_id in join_affected_points
                            and task.point_partition_id != NO_POINT_PARTITION
                        )
                        pipe_touched = (
                            task.pipe_partition_id in join_affected_pipes
                            and task.pipe_partition_id != NO_PIPE_PARTITION
                        )
                        if point_touched or pipe_touched:
                            continue
                        sorted_rows = sort_rows(rows)
                        first = sorted_rows[0]
                        reused_tasks[key] = JoinTaskResult(
                            join_task_key=key,
                            pipe_partition_id=first.get("pipe_partition_id")
                            or NO_PIPE_PARTITION,
                            point_partition_id=first.get("point_partition_id")
                            or NO_POINT_PARTITION,
                            rows=sorted_rows,
                        )

                if join_affected_points or join_affected_pipes:
                    recomputed_tasks = execute_selective_join(
                        point_rows,
                        pipe_rows,
                        query.join_type,
                        recompute_point_partitions=join_affected_points,
                        recompute_pipe_partitions=join_affected_pipes,
                    )
                else:
                    recomputed_tasks = {}
                all_tasks = dict(reused_tasks)
                all_tasks.update(recomputed_tasks)
                self._persist_join_tasks(run_id, query, all_tasks.values())
                final_rows = flatten_task_results(all_tasks.values())
                self._persist_query_results(run_id, query, final_rows)
                central = execute_central_reference(self.conn, query)
                report = self._compare_results(query, plan, final_rows, central)
                report["run_id"] = run_id
                self._persist_consistency(run_id, report)
                persist_plan(
                    self.conn,
                    run_id,
                    plan,
                    point_result_counts={
                        key: value.result_count
                        for key, value in point_executions.items()
                    },
                    pipe_result_counts={
                        key: value.result_count
                        for key, value in pipe_executions.items()
                    },
                    join_result_counts={
                        task.join_task_key: len(task.rows)
                        for task in all_tasks.values()
                    },
                )
                query_reports.append(report)

                if verify_against_full:
                    full_rows = self._compute_full_downpush(plan)
                    check_report = self._compare_results(
                        query, plan, final_rows, full_rows
                    )
                else:
                    check_report = report
                self._persist_incremental_check(
                    run_id,
                    check_report,
                    reused_join_tasks=len(reused_tasks),
                    recomputed_join_tasks=len(recomputed_tasks),
                    recomputed_partition_ids=join_affected_points
                    | join_affected_pipes,
                )
                incremental_checks.append(
                    {
                        "query_id": query.query_id,
                        "is_consistent_with_full_pushdown": check_report[
                            "is_consistent"
                        ],
                        "reused_join_task_count": len(reused_tasks),
                        "recomputed_join_task_count": len(recomputed_tasks),
                        "recomputed_partition_ids": sorted(
                            join_affected_points | join_affected_pipes
                        ),
                        "divergence_partition": check_report[
                            "divergence_partition"
                        ],
                        "divergence_predicate": check_report[
                            "divergence_predicate"
                        ],
                    }
                )

            audit = self._persist_audit(run_id)
            all_consistent = all(item["is_consistent"] for item in query_reports)
            all_incremental_consistent = all(
                item["is_consistent_with_full_pushdown"]
                for item in incremental_checks
            )
            self.conn.execute(
                "UPDATE runs SET status = 'complete' WHERE run_id = ?", (run_id,)
            )
            self.conn.commit()
            return {
                "run_id": run_id,
                "mode": "incremental",
                "previous_run_id": previous_run_id,
                "snapshot_id": snapshot_id,
                "partition_rules_version": self.rules.version,
                "query_count": len(query_reports),
                "all_consistent": all_consistent,
                "all_incremental_consistent": all_incremental_consistent,
                "query_reports": query_reports,
                "incremental_checks": incremental_checks,
                "audit": audit,
                "all_truth_matched": (
                    audit["violations_match_truth"]
                    and audit["reviews_match_truth"]
                ),
            }
        except Exception:
            self.conn.rollback()
            raise

