"""Query execution, deterministic merge, fault injection, and recovery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from .errors import BizError, canonical_json, digest
from .injections import InjectionSet, Scenario
from .router import RewriteResult, build_rewrite, merge_params
from .rules import QueryTemplate
from .state import SnapshotView, StateStore, transaction


CRASH_EXIT_CODE = 75
CRASH_POINTS = {
    "after_route",
    "after_rewrite",
    "after_execute",
    "after_merge",
}
QUERY_WAIT_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class _WaitForQuery:
    query_id: str


def _short_hash(value: Any) -> str:
    payload = canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _query_key(template_id: str, params: dict[str, Any]) -> str:
    return "qk_" + _short_hash({"params": params, "template_id": template_id})


def _query_id(rule_version: str, query_key: str) -> str:
    return "q_" + _short_hash({"query_key": query_key, "rule_version": rule_version})


def _json_load(text: str) -> Any:
    return json.loads(text)


def _sortable(value: Any) -> tuple[int, Any]:
    if value is None:
        return (0, 0)
    if type(value) is int:
        return (1, value)
    if type(value) is float:
        return (2, value)
    if isinstance(value, str):
        return (3, value)
    if isinstance(value, bytes):
        return (4, value.hex())
    return (9, canonical_json(value))


def _outcome_public(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "database": payload["database"],
        "error_code": payload.get("error_code"),
        "error_details": payload.get("error_details", {}),
        "error_stage": payload.get("error_stage"),
        "result_digest": payload.get("result_digest"),
        "rows_returned": int(payload.get("rows_returned", 0)),
        "shard_id": int(payload["shard_id"]),
        "status": payload["status"],
        "table": payload["table"],
    }


class QueryEngine:
    def __init__(
        self,
        state: StateStore,
        clock_ms: int,
        *,
        injections: InjectionSet | None = None,
        crash_at: str | None = None,
    ) -> None:
        if type(clock_ms) is not int or clock_ms < 0:
            raise BizError("E_INVALID_ARGUMENT", "clock must be a non-negative integer")
        if crash_at is not None and crash_at not in CRASH_POINTS:
            raise BizError(
                "E_INVALID_ARGUMENT",
                "crash_at must be one of the supported persistence points",
                details={"allowed": sorted(CRASH_POINTS), "crash_at": crash_at},
            )
        self.state = state
        self.clock_ms = clock_ms
        self.injections = injections or InjectionSet.empty()
        self.crash_at = crash_at

    def _crash(self, point: str) -> None:
        if self.crash_at == point:
            os._exit(CRASH_EXIT_CODE)

    def query(self, template_id: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
        template = self.state.query_template(template_id)
        params = merge_params(template, overrides)
        snapshot = self.state.read_snapshot()
        query_key = _query_key(template.id, params)
        query_id = _query_id(snapshot.version, query_key)
        existing = self._claim_query(
            query_id=query_id,
            query_key=query_key,
            template=template,
            params=params,
            snapshot=snapshot,
        )
        if isinstance(existing, _WaitForQuery):
            return self._await_query(existing.query_id)
        if existing is not None:
            return existing
        bound_snapshot = self.state.read_snapshot(snapshot.version)
        try:
            return self._run_claimed(
                query_id=query_id,
                query_key=query_key,
                template=template,
                params=params,
                snapshot=bound_snapshot,
            )
        except BizError as exc:
            self._record_failure(query_id, exc)
            raise

    def _claim_query(
        self,
        *,
        query_id: str,
        query_key: str,
        template: QueryTemplate,
        params: dict[str, Any],
        snapshot: SnapshotView,
    ) -> dict[str, Any] | _WaitForQuery | None:
        connection = self.state.connect()
        pending_error: BizError | None = None
        result: dict[str, Any] | None = None
        wait_for: str | None = None
        try:
            with transaction(connection):
                row = connection.execute(
                    "SELECT * FROM queries WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
                if row is None:
                    request = {
                        "params": params,
                        "query_key": query_key,
                        "rule_version": snapshot.version,
                        "template": template.to_dict(),
                    }
                    connection.execute(
                        "INSERT INTO queries("
                        "query_id, query_key, template_id, rule_version, rule_generation, "
                        "request_json, status, created_clock_ms, updated_clock_ms"
                        ") VALUES(?, ?, ?, ?, ?, ?, 'STARTED', ?, ?)",
                        (
                            query_id,
                            query_key,
                            template.id,
                            snapshot.version,
                            snapshot.generation,
                            canonical_json(request),
                            self.clock_ms,
                            self.clock_ms,
                        ),
                    )
                    self._audit(
                        connection,
                        query_id,
                        "QUERY_START",
                        "route",
                        {"rule_version": snapshot.version, "template_id": template.id},
                        clock_ms=self.clock_ms,
                    )
                elif row["status"] == "COMMITTED":
                    committed = connection.execute(
                        "SELECT result_json FROM query_results WHERE query_id = ?",
                        (query_id,),
                    ).fetchone()
                    if committed is None:
                        raise BizError(
                            "E_STATE_CORRUPT",
                            "query is committed but has no result record",
                            details={"query_id": query_id},
                        )
                    result = _json_load(committed["result_json"])
                elif row["status"] == "FAILED":
                    stored = row["error_json"]
                    if stored is None:
                        pending_error = BizError(
                            "E_QUERY_FAILED",
                            "query previously failed without stored diagnostics",
                            details={"query_id": query_id},
                        )
                    else:
                        pending_error = BizError.from_dict(_json_load(stored))
                else:
                    wait_for = query_id
                if row is not None and row["status"] != "COMMITTED":
                    connection.execute(
                        "UPDATE meta SET value = ? WHERE key = 'last_clock_ms' "
                        "AND CAST(value AS INTEGER) < ?",
                        (str(self.clock_ms), self.clock_ms),
                    )
        finally:
            connection.close()
        if pending_error is not None:
            raise pending_error
        if wait_for is not None:
            return _WaitForQuery(wait_for)
        return result

    def _await_query(self, query_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + QUERY_WAIT_TIMEOUT_SECONDS
        while True:
            connection = self.state.connect()
            try:
                connection.execute("BEGIN")
                row = connection.execute(
                    "SELECT status, error_json FROM queries WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
                result_row = connection.execute(
                    "SELECT result_json FROM query_results WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
                connection.execute("COMMIT")
            finally:
                connection.close()
            if row is not None:
                if row["status"] == "COMMITTED" and result_row is not None:
                    return _json_load(str(result_row["result_json"]))
                if row["status"] == "FAILED":
                    if row["error_json"] is not None:
                        raise BizError.from_dict(_json_load(str(row["error_json"])))
                    raise BizError(
                        "E_QUERY_FAILED",
                        "concurrent query failed without stored diagnostics",
                        details={"query_id": query_id},
                    )
            if time.monotonic() >= deadline:
                raise BizError(
                    "E_QUERY_IN_PROGRESS",
                    "the same query and rule version is still in progress",
                    details={"query_id": query_id},
                )
            time.sleep(0.01)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        query_id: str | None,
        event: str,
        stage: str | None,
        details: dict[str, Any],
        clock_ms: int | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO audit_log(query_id, event, stage, clock_ms, details_json) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                query_id,
                event,
                stage,
                clock_ms if clock_ms is not None else 0,
                canonical_json(details),
            ),
        )

    def _audit_at_clock(
        self,
        connection: sqlite3.Connection,
        query_id: str,
        event: str,
        stage: str | None,
        details: dict[str, Any],
    ) -> None:
        self._audit(connection, query_id, event, stage, details, self.clock_ms)

    def _set_status(
        self,
        query_id: str,
        status: str,
        *,
        target_shards_json: str | None = None,
        rewritten_json: str | None = None,
        event: str | None = None,
        stage: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        connection = self.state.connect()
        try:
            with transaction(connection):
                connection.execute(
                    "UPDATE queries SET status = ?, updated_clock_ms = ?, "
                    "target_shards_json = COALESCE(?, target_shards_json), "
                    "rewritten_json = COALESCE(?, rewritten_json) "
                    "WHERE query_id = ? AND status != 'COMMITTED'",
                    (
                        status,
                        self.clock_ms,
                        target_shards_json,
                        rewritten_json,
                        query_id,
                    ),
                )
                if event is not None:
                    self._audit_at_clock(
                        connection,
                        query_id,
                        event,
                        stage,
                        details or {},
                    )
                connection.execute(
                    "UPDATE meta SET value = ? WHERE key = 'last_clock_ms' "
                    "AND CAST(value AS INTEGER) < ?",
                    (str(self.clock_ms), self.clock_ms),
                )
        finally:
            connection.close()

    def _record_failure(self, query_id: str, exc: BizError) -> None:
        payload = exc.as_dict()
        connection = self.state.connect()
        try:
            with transaction(connection):
                row = connection.execute(
                    "SELECT status, error_json FROM queries WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
                if row is None or row["status"] == "COMMITTED":
                    return
                encoded = canonical_json(payload)
                if row["status"] == "FAILED" and row["error_json"] == encoded:
                    return
                connection.execute(
                    "UPDATE queries SET status = 'FAILED', error_json = ?, updated_clock_ms = ? "
                    "WHERE query_id = ? AND status != 'COMMITTED'",
                    (encoded, self.clock_ms, query_id),
                )
                self._audit_at_clock(
                    connection,
                    query_id,
                    "QUERY_FAILED",
                    exc.stage,
                    {
                        "code": exc.code,
                        "details": exc.details,
                        "query_template": exc.query_template,
                        "rule_version": exc.rule_version,
                        "target_shards": exc.target_shards,
                    },
                )
                connection.execute(
                    "UPDATE meta SET value = ? WHERE key = 'last_clock_ms' "
                    "AND CAST(value AS INTEGER) < ?",
                    (str(self.clock_ms), self.clock_ms),
                )
        finally:
            connection.close()

    def _run_claimed(
        self,
        *,
        query_id: str,
        query_key: str,
        template: QueryTemplate,
        params: dict[str, Any],
        snapshot: SnapshotView,
    ) -> dict[str, Any]:
        try:
            rewrite = build_rewrite(snapshot.snapshot, template, params, snapshot.version)
        except BizError as exc:
            exc.with_context(
                query_template=template.id,
                rule_version=snapshot.version,
                stage=exc.stage or "route",
            )
            self._record_failure(query_id, exc)
            raise
        route_scenario = self.injections.find("route", template.id, params)
        if route_scenario is not None:
            exc = BizError(
                route_scenario.code,
                "injected rule version conflict at routing stage",
                stage="route",
                query_template=template.id,
                target_shards=list(rewrite.target_shards),
                rule_version=snapshot.version,
                details={
                    **route_scenario.details,
                    "actual_rule_version": snapshot.version,
                    "scenario_id": route_scenario.id,
                },
            )
            self._record_failure(query_id, exc)
            raise exc
        self._set_status(
            query_id,
            "ROUTED",
            target_shards_json=canonical_json(list(rewrite.target_shards)),
            event="QUERY_ROUTED",
            stage="route",
            details={
                "full_route": rewrite.full_route,
                "target_shards": list(rewrite.target_shards),
            },
        )
        self._crash("after_route")
        rewrite_scenario = self.injections.find("rewrite", template.id, params)
        if rewrite_scenario is not None:
            exc = BizError(
                rewrite_scenario.code,
                "injected shard key range failure at rewrite stage",
                stage="rewrite",
                query_template=template.id,
                target_shards=list(rewrite.target_shards),
                rule_version=snapshot.version,
                details={**rewrite_scenario.details, "scenario_id": rewrite_scenario.id},
            )
            self._record_failure(query_id, exc)
            raise exc
        self._set_status(
            query_id,
            "REWRITTEN",
            rewritten_json=canonical_json(rewrite.public_dict()),
            event="QUERY_REWRITTEN",
            stage="rewrite",
            details={"rewritten_digest": rewrite.rewritten_digest},
        )
        self._crash("after_rewrite")
        self._set_status(
            query_id,
            "EXECUTING",
            event="QUERY_EXECUTING",
            stage="execute",
            details={"target_shards": list(rewrite.target_shards)},
        )
        for shard in rewrite.shards:
            scenario = self.injections.find(
                "execute", template.id, params, shard_id=shard.shard_id
            )
            if scenario is not None:
                payload = self._injected_execute_failure(
                    query_id=query_id,
                    shard=shard,
                    scenario=scenario,
                    template=template,
                    snapshot=snapshot,
                )
            else:
                payload = self._execute_shard(
                    query_id=query_id,
                    shard=shard,
                    template=template,
                )
            self._persist_outcome(query_id, payload)
        self._set_status(
            query_id,
            "EXECUTED",
            event="QUERY_EXECUTED",
            stage="execute",
            details={"target_shards": list(rewrite.target_shards)},
        )
        self._crash("after_execute")
        outcomes = self._load_outcomes(query_id)
        success_count = sum(1 for item in outcomes if item["status"] == "SUCCESS")
        if success_count == 0:
            codes = sorted(
                {
                    str(item["error_code"])
                    for item in outcomes
                    if item.get("error_code")
                }
            )
            code = codes[0] if len(codes) == 1 else "E_ALL_SHARDS_FAILED"
            exc = BizError(
                code,
                "all target shards failed during execution",
                stage="execute",
                query_template=template.id,
                target_shards=list(rewrite.target_shards),
                rule_version=snapshot.version,
                details={
                    "shard_outcomes": [_outcome_public(item) for item in outcomes],
                    "error_codes": codes,
                },
            )
            self._record_failure(query_id, exc)
            raise exc
        result = self._merge_result(
            query_id=query_id,
            query_key=query_key,
            template=template,
            params=params,
            rewrite=rewrite,
            outcomes=outcomes,
        )
        merge_scenario = self.injections.find("merge", template.id, params)
        if merge_scenario is not None:
            expected_outcome = next(
                (
                    item
                    for item in outcomes
                    if item["shard_id"] == merge_scenario.shard_id
                    and item["status"] == "SUCCESS"
                ),
                None,
            )
            expected = (
                expected_outcome.get("result_digest") if expected_outcome is not None else None
            )
            actual = digest({"tampered_digest": expected})
            exc = BizError(
                merge_scenario.code,
                "injected per-shard result digest mismatch at merge stage",
                stage="merge",
                query_template=template.id,
                target_shards=list(rewrite.target_shards),
                rule_version=snapshot.version,
                details={
                    **merge_scenario.details,
                    "actual_digest": actual,
                    "expected_digest": expected,
                    "scenario_id": merge_scenario.id,
                    "shard_id": merge_scenario.shard_id,
                    "shard_outcomes": [_outcome_public(item) for item in outcomes],
                },
            )
            self._record_failure(query_id, exc)
            raise exc
        self._stage_merge(query_id, result)
        self._crash("after_merge")
        return self._commit_result(query_id, result)

    def _execute_shard(
        self,
        *,
        query_id: str,
        shard: Any,
        template: QueryTemplate,
    ) -> dict[str, Any]:
        database_path = self.state.sample_root() / shard.database
        connection: sqlite3.Connection | None = None
        try:
            uri = database_path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            connection.execute("PRAGMA query_only = ON")
            cursor = connection.execute(shard.sql)
            rows = cursor.fetchall()
            if template.operation == "sum_count":
                if len(rows) != 1:
                    raise BizError(
                        "E_AGGREGATE_SHAPE",
                        "sum_count query must return exactly one row per shard",
                        details={"rows_returned": len(rows)},
                    )
                names = [str(item[0]) for item in cursor.description]
                row = dict(zip(names, rows[0]))
                total_amount = row.get("total_amount")
                row_count = row.get("row_count")
                if type(total_amount) is not int or type(row_count) is not int:
                    raise BizError(
                        "E_AGGREGATE_TYPE",
                        "sum_count query must return integer total_amount and row_count",
                        details={
                            "row_count_type": type(row_count).__name__,
                            "total_amount_type": type(total_amount).__name__,
                        },
                    )
                payload_value = {
                    "row_count": row_count,
                    "total_amount": total_amount,
                }
                rows_returned = row_count
            else:
                columns = [str(item[0]) for item in cursor.description]
                payload_rows = []
                for row in rows:
                    payload_rows.append(
                        {
                            columns[index]: row[index]
                            for index in range(len(columns))
                        }
                    )
                payload_value = {"columns": columns, "rows": payload_rows}
                rows_returned = len(payload_rows)
            payload = {
                "database": shard.database,
                "error_code": None,
                "error_details": {},
                "error_stage": None,
                "operation": template.operation,
                "result": payload_value,
                "result_digest": digest(payload_value),
                "rows_returned": rows_returned,
                "shard_id": shard.shard_id,
                "status": "SUCCESS",
                "table": shard.table,
            }
            self._audit_shard(query_id, "SHARD_SUCCEEDED", shard.shard_id, payload)
            return payload
        except sqlite3.Error as exc:
            code = (
                "E_PHYSICAL_TABLE_MISSING"
                if "no such table" in str(exc).lower()
                else "E_SHARD_EXECUTION_FAILED"
            )
            payload = {
                "database": shard.database,
                "error_code": code,
                "error_details": {"reason": str(exc)},
                "error_stage": "execute",
                "operation": template.operation,
                "result": None,
                "result_digest": None,
                "rows_returned": 0,
                "shard_id": shard.shard_id,
                "status": "FAILED",
                "table": shard.table,
            }
            self._audit_shard(query_id, "SHARD_FAILED", shard.shard_id, payload)
            return payload
        except BizError as exc:
            payload = {
                "database": shard.database,
                "error_code": exc.code,
                "error_details": exc.details,
                "error_stage": "execute",
                "operation": template.operation,
                "result": None,
                "result_digest": None,
                "rows_returned": 0,
                "shard_id": shard.shard_id,
                "status": "FAILED",
                "table": shard.table,
            }
            self._audit_shard(query_id, "SHARD_FAILED", shard.shard_id, payload)
            return payload
        finally:
            if connection is not None:
                connection.close()

    def _audit_shard(
        self,
        query_id: str,
        event: str,
        shard_id: int,
        payload: dict[str, Any],
    ) -> None:
        connection = self.state.connect()
        try:
            with transaction(connection):
                self._audit_at_clock(
                    connection,
                    query_id,
                    event,
                    "execute",
                    {
                        "error_code": payload.get("error_code"),
                        "result_digest": payload.get("result_digest"),
                        "shard_id": shard_id,
                        "status": payload["status"],
                    },
                )
        finally:
            connection.close()

    def _injected_execute_failure(
        self,
        *,
        query_id: str,
        shard: Any,
        scenario: Scenario,
        template: QueryTemplate,
        snapshot: SnapshotView,
    ) -> dict[str, Any]:
        payload = {
            "database": shard.database,
            "error_code": scenario.code,
            "error_details": {
                **scenario.details,
                "query_template": template.id,
                "rule_version": snapshot.version,
                "scenario_id": scenario.id,
                "target_shard": shard.shard_id,
            },
            "error_stage": "execute",
            "operation": template.operation,
            "result": None,
            "result_digest": None,
            "rows_returned": 0,
            "shard_id": shard.shard_id,
            "status": "FAILED",
            "table": shard.table,
        }
        self._audit_shard(query_id, "SHARD_FAILED", shard.shard_id, payload)
        return payload

    def _persist_outcome(self, query_id: str, payload: dict[str, Any]) -> None:
        connection = self.state.connect()
        try:
            with transaction(connection):
                connection.execute(
                    "INSERT INTO shard_outcomes("
                    "query_id, shard_id, status, error_code, result_digest, payload_json"
                    ") VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(query_id, shard_id) DO UPDATE SET "
                    "status = excluded.status, error_code = excluded.error_code, "
                    "result_digest = excluded.result_digest, payload_json = excluded.payload_json",
                    (
                        query_id,
                        int(payload["shard_id"]),
                        str(payload["status"]),
                        payload.get("error_code"),
                        payload.get("result_digest"),
                        canonical_json(payload),
                    ),
                )
        finally:
            connection.close()

    def _load_outcomes(self, query_id: str) -> list[dict[str, Any]]:
        connection = self.state.connect()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT payload_json FROM shard_outcomes WHERE query_id = ? ORDER BY shard_id",
                (query_id,),
            ).fetchall()
            connection.execute("COMMIT")
        finally:
            connection.close()
        return [_json_load(str(row["payload_json"])) for row in rows]

    def _merge_result(
        self,
        *,
        query_id: str,
        query_key: str,
        template: QueryTemplate,
        params: dict[str, Any],
        rewrite: RewriteResult,
        outcomes: list[dict[str, Any]],
    ) -> dict[str, Any]:
        successful = [item for item in outcomes if item["status"] == "SUCCESS"]
        failures = [item for item in outcomes if item["status"] != "SUCCESS"]
        if template.operation == "sum_count":
            total_amount = 0
            row_count = 0
            for outcome in successful:
                value = outcome["result"]
                total_amount += int(value["total_amount"])
                row_count += int(value["row_count"])
            aggregate: dict[str, Any] | None = {
                "row_count": row_count,
                "total_amount": total_amount,
            }
            columns: list[str] = ["total_amount", "row_count"]
            rows: list[dict[str, Any]] = []
        else:
            columns = []
            all_rows: list[dict[str, Any]] = []
            for outcome in successful:
                value = outcome["result"]
                if not columns:
                    columns = list(value["columns"])
                elif columns != list(value["columns"]):
                    raise BizError(
                        "E_MERGE_SCHEMA_MISMATCH",
                        "successful shards returned different result columns",
                        stage="merge",
                        query_template=template.id,
                        target_shards=list(rewrite.target_shards),
                        rule_version=rewrite.rule_version,
                        details={
                            "expected_columns": columns,
                            "shard_columns": list(value["columns"]),
                        },
                    )
                all_rows.extend(value["rows"])
            order_by = list(template.order_by)

            def row_key(row: dict[str, Any]) -> tuple[Any, ...]:
                ordered = tuple(_sortable(row.get(column)) for column in order_by)
                return ordered + (canonical_json(row),)

            all_rows.sort(key=row_key)
            rows = all_rows
            aggregate = None
        error_codes = sorted(
            {
                str(item["error_code"])
                for item in failures
                if item.get("error_code") is not None
            }
        )
        if failures:
            overall_status = "PARTIAL"
            error_code: str | None = "E_PARTIAL_SHARD_FAILURE"
            error_stage: str | None = "execute"
        else:
            overall_status = "SUCCESS"
            error_code = None
            error_stage = None
        result: dict[str, Any] = {
            "aggregate": aggregate,
            "columns": columns,
            "error_code": error_code,
            "error_codes": error_codes,
            "error_stage": error_stage,
            "logical_clock_ms": self.clock_ms,
            "overall_status": overall_status,
            "query_id": query_id,
            "query_key": query_key,
            "request": {"params": params, "query_template": template.id},
            "rows": rows,
            "rule_version": rewrite.rule_version,
            "shard_outcomes": [_outcome_public(item) for item in outcomes],
            "target_shards": list(rewrite.target_shards),
        }
        result_without_digest = dict(result)
        result["result_digest"] = digest(result_without_digest)
        return result

    def _stage_merge(self, query_id: str, result: dict[str, Any]) -> None:
        connection = self.state.connect()
        try:
            with transaction(connection):
                connection.execute(
                    "INSERT INTO query_staging(query_id, payload_json, result_digest, created_clock_ms) "
                    "VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(query_id) DO UPDATE SET "
                    "payload_json = excluded.payload_json, result_digest = excluded.result_digest, "
                    "created_clock_ms = excluded.created_clock_ms",
                    (
                        query_id,
                        canonical_json(result),
                        str(result["result_digest"]),
                        self.clock_ms,
                    ),
                )
                connection.execute(
                    "UPDATE queries SET status = 'MERGED', updated_clock_ms = ? "
                    "WHERE query_id = ? AND status != 'COMMITTED'",
                    (self.clock_ms, query_id),
                )
                self._audit_at_clock(
                    connection,
                    query_id,
                    "QUERY_MERGED",
                    "merge",
                    {"result_digest": result["result_digest"]},
                )
        finally:
            connection.close()

    def _commit_result(self, query_id: str, result: dict[str, Any]) -> dict[str, Any]:
        result_json = canonical_json(result)
        connection = self.state.connect()
        try:
            with transaction(connection):
                existing = connection.execute(
                    "SELECT result_json, result_digest FROM query_results WHERE query_id = ?",
                    (query_id,),
                ).fetchone()
                if existing is not None:
                    if existing["result_digest"] != result["result_digest"]:
                        raise BizError(
                            "E_RESULT_CONFLICT",
                            "a different result was already committed for this query",
                            details={
                                "committed_digest": existing["result_digest"],
                                "computed_digest": result["result_digest"],
                            },
                        )
                    committed = _json_load(existing["result_json"])
                else:
                    query_row = connection.execute(
                        "SELECT query_key, rule_version FROM queries WHERE query_id = ?",
                        (query_id,),
                    ).fetchone()
                    if query_row is None:
                        raise BizError(
                            "E_STATE_CORRUPT",
                            "query audit record is missing before commit",
                            details={"query_id": query_id},
                        )
                    connection.execute(
                        "INSERT INTO query_results("
                        "query_id, query_key, rule_version, result_json, result_digest, committed_clock_ms"
                        ") VALUES(?, ?, ?, ?, ?, ?)",
                        (
                            query_id,
                            query_row["query_key"],
                            query_row["rule_version"],
                            result_json,
                            result["result_digest"],
                            self.clock_ms,
                        ),
                    )
                    connection.execute(
                        "UPDATE queries SET status = 'COMMITTED', result_json = ?, "
                        "result_digest = ?, error_json = NULL, updated_clock_ms = ?, "
                        "committed_clock_ms = ? WHERE query_id = ?",
                        (
                            result_json,
                            result["result_digest"],
                            self.clock_ms,
                            self.clock_ms,
                            query_id,
                        ),
                    )
                    self._audit_at_clock(
                        connection,
                        query_id,
                        "QUERY_COMMITTED",
                        "merge",
                        {"result_digest": result["result_digest"]},
                    )
                    committed = result
                connection.execute(
                    "DELETE FROM query_staging WHERE query_id = ?",
                    (query_id,),
                )
                connection.execute(
                    "UPDATE meta SET value = ? WHERE key = 'last_clock_ms' "
                    "AND CAST(value AS INTEGER) < ?",
                    (str(self.clock_ms), self.clock_ms),
                )
        finally:
            connection.close()
        return committed

    def resume(self) -> dict[str, Any]:
        with self.state.lock():
            return self._resume_locked()

    def _resume_locked(self) -> dict[str, Any]:
        connection = self.state.connect()
        try:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT query_id FROM queries WHERE status IN "
                "('STARTED','ROUTED','REWRITTEN','EXECUTING','EXECUTED','MERGED') "
                "ORDER BY query_id"
            ).fetchall()
            connection.execute("COMMIT")
        finally:
            connection.close()
        cleaned: list[dict[str, Any]] = []
        completed: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for listed in rows:
            query_id = str(listed["query_id"])
            claimed = self._claim_resume(query_id)
            if claimed is None:
                continue
            cleaned.append(
                {
                    "query_id": query_id,
                    "removed_shard_outcomes": claimed["removed_shard_outcomes"],
                    "removed_staging": claimed["removed_staging"],
                }
            )
            request = claimed["request"]
            template = QueryTemplate.from_dict(request["template"])
            params = request["params"]
            try:
                snapshot = self.state.read_snapshot(str(request["rule_version"]))
                result = self._run_claimed(
                    query_id=query_id,
                    query_key=str(request["query_key"]),
                    template=template,
                    params=params,
                    snapshot=snapshot,
                )
                completed.append(result)
            except BizError as exc:
                self._record_failure(query_id, exc)
                failures.append({"error": exc.as_dict(), "query_id": query_id})
        return {
            "cleaned": cleaned,
            "completed": completed,
            "failures": failures,
            "resumed_count": len(cleaned),
            "status": "OK" if not failures else "PARTIAL",
        }

    def _claim_resume(self, query_id: str) -> dict[str, Any] | None:
        connection = self.state.connect()
        try:
            with transaction(connection):
                row = connection.execute(
                    "SELECT * FROM queries WHERE query_id = ? AND status IN "
                    "('STARTED','ROUTED','REWRITTEN','EXECUTING','EXECUTED','MERGED')",
                    (query_id,),
                ).fetchone()
                if row is None:
                    return None
                removed_outcomes = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM shard_outcomes WHERE query_id = ?",
                        (query_id,),
                    ).fetchone()["count"]
                )
                removed_staging = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM query_staging WHERE query_id = ?",
                        (query_id,),
                    ).fetchone()["count"]
                )
                connection.execute(
                    "DELETE FROM shard_outcomes WHERE query_id = ?",
                    (query_id,),
                )
                connection.execute(
                    "DELETE FROM query_staging WHERE query_id = ?",
                    (query_id,),
                )
                connection.execute(
                    "UPDATE queries SET status = 'STARTED', target_shards_json = NULL, "
                    "rewritten_json = NULL, result_json = NULL, result_digest = NULL, "
                    "error_json = NULL, updated_clock_ms = ? WHERE query_id = ?",
                    (self.clock_ms, query_id),
                )
                self._audit_at_clock(
                    connection,
                    query_id,
                    "QUERY_RESUME_CLEAN",
                    "resume",
                    {
                        "removed_shard_outcomes": removed_outcomes,
                        "removed_staging": removed_staging,
                    },
                )
                request = _json_load(str(row["request_json"]))
                return {
                    "removed_shard_outcomes": removed_outcomes,
                    "removed_staging": removed_staging,
                    "request": request,
                }
        finally:
            connection.close()

