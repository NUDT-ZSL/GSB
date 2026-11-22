from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable

from .config import load_sample
from .errors import BusinessError, fail
from .models import LoadedSample, LogicalTable, MergedRule, PhysicalMapping, Template
from .router import build_plan, plan_digest_without_self, route_key
from .rules import (
    build_rule_models,
    load_v1_rules,
    load_v2_rules,
    merged_rule_from_dict,
    rule_for_clock,
    select_snapshot_rule,
)
from .store import Store
from .util import canonical_json, digest, quote_identifier


def _column_ddl(column: Any) -> str:
    parts = [quote_identifier(column.name), column.sql_type]
    if column.primary_key:
        parts.append("PRIMARY KEY")
    if column.not_null:
        parts.append("NOT NULL")
    if column.generated_expression is not None:
        storage = "STORED" if column.generated_stored else "VIRTUAL"
        parts.extend(
            [
                "GENERATED ALWAYS AS",
                f"({column.generated_expression})",
                storage,
            ]
        )
    return " ".join(parts)


def _create_physical_table(
    connection: sqlite3.Connection,
    table: LogicalTable,
    physical_table: str,
) -> None:
    columns_sql = ", ".join(_column_ddl(column) for column in table.columns)
    connection.execute(
        f"CREATE TABLE {quote_identifier(physical_table)} ({columns_sql})"
    )
    if table.shard_key in table.column_names:
        index_name = f"idx_{physical_table}_{table.shard_key}"
        connection.execute(
            f"CREATE INDEX {quote_identifier(index_name)} "
            f"ON {quote_identifier(physical_table)} "
            f"({quote_identifier(table.shard_key)})"
        )


def _seed_assignments(
    sample: LoadedSample,
    *,
    initial_clock_ms: int,
) -> dict[Path, list[tuple[LogicalTable, PhysicalMapping, list[dict[str, Any]]]]]:
    rule = rule_for_clock(sample.merged_rules, initial_clock_ms)
    grouped: dict[Path, dict[tuple[str, int], tuple[LogicalTable, PhysicalMapping, list[dict[str, Any]]]]] = {}
    for table_name, table in sample.tables.items():
        routing = rule.routing[table_name]
        rows = sample.seed_rows.get(table_name, [])
        for mapping in sample.mappings[table_name]:
            key = (table_name, mapping.shard_no)
            target = grouped.setdefault(mapping.database, {})
            target.setdefault(key, (table, mapping, []))
        for row in rows:
            shard_no = route_key(table, routing, int(row[table.shard_key]))
            mapping = sample.mappings[table_name][shard_no]
            key = (table_name, shard_no)
            target = grouped.setdefault(mapping.database, {})
            if key not in target:
                target[key] = (table, mapping, [])
            target[key][2].append(row)
    return {
        database: [items[key] for key in sorted(items)]
        for database, items in grouped.items()
    }


def initialize_shard_databases(
    sample: LoadedSample,
    *,
    initial_clock_ms: int,
) -> dict[str, Any]:
    assignments = _seed_assignments(sample, initial_clock_ms=initial_clock_ms)
    _preflight_physical_schema(assignments)
    created: list[dict[str, Any]] = []
    all_databases = {
        mapping.database
        for mappings in sample.mappings.values()
        for mapping in mappings
    }
    for database in sorted(all_databases):
        database.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{database.name}.",
            suffix=".tmp",
            dir=str(database.parent),
        )
        os.close(fd)
        temporary = Path(temporary_name)
        temporary.unlink()
        try:
            connection = sqlite3.connect(temporary, timeout=30)
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute("BEGIN IMMEDIATE")
                for table, mapping, rows in assignments.get(database, []):
                    _create_physical_table(
                        connection,
                        table,
                        mapping.physical_table,
                    )
                    insert_columns = [
                        column.name for column in table.non_generated_columns
                    ]
                    placeholders = ", ".join("?" for _ in insert_columns)
                    column_sql = ", ".join(
                        quote_identifier(column) for column in insert_columns
                    )
                    for row in rows:
                        connection.execute(
                            f"INSERT INTO {quote_identifier(mapping.physical_table)} "
                            f"({column_sql}) VALUES ({placeholders})",
                            tuple(row.get(column) for column in insert_columns),
                        )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            os.replace(temporary, database)
            created.append(
                {
                    "database": str(database),
                    "tables": [
                        {
                            "logical_table": table.name,
                            "physical_table": mapping.physical_table,
                            "shard_no": mapping.shard_no,
                            "rows": len(rows),
                        }
                        for table, mapping, rows in assignments.get(database, [])
                    ],
                }
            )
        finally:
            if temporary.exists():
                temporary.unlink()
    return {"databases": created}


def _preflight_physical_schema(
    assignments: dict[
        Path, list[tuple[LogicalTable, PhysicalMapping, list[dict[str, Any]]]]
    ],
) -> None:
    for database, items in sorted(assignments.items()):
        seen_tables: set[str] = set()
        for table, mapping, rows in items:
            if mapping.physical_table in seen_tables:
                fail(
                    "E_MAPPING_INCOMPLETE",
                    "physical table names must be unique within one SQLite database",
                    database=str(database),
                    physical_table=mapping.physical_table,
                )
            seen_tables.add(mapping.physical_table)
            connection = sqlite3.connect(":memory:")
            try:
                _create_physical_table(connection, table, mapping.physical_table)
                insert_columns = [
                    column.name for column in table.non_generated_columns
                ]
                placeholders = ", ".join("?" for _ in insert_columns)
                column_sql = ", ".join(
                    quote_identifier(column) for column in insert_columns
                )
                for row in rows:
                    connection.execute(
                        f"INSERT INTO {quote_identifier(mapping.physical_table)} "
                        f"({column_sql}) VALUES ({placeholders})",
                        tuple(row.get(column) for column in insert_columns),
                    )
                connection.commit()
            except sqlite3.Error as exc:
                raise BusinessError(
                    code="E_VALIDATION",
                    message="physical schema or seed data failed preflight",
                    details={
                        "table": table.name,
                        "shard_no": mapping.shard_no,
                        "database": str(database),
                        "error": str(exc),
                    },
                ) from exc
            finally:
                connection.close()


def parse_params(raw: str | None) -> dict[str, Any]:
    if raw is None or raw == "":
        return {}
    if raw == "-":
        raw = __import__("sys").stdin.read()
    elif raw.startswith("@"):
        path = Path(raw[1:])
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            fail("E_VALIDATION", "cannot read params file", path=str(path), error=str(exc))
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        fail(
            "E_VALIDATION",
            "params must be a JSON object",
            line=exc.lineno,
            column=exc.colno,
            error=str(exc),
        )
    if not isinstance(value, dict):
        fail("E_VALIDATION", "params must be a JSON object")
    return value


def _error_from_persisted(value: dict[str, Any] | None) -> BusinessError:
    if not value:
        return BusinessError(
            code="E_QUERY_INCOMPLETE",
            message="query is in a failed state without a persisted error",
        )
    return BusinessError(
        code=str(value.get("code", "E_QUERY_INCOMPLETE")),
        message=str(value.get("message", "persisted query failure")),
        details=value.get("details", {}),
    )


class Engine:
    def __init__(
        self,
        data_dir: Path,
        *,
        injection_path: Path | None = None,
        injections_enabled: bool = True,
    ):
        self.data_dir = data_dir.resolve()
        self.store = Store(self.data_dir)
        self.injection_path = injection_path
        self.injections_enabled = injections_enabled
        self._sample: LoadedSample | None = None
        self._injections: list[dict[str, Any]] = []

    def _load(
        self,
        *,
        source_filter: str = "merged",
        v1_base_clock_ms: int | None = None,
        load_rule_snapshots: bool = True,
    ) -> LoadedSample:
        injection_path = None
        if self.injections_enabled:
            injection_path = self.injection_path
            if injection_path is None:
                default = self.data_dir / "injections.json"
                if default.exists():
                    injection_path = default
        sample = load_sample(
            self.data_dir,
            injection_path=injection_path,
            v1_base_clock_ms=v1_base_clock_ms,
            source_filter=source_filter,
            load_rule_snapshots=load_rule_snapshots,
        )
        self._sample = sample
        self._injections = sample.injections
        return sample

    def load(
        self,
        *,
        source_filter: str = "merged",
        now_clock_ms: int | None = None,
        v1_base_clock_ms: int | None = None,
    ) -> dict[str, Any]:
        sample = self._load(
            source_filter=source_filter,
            v1_base_clock_ms=v1_base_clock_ms,
            load_rule_snapshots=True,
        )
        initial_clock = sample.initial_clock_ms if now_clock_ms is None else now_clock_ms
        if not isinstance(initial_clock, int) or isinstance(initial_clock, bool):
            fail("E_VALIDATION", "now-ms must be an integer")
        if initial_clock < 0:
            fail("E_VALIDATION", "now-ms must be non-negative")
        with self.store.lock():
            shard_summary = initialize_shard_databases(
                sample,
                initial_clock_ms=initial_clock,
            )
            self.store.initialize_from_sample(
                sample,
                initial_clock_ms=initial_clock,
            )
        status = self.store.status()
        return {
            "ok": True,
            "command": "load",
            "data_dir": str(self.data_dir),
            "logical_clock_ms": initial_clock,
            "source_filter": source_filter,
            "tables": sorted(sample.tables),
            "templates": sorted(sample.templates),
            "rule_versions": [
                {
                    "version": rule.version,
                    "digest": rule.digest,
                    "effective_clock_ms": rule.effective_clock_ms,
                    "generation": rule.generation,
                    "source": rule.source,
                }
                for rule in sample.merged_rules
            ],
            "current_rule": status["current_rule"],
            "shards": shard_summary["databases"],
        }

    def _match_injection(
        self,
        *,
        stage: str,
        template: Template,
        query_id: str | None,
        params: dict[str, Any],
        shard_no: int | None = None,
        match_shard: bool = False,
    ) -> dict[str, Any] | None:
        for injection in self._injections:
            if injection["stage"] != stage:
                continue
            if injection.get("template_id") not in (None, template.template_id):
                continue
            if injection.get("query_id") not in (None, query_id):
                continue
            if (
                match_shard
                and injection.get("shard_no") is not None
                and injection["shard_no"] != shard_no
            ):
                continue
            expected_params = injection.get("params")
            if isinstance(expected_params, dict):
                if any(params.get(key) != value for key, value in expected_params.items()):
                    continue
            return injection
        return None

    def _injection_error(
        self,
        injection: dict[str, Any],
        *,
        template: Template,
        rule: MergedRule,
        query_id: str | None,
        shard_no: int | None,
        extra: dict[str, Any] | None = None,
    ) -> BusinessError:
        details = {
            "stage": injection["stage"],
            "template_id": template.template_id,
            "rule_version": rule.version,
            "query_id": query_id,
            "shard_no": shard_no,
            "error_type": injection["error_type"],
            "injection_id": injection["id"],
            "message": injection["message"],
        }
        if extra:
            details.update(extra)
        if isinstance(injection.get("details"), dict):
            details.update(injection["details"])
        return BusinessError(
            code=injection["code"],
            message=injection["message"],
            details=details,
        )

    def _apply_phase_injection(
        self,
        *,
        stage: str,
        template: Template,
        rule: MergedRule,
        query_id: str | None,
        params: dict[str, Any],
        shard_no: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        injection = self._match_injection(
            stage=stage,
            template=template,
            query_id=query_id,
            params=params,
            shard_no=shard_no,
        )
        if injection is None:
            return
        if injection["action"] == "none":
            return
        if injection["action"] == "delay":
            time.sleep(injection["delay_ms"] / 1000.0)
            return
        if injection["action"] == "crash":
            os._exit(137)
        if injection["action"] == "corrupt_digest":
            return
        raise self._injection_error(
            injection,
            template=template,
            rule=rule,
            query_id=query_id,
            shard_no=shard_no,
            extra=extra,
        )

    def route(
        self,
        *,
        template_id: str,
        params: dict[str, Any],
        source_filter: str = "merged",
        now_clock_ms: int | None = None,
        v1_base_clock_ms: int | None = None,
    ) -> dict[str, Any]:
        sample = self._load(
            source_filter=source_filter,
            v1_base_clock_ms=v1_base_clock_ms,
        )
        template = sample.templates.get(template_id)
        if template is None:
            fail("E_TEMPLATE_INVALID", "template does not exist", template=template_id)
        if source_filter == "v1":
            rules = sample.v1_versions
        elif source_filter == "v2":
            rules = sample.v2_versions
        else:
            rules = sample.merged_rules
        clock = (
            self.store.status()["logical_clock_ms"]
            if now_clock_ms is None
            else now_clock_ms
        )
        if not isinstance(clock, int) or isinstance(clock, bool):
            fail("E_VALIDATION", "now-ms must be an integer")
        rule = rule_for_clock(rules, clock)
        try:
            self._apply_phase_injection(
                stage="route",
                template=template,
                rule=rule,
                query_id=None,
                params=params,
            )
            self._apply_phase_injection(
                stage="rewrite",
                template=template,
                rule=rule,
                query_id=None,
                params=params,
            )
        except BusinessError:
            raise
        table = sample.tables[template.logical_table]
        plan = build_plan(
            template=template,
            table=table,
            rule=rule,
            mappings=sample.mappings[table.name],
            params=params,
        )
        return {
            "ok": True,
            "command": "route",
            "source_filter": source_filter,
            "logical_clock_ms": clock,
            "plan": plan,
        }

    def query(
        self,
        *,
        template_id: str,
        params: dict[str, Any],
        query_id: str | None = None,
        now_clock_ms: int | None = None,
        source_filter: str = "merged",
        v1_base_clock_ms: int | None = None,
        retry_failed: bool = False,
    ) -> dict[str, Any]:
        sample = self._load(
            source_filter=source_filter,
            v1_base_clock_ms=v1_base_clock_ms,
            load_rule_snapshots=False,
        )
        if not self.store.path.exists():
            fail(
                "E_STATE_NOT_LOADED",
                "state is not initialized; run load first",
                path=str(self.store.path),
            )
        template = sample.templates.get(template_id)
        if template is None:
            fail("E_TEMPLATE_INVALID", "template does not exist", template=template_id)
        clock = (
            self.store.status()["logical_clock_ms"]
            if now_clock_ms is None
            else now_clock_ms
        )
        if not isinstance(clock, int) or isinstance(clock, bool):
            fail("E_VALIDATION", "now-ms must be an integer")
        request = {
            "template_id": template_id,
            "params": params,
            "source_filter": source_filter,
        }
        started = self.store.begin_query(
            template_id=template_id,
            params=params,
            source_filter=source_filter,
            request_json=request,
            requested_query_id=query_id,
            clock_ms=clock,
        )
        bound_query_id = started["query_id"]
        rule = merged_rule_from_dict(started["rule"])
        existing = started["query"]
        if existing["status"] == "committed":
            return json.loads(existing["result_json"])
        if existing["status"] == "failed" and not retry_failed:
            raise _error_from_persisted(
                json.loads(existing["error_json"]) if existing["error_json"] else None
            )
        if existing["status"] == "failed" and retry_failed:
            fail(
                "E_RESUME_UNSAFE",
                "terminal failed queries cannot be retried without a new query id",
                query_id=bound_query_id,
            )
        try:
            return self._drive_query(
                sample=sample,
                template=template,
                rule=rule,
                query_id=bound_query_id,
                params=params,
                clock_ms=max(clock, started["clock_ms"]),
                resume=not started["created"],
            )
        except BusinessError as error:
            if getattr(error, "_persisted_error", False):
                raise
            stage = str(error.details.get("stage", "route"))
            self.store.mark_query_error(
                query_id=bound_query_id,
                stage=stage,
                error=error,
                clock_ms=max(clock, started["clock_ms"]),
            )
            raise

    def _drive_query(
        self,
        *,
        sample: LoadedSample,
        template: Template,
        rule: MergedRule,
        query_id: str,
        params: dict[str, Any],
        clock_ms: int,
        resume: bool,
    ) -> dict[str, Any]:
        query = self.store.get_query_with_shards(query_id)
        if query is None:
            fail("E_QUERY_NOT_FOUND", "query does not exist", query_id=query_id)
        if query["status"] == "committed":
            return json.loads(query["result_json"])
        if query["status"] == "failed":
            raise _error_from_persisted(
                json.loads(query["error_json"]) if query["error_json"] else None
            )

        if query["plan_json"] is None:
            self._apply_phase_injection(
                stage="route",
                template=template,
                rule=rule,
                query_id=query_id,
                params=params,
            )
            self._apply_phase_injection(
                stage="rewrite",
                template=template,
                rule=rule,
                query_id=query_id,
                params=params,
            )
            table = sample.tables[template.logical_table]
            plan = build_plan(
                template=template,
                table=table,
                rule=rule,
                mappings=sample.mappings[table.name],
                params=params,
            )
            saved = self.store.save_plan(
                query_id=query_id,
                plan=plan,
                clock_ms=clock_ms,
            )
            if saved.get("already_committed"):
                commit = saved["commit"]
                return json.loads(commit["result_json"])
        else:
            plan = json.loads(query["plan_json"])
            if plan.get("plan_digest") != plan_digest_without_self(plan):
                fail(
                    "E_STATE_CORRUPT",
                    "persisted plan digest is invalid",
                    query_id=query_id,
                )
            table = sample.tables[template.logical_table]

        query = self.store.get_query_with_shards(query_id)
        assert query is not None
        if query["status"] == "committed":
            return json.loads(query["result_json"])
        shards_by_no = {
            int(item["shard_no"]): item for item in query["shards"]
        }
        for shard_plan in plan["shards"]:
            shard_no = int(shard_plan["shard_no"])
            persisted = shards_by_no.get(shard_no)
            if persisted is not None and persisted["status"] in {
                "succeeded",
                "failed",
                "abandoned",
            }:
                continue
            injection = self._match_injection(
                stage="execute",
                template=template,
                query_id=query_id,
                params=params,
                shard_no=shard_no,
                match_shard=True,
            )
            if injection is not None and injection["action"] == "delay":
                time.sleep(injection["delay_ms"] / 1000.0)
            if (
                injection is not None
                and injection["action"] == "crash"
                and injection.get("position") != "after_persist"
            ):
                os._exit(137)
            if injection is not None and injection["action"] == "abandon_shard":
                raw_error = self._injection_error(
                    injection,
                    template=template,
                    rule=rule,
                    query_id=query_id,
                    shard_no=shard_no,
                ).to_dict()
                self.store.record_shard_result(
                    query_id=query_id,
                    shard_no=shard_no,
                    status="abandoned",
                    rows=None,
                    row_count=None,
                    sum_value=None,
                    result_digest=None,
                    error_code=injection["code"],
                    error_json=raw_error,
                    clock_ms=clock_ms,
                )
                continue
            if injection is not None and injection["action"] in {"fail_shard", "raise"}:
                raw_error = self._injection_error(
                    injection,
                    template=template,
                    rule=rule,
                    query_id=query_id,
                    shard_no=shard_no,
                ).to_dict()
                raw_error["raw_error"] = {
                    "exception": "Injected" + "".join(
                        part.capitalize()
                        for part in injection["error_type"].split("_")
                    ),
                    "message": injection["message"],
                    "error_type": injection["error_type"],
                }
                self.store.record_shard_result(
                    query_id=query_id,
                    shard_no=shard_no,
                    status="failed",
                    rows=None,
                    row_count=None,
                    sum_value=None,
                    result_digest=None,
                    error_code=injection["code"],
                    error_json=raw_error,
                    clock_ms=clock_ms,
                )
                continue
            try:
                rows, row_count, sum_value = self._execute_shard(
                    shard_plan,
                    template=template,
                )
            except sqlite3.Error as exc:
                error = BusinessError(
                    code="E_EXECUTION_FAILED",
                    message="physical shard query failed",
                    details={
                        "stage": "execute",
                        "template_id": template.template_id,
                        "rule_version": rule.version,
                        "query_id": query_id,
                        "shard_no": shard_no,
                        "raw_error": {
                            "exception": type(exc).__name__,
                            "message": str(exc),
                            "sqlite_errorcode": getattr(exc, "sqlite_errorcode", None),
                            "sqlite_errorname": getattr(exc, "sqlite_errorname", None),
                            "sql": shard_plan["sql"],
                        },
                    },
                )
                self.store.record_shard_result(
                    query_id=query_id,
                    shard_no=shard_no,
                    status="failed",
                    rows=None,
                    row_count=None,
                    sum_value=None,
                    result_digest=None,
                    error_code=error.code,
                    error_json=error.to_dict(),
                    clock_ms=clock_ms,
                )
                continue
            except BusinessError as error:
                error.details.setdefault("stage", "execute")
                error.details.setdefault("template_id", template.template_id)
                error.details.setdefault("rule_version", rule.version)
                error.details.setdefault("query_id", query_id)
                error.details.setdefault("shard_no", shard_no)
                self.store.record_shard_result(
                    query_id=query_id,
                    shard_no=shard_no,
                    status="failed",
                    rows=None,
                    row_count=None,
                    sum_value=None,
                    result_digest=None,
                    error_code=error.code,
                    error_json=error.to_dict(),
                    clock_ms=clock_ms,
                )
                continue
            result_digest = digest(
                {
                    "query_id": query_id,
                    "shard_no": shard_no,
                    "rule_version": rule.version,
                    "rows": rows,
                    "row_count": row_count,
                    "sum_value": sum_value,
                }
            )
            self.store.record_shard_result(
                query_id=query_id,
                shard_no=shard_no,
                status="succeeded",
                rows=rows,
                row_count=row_count,
                sum_value=sum_value,
                result_digest=result_digest,
                error_code=None,
                error_json=None,
                clock_ms=clock_ms,
            )
            if (
                injection is not None
                and injection["action"] == "crash"
                and injection.get("position") == "after_persist"
            ):
                os._exit(137)

        return self._merge_and_commit(
            sample=sample,
            template=template,
            rule=rule,
            query_id=query_id,
            params=params,
            clock_ms=clock_ms,
        )

    def _execute_shard(
        self,
        shard_plan: dict[str, Any],
        *,
        template: Template,
    ) -> tuple[list[dict[str, Any]], int, int | None]:
        database = Path(shard_plan["database_absolute"])
        connection = sqlite3.connect(database, timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            cursor = connection.execute(
                shard_plan["sql"],
                shard_plan["params"],
            )
            rows = [dict(row) for row in cursor.fetchall()]
        finally:
            connection.close()
        sum_value: int | None = None
        if template.aggregate_sum is not None:
            total = 0
            for row in rows:
                value = row.get(template.aggregate_sum)
                if isinstance(value, bool) or not isinstance(value, int):
                    fail(
                        "E_AGGREGATE_FAILED",
                        "aggregate sum column must contain integers",
                        column=template.aggregate_sum,
                        value=value,
                    )
                total += value
            sum_value = total
        return rows, len(rows), sum_value

    def _merge_and_commit(
        self,
        *,
        sample: LoadedSample,
        template: Template,
        rule: MergedRule,
        query_id: str,
        params: dict[str, Any],
        clock_ms: int,
    ) -> dict[str, Any]:
        self._apply_phase_injection(
            stage="merge",
            template=template,
            rule=rule,
            query_id=query_id,
            params=params,
        )
        query = self.store.get_query_with_shards(query_id)
        assert query is not None
        plan = json.loads(query["plan_json"])
        shard_rows = sorted(query["shards"], key=lambda item: int(item["shard_no"]))
        if any(
            row["status"] not in {"succeeded", "failed", "abandoned"}
            for row in shard_rows
        ):
            fail(
                "E_QUERY_INCOMPLETE",
                "merge reached with unfinished shards",
                query_id=query_id,
            )

        merge_injection = self._match_injection(
            stage="merge",
            template=template,
            query_id=query_id,
            params=params,
        )
        for row in shard_rows:
            if row["status"] != "succeeded":
                continue
            rows = json.loads(row["rows_json"])
            recomputed = digest(
                {
                    "query_id": query_id,
                    "shard_no": int(row["shard_no"]),
                    "rule_version": rule.version,
                    "rows": rows,
                    "row_count": row["row_count"],
                    "sum_value": row["sum_value"],
                }
            )
            mismatch = row["result_digest"] != recomputed
            mismatch_injected = False
            if (
                merge_injection is not None
                and merge_injection["action"] == "corrupt_digest"
                and merge_injection.get("shard_no") in (None, int(row["shard_no"]))
            ):
                mismatch = True
                mismatch_injected = True
            if mismatch:
                error = BusinessError(
                    code=(
                        merge_injection["code"]
                        if merge_injection is not None
                        and merge_injection["action"] == "corrupt_digest"
                        else "E_SHARD_RESULT_DIGEST_MISMATCH"
                    ),
                    message="shard result digest does not match the execution record",
                    details={
                        "stage": "merge",
                        "template_id": template.template_id,
                        "rule_version": rule.version,
                        "query_id": query_id,
                        "shard_no": int(row["shard_no"]),
                        "persisted_digest": row["result_digest"],
                        "computed_digest": recomputed,
                        "mismatch_injected": mismatch_injected,
                        "trace": {
                            "execution_stage": "execute",
                            "shard_no": int(row["shard_no"]),
                            "shard_status": row["status"],
                        },
                    },
                )
                self.store.mark_query_error(
                    query_id=query_id,
                    stage="merge",
                    error=error,
                    clock_ms=clock_ms,
                )
                error._persisted_error = True  # type: ignore[attr-defined]
                raise error

        succeeded = [row for row in shard_rows if row["status"] == "succeeded"]
        failed = [row for row in shard_rows if row["status"] == "failed"]
        abandoned = [row for row in shard_rows if row["status"] == "abandoned"]
        failures = [
            {
                "shard_no": int(row["shard_no"]),
                "status": "failed",
                "error_code": row["error_code"],
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
            }
            for row in failed
        ]
        abandoned_items = [
            {
                "shard_no": int(row["shard_no"]),
                "status": "abandoned",
                "error_code": row["error_code"],
                "error": json.loads(row["error_json"]) if row["error_json"] else None,
            }
            for row in abandoned
        ]
        if not succeeded:
            error = BusinessError(
                code="E_EXECUTION_FAILED",
                message="all routed shards failed or were abandoned",
                details={
                    "stage": "merge",
                    "template_id": template.template_id,
                    "rule_version": rule.version,
                    "query_id": query_id,
                    "failed_shards": [item["shard_no"] for item in failures],
                    "abandoned_shards": [
                        item["shard_no"] for item in abandoned_items
                    ],
                    "failures": failures,
                    "abandoned": abandoned_items,
                },
            )
            result = {
                "query_id": query_id,
                "template_id": template.template_id,
                "rule_version": rule.version,
                "rule_digest": rule.digest,
                "plan_digest": plan["plan_digest"],
                "outcome": "failed",
                "aggregate": {"count": 0, "sum": None},
                "rows": [],
                "failures": failures,
                "abandoned": abandoned_items,
            }
            self.store.mark_failed_terminal(
                query_id=query_id,
                outcome="failed",
                aggregate=result["aggregate"],
                result=result,
                error=error,
                clock_ms=clock_ms,
            )
            error._persisted_error = True  # type: ignore[attr-defined]
            raise error

        merged_rows: list[dict[str, Any]] = []
        per_shard: list[dict[str, Any]] = []
        for row in succeeded:
            rows = json.loads(row["rows_json"])
            merged_rows.extend(rows)
            per_shard.append(
                {
                    "shard_no": int(row["shard_no"]),
                    "row_count": row["row_count"],
                    "sum": row["sum_value"],
                    "result_digest": row["result_digest"],
                }
            )
        if template.order_by:
            merged_rows.sort(
                key=lambda item: tuple(
                    (str(type(item.get(column))), item.get(column))
                    for column in template.order_by
                )
            )
        else:
            merged_rows.sort(key=canonical_json)
        count = len(merged_rows)
        sum_value = (
            sum(int(row[template.aggregate_sum]) for row in merged_rows)
            if template.aggregate_sum is not None
            else None
        )
        outcome = (
            "partial"
            if failures or abandoned_items
            else "success"
        )
        result: dict[str, Any] = {
            "ok": True,
            "query_id": query_id,
            "template_id": template.template_id,
            "logical_table": template.logical_table,
            "kind": template.kind,
            "outcome": outcome,
            "rule_version": rule.version,
            "rule_digest": rule.digest,
            "rule_source": rule.source,
            "plan_digest": plan["plan_digest"],
            "all_route": plan["all_route"],
            "target_shards": plan["target_shards"],
            "aggregate": {
                "count": count,
                "sum": sum_value,
            },
            "per_shard": per_shard,
            "rows": merged_rows,
            "shard_outcomes": [
                {
                    "shard_no": int(row["shard_no"]),
                    "status": row["status"],
                    "row_count": row["row_count"],
                    "sum": row["sum_value"],
                    "result_digest": row["result_digest"],
                    "error_code": row["error_code"],
                }
                for row in shard_rows
            ],
            "failures": failures,
            "abandoned": abandoned_items,
            "merge_order": {
                "shards": [int(row["shard_no"]) for row in succeeded],
                "row_sort": list(template.order_by),
                "integer_arithmetic": True,
            },
        }
        commit = self.store.commit_query(
            query_id=query_id,
            outcome=outcome,
            aggregate=result["aggregate"],
            result=result,
            clock_ms=clock_ms,
        )
        stored = commit.get("result", result)
        return stored

    def resume(
        self,
        *,
        query_id: str | None,
        all_queries: bool = False,
        now_clock_ms: int | None = None,
        v1_base_clock_ms: int | None = None,
    ) -> dict[str, Any]:
        sample = self._load(
            source_filter="merged",
            v1_base_clock_ms=v1_base_clock_ms,
            load_rule_snapshots=False,
        )
        if query_id is None:
            all_queries = True
        targets: list[str] = []
        if all_queries:
            targets = [row["query_id"] for row in self.store.uncommitted_queries()]
        elif query_id is not None:
            targets = [query_id]
        results: list[dict[str, Any]] = []
        for target in sorted(set(targets)):
            persisted = self.store.get_query_with_shards(target)
            if persisted is None:
                fail("E_QUERY_NOT_FOUND", "query does not exist", query_id=target)
            if persisted["status"] == "committed":
                results.append(json.loads(persisted["result_json"]))
                continue
            if persisted["status"] == "failed":
                raise _error_from_persisted(
                    json.loads(persisted["error_json"])
                    if persisted["error_json"]
                    else None
                )
            template = sample.templates.get(persisted["template_id"])
            if template is None:
                fail(
                    "E_TEMPLATE_INVALID",
                    "persisted query references an unknown template",
                    template=persisted["template_id"],
                )
            rule = merged_rule_from_dict(
                json.loads(persisted["bound_rule_model_json"])
            )
            params = json.loads(persisted["params_json"])
            clock = (
                int(now_clock_ms)
                if now_clock_ms is not None
                else int(persisted["updated_clock_ms"])
            )
            results.append(
                self._drive_query(
                    sample=sample,
                    template=template,
                    rule=rule,
                    query_id=target,
                    params=params,
                    clock_ms=clock,
                    resume=True,
                )
            )
        return {
            "ok": True,
            "command": "resume",
            "resumed_queries": sorted(set(targets)),
            "results": results,
        }

    def apply(
        self,
        *,
        rules_path: Path,
        version: str | None = None,
        now_clock_ms: int | None = None,
        v1_base_clock_ms: int | None = None,
    ) -> dict[str, Any]:
        sample = self._load(
            source_filter="merged",
            v1_base_clock_ms=v1_base_clock_ms,
            load_rule_snapshots=False,
        )
        if not self.store.path.exists():
            fail("E_STATE_NOT_LOADED", "state is not initialized; run load first")
        rules_path = rules_path.resolve()
        if rules_path.is_dir():
            candidates = load_v2_rules(rules_path, tables=sample.tables)
            models, _, _ = build_rule_models([], candidates, tables=sample.tables)
        elif rules_path.is_file():
            candidates = load_v1_rules(
                rules_path,
                tables=sample.tables,
                base_clock_override_ms=v1_base_clock_ms,
            )
            models, _, _ = build_rule_models(candidates, [], tables=sample.tables)
        else:
            fail("E_INPUT_MISSING", "rules path does not exist", path=str(rules_path))
        target = select_snapshot_rule(models, version=version)
        clock = (
            int(now_clock_ms)
            if now_clock_ms is not None
            else self.store.status()["logical_clock_ms"]
        )
        outcome = self.store.apply_rule(rule=target, now_clock_ms=clock)
        if not outcome["ok"]:
            raise BusinessError(
                code=outcome["code"],
                message=outcome["message"],
                details=outcome.get("details", {}),
            )
        status = self.store.status()
        return {
            "ok": True,
            "command": "apply",
            "result": outcome,
            "current_rule": status["current_rule"],
            "logical_clock_ms": status["logical_clock_ms"],
        }

    def status(self, *, query_id: str | None = None) -> dict[str, Any]:
        return self.store.status(query_id=query_id)

    def verify(
        self,
        *,
        now_clock_ms: int | None = None,
        v1_base_clock_ms: int | None = None,
    ) -> dict[str, Any]:
        sample = self._load(
            source_filter="merged",
            v1_base_clock_ms=v1_base_clock_ms,
        )
        clock = sample.initial_clock_ms if now_clock_ms is None else now_clock_ms
        checks: list[dict[str, Any]] = []

        def add_check(name: str, passed: bool, **details: Any) -> None:
            checks.append({"name": name, "ok": bool(passed), "details": details})

        add_check(
            "merged_rule_versions_present",
            bool(sample.merged_rules),
            versions=[rule.version for rule in sample.merged_rules],
        )
        shared_versions = sorted(
            {rule.version for rule in sample.v1_versions}
            & {rule.version for rule in sample.v2_versions}
        )
        route_comparison: list[dict[str, Any]] = []
        for template_id, template in sorted(sample.templates.items()):
            table = sample.tables[template.logical_table]
            for version in shared_versions:
                v1_rule = next(
                    rule for rule in sample.v1_versions if rule.version == version
                )
                v2_rule = next(
                    rule for rule in sample.v2_versions if rule.version == version
                )
                params_for_route = self._representative_params(
                    template,
                    table=table,
                    routing=v1_rule.routing[table.name],
                )
                try:
                    v1_plan = build_plan(
                        template=template,
                        table=table,
                        rule=v1_rule,
                        mappings=sample.mappings[table.name],
                        params=params_for_route,
                    )
                    v2_plan = build_plan(
                        template=template,
                        table=table,
                        rule=v2_rule,
                        mappings=sample.mappings[table.name],
                        params=params_for_route,
                    )
                    same = v1_plan["target_shards"] == v2_plan["target_shards"]
                    route_comparison.append(
                        {
                            "template_id": template_id,
                            "version": version,
                            "v1_shards": v1_plan["target_shards"],
                            "v2_shards": v2_plan["target_shards"],
                            "same": same,
                        }
                    )
                except BusinessError as exc:
                    route_comparison.append(
                        {
                            "template_id": template_id,
                            "version": version,
                            "same": False,
                            "error": exc.to_dict(),
                        }
                    )
        add_check(
            "v1_v2_shared_version_routes_match",
            all(item.get("same") for item in route_comparison),
            comparisons=route_comparison,
        )

        state_checks: list[dict[str, Any]] = []
        if self.store.path.exists():
            status = self.store.status()
            current_rule = status["current_rule"]
            matching_rule = next(
                (
                    rule
                    for rule in sample.merged_rules
                    if rule.version == current_rule["version"]
                ),
                None,
            )
            add_check(
                "current_pointer_matches_merged_model",
                matching_rule is not None
                and matching_rule.digest == current_rule["digest"],
                current_rule=current_rule,
            )
            with self.store.read() as connection:
                commits = connection.execute(
                    "SELECT * FROM query_commits ORDER BY query_id"
                ).fetchall()
                for commit in commits:
                    result = json.loads(commit["result_json"])
                    recomputed = digest(result)
                    query = connection.execute(
                        "SELECT * FROM query_runs WHERE query_id=?",
                        (commit["query_id"],),
                    ).fetchone()
                    shards = connection.execute(
                        "SELECT * FROM shard_runs WHERE query_id=? ORDER BY shard_no",
                        (commit["query_id"],),
                    ).fetchall()
                    query_ok = (
                        query is not None
                        and query["status"] == "committed"
                        and recomputed == commit["result_digest"]
                        and query["result_json"] == commit["result_json"]
                    )
                    aggregate_ok = True
                    aggregate_reason = ""
                    aggregate = result.get("aggregate", {})
                    rows = result.get("rows", [])
                    if aggregate.get("count") != len(rows):
                        aggregate_ok = False
                        aggregate_reason = "count mismatch"
                    template = sample.templates.get(query["template_id"])
                    if aggregate.get("sum") is not None and template is not None:
                        recomputed_sum = 0
                        for row in rows:
                            raw = row.get(template.aggregate_sum)
                            if isinstance(raw, bool) or not isinstance(raw, int):
                                aggregate_ok = False
                                aggregate_reason = "non-integer aggregate input"
                                break
                            recomputed_sum += raw
                        if aggregate_ok and aggregate["sum"] != recomputed_sum:
                            aggregate_ok = False
                            aggregate_reason = "sum mismatch"
                    state_checks.append(
                        {
                            "query_id": commit["query_id"],
                            "rule_version": commit["rule_version"],
                            "result_digest_matches": recomputed
                            == commit["result_digest"],
                            "query_commit_consistent": bool(query_ok),
                            "aggregate_consistent": aggregate_ok,
                            "aggregate_reason": aggregate_reason,
                            "shard_count": len(shards),
                            "shard_outcomes": [
                                {
                                    "shard_no": shard["shard_no"],
                                    "status": shard["status"],
                                    "result_digest": shard["result_digest"],
                                }
                                for shard in shards
                            ],
                        }
                    )
            add_check(
                "committed_results_are_recomputable",
                all(
                    item["result_digest_matches"]
                    and item["query_commit_consistent"]
                    and item["aggregate_consistent"]
                    for item in state_checks
                ),
                commits=state_checks,
            )
        else:
            add_check(
                "state_database_present",
                False,
                reason="run load before verify to validate committed state",
            )
        all_ok = all(check["ok"] for check in checks)
        report: dict[str, Any] = {
            "ok": all_ok,
            "command": "verify",
            "logical_clock_ms": clock,
            "checks": checks,
        }
        if not all_ok:
            report.update(
                {
                    "code": "E_VERIFY_FAILED",
                    "message": "one or more verification checks failed",
                    "exit_code": 5,
                }
            )
        return report

    @staticmethod
    def _representative_params(
        template: Template,
        *,
        table: LogicalTable,
        routing: Any,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if template.kind == "equal":
            if routing.rule_type == "range" and routing.boundaries:
                values[template.shard_key_param] = routing.boundaries[0]  # type: ignore[index]
            else:
                values[template.shard_key_param] = 0  # type: ignore[index]
        elif template.kind == "range":
            if routing.rule_type == "range" and routing.boundaries:
                values[template.shard_key_params[0]] = routing.boundaries[0]
                values[template.shard_key_params[1]] = routing.boundaries[-1] - 1
            else:
                values[template.shard_key_params[0]] = 0
                values[template.shard_key_params[1]] = max(
                    0,
                    table.shard_count - 1,
                )
        elif template.kind == "in":
            for index, name in enumerate(template.shard_key_params):
                if routing.rule_type == "range" and routing.boundaries:
                    values[name] = routing.boundaries[
                        index % routing.shard_count
                    ]
                else:
                    values[name] = index % routing.shard_count
        for name in template.parameters:
            values.setdefault(name, 0)
        return values
