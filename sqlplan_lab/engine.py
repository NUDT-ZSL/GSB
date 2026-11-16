"""SQLite-backed experiment engine."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .errors import (
    E_DATA,
    E_PARSE,
    E_REPLAY,
    E_SKEW,
    E_SNAPSHOT,
    E_STATS,
    E_VERSION,
    LabError,
)
from .parser import Query, parse_sql
from .planner import (
    build_plan as build_logical_plan,
    node_map,
    query_count_sql,
    scan_count_sql,
)
from .stats import (
    build_stats_payload,
    estimate_plan,
    normalize_parameters,
    stats_checksum,
    validate_stats_payload,
)
from .util import (
    canonical_json,
    deviation_score,
    ensure_parent,
    heat_band,
    json_digest,
    ratio_ppm,
    read_text,
    round_half_up,
    split_sql_script,
    sql_quote_identifier,
    text_digest,
    typed_row,
)


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS _lab_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _lab_data_versions (
    version TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    sql_sha256 TEXT NOT NULL,
    tables_json TEXT NOT NULL,
    markers_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _lab_tables (
    table_name TEXT PRIMARY KEY,
    schema_json TEXT NOT NULL,
    markers_json TEXT NOT NULL,
    data_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _lab_stats (
    version TEXT PRIMARY KEY,
    data_version TEXT NOT NULL,
    params_json TEXT NOT NULL,
    stats_json TEXT NOT NULL,
    checksum TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _lab_plans (
    plan_id TEXT PRIMARY KEY,
    normalized_sql TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    signature TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _lab_snapshots (
    name TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    checksum TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _lab_session_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _lab_heatmap (
    ordinal INTEGER PRIMARY KEY,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS _lab_logs (
    event_id TEXT PRIMARY KEY,
    event_key TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


PRIMARY_MARKER_RE = re.compile(
    r"^\s*--\s*@primary-key\s+([a-z_][a-z0-9_$]*)\.([a-z_][a-z0-9_$]*)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
SKEW_MARKER_RE = re.compile(
    r"^\s*--\s*@skew\s+([a-z_][a-z0-9_$]*)\.([a-z_][a-z0-9_$]*)=(.+):([0-9]*\.?[0-9]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def _default_session(data_version: str | None = None) -> dict[str, Any]:
    return {
        "session_id": "session-main",
        "data_version": data_version,
        "stats_version": None,
        "plan_history": [],
        "last_analysis": None,
    }


class Engine:
    """A deterministic local experiment engine backed by one SQLite file."""

    def __init__(self, db_path: str | Path):
        self.db_path = ensure_parent(db_path)
        self.conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.conn.executescript(SCHEMA_SQL)
        self._state = self._load_session()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Engine":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Metadata and session state
    # ------------------------------------------------------------------
    def _meta_get(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM _lab_meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def _meta_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO _lab_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def current_data_version(self) -> str | None:
        return self._meta_get("current_data_version")

    def _load_session(self) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT payload_json FROM _lab_session_state WHERE id = 1"
        ).fetchone()
        if row is None:
            return _default_session(self.current_data_version)
        try:
            state = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise LabError(E_SNAPSHOT, "current session state is corrupted", {"error": str(exc)})
        if not isinstance(state, dict):
            raise LabError(E_SNAPSHOT, "current session state is not an object")
        state.setdefault("session_id", "session-main")
        state.setdefault("data_version", self.current_data_version)
        state.setdefault("stats_version", None)
        state.setdefault("plan_history", [])
        state.setdefault("last_analysis", None)
        return state

    def _save_session(self, state: dict[str, Any] | None = None) -> None:
        if state is not None:
            self._state = state
        payload = canonical_json(self._state)
        self.conn.execute(
            "INSERT INTO _lab_session_state(id, payload_json) VALUES(1, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload_json = excluded.payload_json",
            (payload,),
        )

    def session_state(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    # ------------------------------------------------------------------
    # User-table discovery and ingestion
    # ------------------------------------------------------------------
    def _user_table_names(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_lab_%' ORDER BY name"
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def _parse_markers(self, script: str) -> list[dict[str, Any]]:
        markers: list[dict[str, Any]] = []
        for match in PRIMARY_MARKER_RE.finditer(script):
            markers.append(
                {
                    "kind": "primary_key",
                    "table": match.group(1).lower(),
                    "column": match.group(2).lower(),
                }
            )
        for match in SKEW_MARKER_RE.finditer(script):
            raw_value = match.group(3).strip()
            if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in "'\"":
                value: Any = raw_value[1:-1]
            else:
                try:
                    value = int(raw_value)
                except ValueError:
                    try:
                        value = float(raw_value)
                    except ValueError:
                        value = raw_value
            share = float(match.group(4))
            if share < 0.0 or share > 1.0:
                raise LabError(E_DATA, "skew marker expected share must be in [0, 1]")
            markers.append(
                {
                    "kind": "skew",
                    "table": match.group(1).lower(),
                    "column": match.group(2).lower(),
                    "value": value,
                    "expected_share": share,
                }
            )
        markers.sort(key=lambda item: canonical_json(item))
        return markers

    def _validate_statement(self, statement: str, index: int) -> None:
        stripped = re.sub(
            r"^(?:\s|--[^\n]*(?:\n|$)|/\*.*?\*/)*", "", statement, flags=re.DOTALL
        ).strip()
        upper = stripped.upper()
        allowed = (
            "CREATE TABLE ",
            "CREATE INDEX ",
            "CREATE UNIQUE INDEX ",
            "INSERT INTO ",
            "REPLACE INTO ",
            "DROP TABLE ",
            "DROP INDEX ",
            "DELETE FROM ",
            "UPDATE ",
        )
        if not any(upper.startswith(prefix) for prefix in allowed):
            raise LabError(
                E_DATA,
                "SQL statement is not allowed in deterministic ingestion",
                {"statement_index": index, "leading": stripped[:80]},
            )
        if not stripped:
            raise LabError(E_DATA, "empty SQL statement", {"statement_index": index})

    def _table_metadata(
        self, table: str, markers: list[dict[str, Any]], data_version: str
    ) -> dict[str, Any]:
        cursor = self.conn.execute(f"PRAGMA table_info({sql_quote_identifier(table)})")
        raw_columns = cursor.fetchall()
        columns = [
            {
                "name": str(row["name"]).lower(),
                "type": str(row["type"] or ""),
                "not_null": bool(row["notnull"]),
                "default": row["dflt_value"],
                "primary_key_position": int(row["pk"] or 0),
            }
            for row in raw_columns
        ]
        primary_key = [
            column["name"]
            for column in sorted(columns, key=lambda item: item["primary_key_position"])
            if column["primary_key_position"] > 0
        ]
        table_markers = [marker for marker in markers if marker.get("table") == table]
        return {
            "table": table,
            "schema": {"columns": columns},
            "primary_key": primary_key,
            "markers": sorted(table_markers, key=lambda item: canonical_json(item)),
            "data_version": data_version,
        }

    def _content_digest(self, tables_meta: list[dict[str, Any]], markers: list[dict[str, Any]]) -> str:
        table_payloads = []
        for table_meta in sorted(tables_meta, key=lambda item: item["table"]):
            table = table_meta["table"]
            row_texts = []
            for row in self.conn.execute(f"SELECT * FROM {sql_quote_identifier(table)}"):
                row_texts.append(canonical_json(typed_row(tuple(row))))
            row_texts.sort()
            table_payloads.append(
                {
                    "table": table,
                    "schema": table_meta["schema"],
                    "primary_key": table_meta["primary_key"],
                    "rows": row_texts,
                }
            )
        content = {
            "format": 1,
            "tables": table_payloads,
            "markers": sorted(markers, key=lambda item: canonical_json(item)),
        }
        return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()

    def ingest_sql_text(self, script: str) -> dict[str, Any]:
        if not isinstance(script, str) or not script.strip():
            raise LabError(E_DATA, "SQL sample is empty")
        if "_lab_" in script.lower():
            raise LabError(E_DATA, "sample SQL may not access reserved _lab_ tables")
        markers = self._parse_markers(script)
        statements = split_sql_script(script)
        if not statements:
            raise LabError(E_DATA, "SQL sample has no executable statements")
        for index, statement in enumerate(statements):
            self._validate_statement(statement, index)
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            for table in self._user_table_names():
                self.conn.execute(f"DROP TABLE IF EXISTS {sql_quote_identifier(table)}")
            for index, statement in enumerate(statements):
                try:
                    self.conn.execute(statement)
                except sqlite3.Error as exc:
                    raise LabError(
                        E_DATA,
                        "SQL execution failed during ingestion",
                        {"statement_index": index, "sqlite_error": str(exc)},
                    ) from exc
            tables = self._user_table_names()
            if not tables:
                raise LabError(E_DATA, "sample SQL did not create any user table")
            if any(table.startswith("_lab_") for table in tables):
                raise LabError(E_DATA, "reserved table name in sample")
            digest = self._content_digest(
                [self._table_metadata(table, markers, "pending") for table in tables], markers
            )
            data_version = "dv_" + digest[:24]
            tables_meta = [
                self._table_metadata(table, markers, data_version) for table in tables
            ]
            sql_sha256 = text_digest(script)
            self.conn.execute(
                "INSERT INTO _lab_data_versions(version, digest, sql_sha256, tables_json, markers_json) "
                "VALUES(?, ?, ?, ?, ?) ON CONFLICT(version) DO UPDATE SET "
                "digest=excluded.digest, sql_sha256=excluded.sql_sha256, "
                "tables_json=excluded.tables_json, markers_json=excluded.markers_json",
                (
                    data_version,
                    digest,
                    sql_sha256,
                    canonical_json(tables_meta),
                    canonical_json(markers),
                ),
            )
            self.conn.execute("DELETE FROM _lab_tables")
            for table_meta in tables_meta:
                self.conn.execute(
                    "INSERT INTO _lab_tables(table_name, schema_json, markers_json, data_version) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        table_meta["table"],
                        canonical_json(table_meta["schema"]),
                        canonical_json(table_meta["markers"]),
                        data_version,
                    ),
                )
            self._meta_set("current_data_version", data_version)
            self._state = _default_session(data_version)
            self._save_session()
            self.conn.execute("DELETE FROM _lab_heatmap")
            self.conn.commit()
            return {
                "ok": True,
                "data_version": data_version,
                "digest": digest,
                "sql_sha256": sql_sha256,
                "tables": [entry["table"] for entry in tables_meta],
                "markers": markers,
            }
        except LabError:
            self.conn.rollback()
            self._state = self._load_session()
            raise
        except Exception as exc:
            self.conn.rollback()
            self._state = self._load_session()
            raise LabError(E_DATA, "ingestion failed and was rolled back", {"error": str(exc)}) from exc

    def ingest_sql_file(self, path: str | Path) -> dict[str, Any]:
        return self.ingest_sql_text(read_text(path))

    def list_tables(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT table_name, schema_json, markers_json, data_version "
            "FROM _lab_tables ORDER BY table_name"
        ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "table": row["table_name"],
                    "schema": json.loads(row["schema_json"]),
                    "markers": json.loads(row["markers_json"]),
                    "data_version": row["data_version"],
                }
            )
        return result

    def _schemas(self) -> dict[str, dict[str, Any]]:
        return {entry["table"]: entry["schema"] for entry in self.list_tables()}

    # ------------------------------------------------------------------
    # Statistics versions
    # ------------------------------------------------------------------
    def create_stats_version(
        self,
        histogram_buckets: int = 8,
        sample_rate: float = 1.0,
        null_fraction_overrides: dict[str, float] | None = None,
    ) -> dict[str, Any]:
        params = normalize_parameters(
            histogram_buckets, sample_rate, null_fraction_overrides
        )
        tables = self.list_tables()
        if not tables:
            raise LabError(E_STATS, "no tables are available for statistics")
        table_columns = {
            entry["table"]: {column["name"] for column in entry["schema"]["columns"]}
            for entry in tables
        }
        for key in params["null_fraction_overrides"]:
            table, column = key.split(".", 1)
            if table not in table_columns or column not in table_columns[table]:
                raise LabError(
                    E_STATS,
                    "null override references an unknown column",
                    {"column": key},
                )
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            data_version = self.current_data_version
            if not data_version:
                raise LabError(E_STATS, "no current data version")
            current_tables = self.list_tables()
            if any(entry["data_version"] != data_version for entry in current_tables):
                raise LabError(E_VERSION, "table metadata and current data version disagree")
            payload = build_stats_payload(self.conn, data_version, current_tables, params)
            checksum = stats_checksum(payload)
            stats_version = payload["stats_version"]
            existing = self.conn.execute(
                "SELECT checksum FROM _lab_stats WHERE version = ?", (stats_version,)
            ).fetchone()
            if existing is not None and existing["checksum"] != checksum:
                raise LabError(E_STATS, "statistics version checksum collision")
            self.conn.execute(
                "INSERT OR IGNORE INTO _lab_stats(version, data_version, params_json, stats_json, checksum) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    stats_version,
                    payload["data_version"],
                    canonical_json(payload["parameters"]),
                    canonical_json(payload),
                    checksum,
                ),
            )
            self._state["data_version"] = data_version
            self._state["stats_version"] = stats_version
            self._state["last_analysis"] = None
            self._save_session()
            self.conn.execute("DELETE FROM _lab_heatmap")
            self.conn.commit()
            return {
                "ok": True,
                "stats_version": stats_version,
                "data_version": data_version,
                "parameters": payload["parameters"],
                "checksum": checksum,
            }
        except Exception:
            self.conn.rollback()
            self._state = self._load_session()
            raise

    def _load_stats(self, stats_version: str | None = None) -> dict[str, Any]:
        version = stats_version or self._state.get("stats_version")
        if not version:
            raise LabError(E_STATS, "no statistics version is selected")
        row = self.conn.execute(
            "SELECT stats_json, checksum, data_version FROM _lab_stats WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            raise LabError(E_STATS, "statistics version is missing", {"stats_version": version})
        try:
            payload = json.loads(row["stats_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise LabError(
                E_STATS,
                "statistics version is corrupted",
                {"stats_version": version, "reason": "invalid_json"},
            ) from exc
        validate_stats_payload(payload)
        actual_checksum = stats_checksum(payload)
        if actual_checksum != row["checksum"]:
            raise LabError(
                E_STATS,
                "statistics version checksum mismatch",
                {"stats_version": version, "reason": "checksum_mismatch"},
            )
        if payload.get("stats_version") != version:
            raise LabError(
                E_STATS,
                "statistics version payload mismatch",
                {"stats_version": version},
            )
        if payload.get("data_version") != row["data_version"]:
            raise LabError(
                E_STATS,
                "statistics version row mismatch",
                {"stats_version": version},
            )
        return payload

    def stats_version_info(self, stats_version: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT version, data_version, params_json, checksum FROM _lab_stats WHERE version = ?",
            (stats_version,),
        ).fetchone()
        if row is None:
            raise LabError(E_STATS, "statistics version is missing", {"stats_version": stats_version})
        return {
            "stats_version": row["version"],
            "data_version": row["data_version"],
            "parameters": json.loads(row["params_json"]),
            "checksum": row["checksum"],
        }

    def list_stats_versions(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT version, data_version, params_json FROM _lab_stats ORDER BY version"
        ).fetchall()
        return [
            {
                "stats_version": row["version"],
                "data_version": row["data_version"],
                "parameters": json.loads(row["params_json"]),
            }
            for row in rows
        ]

    def _require_same_data_version(self, stats: dict[str, Any]) -> str:
        current = self.current_data_version
        if current is None:
            raise LabError(E_VERSION, "no current data version")
        if stats["data_version"] != current:
            raise LabError(
                E_VERSION,
                "statistics and current data use different versions",
                {"stats_data_version": stats["data_version"], "current_data_version": current},
            )
        return current

    # ------------------------------------------------------------------
    # Plans and estimation
    # ------------------------------------------------------------------
    def build_plan(self, query_text: str) -> dict[str, Any]:
        query = parse_sql(query_text)
        schemas = self._schemas()
        if not schemas:
            raise LabError(E_PARSE, "no tables are available")
        return build_logical_plan(query, schemas)

    def _persist_plan(self, plan: dict[str, Any]) -> None:
        serialized = canonical_json(plan)
        row = self.conn.execute(
            "SELECT plan_json FROM _lab_plans WHERE plan_id = ?", (plan["plan_id"],)
        ).fetchone()
        if row is not None:
            if row["plan_json"] != serialized:
                raise LabError(
                    E_REPLAY,
                    "plan id collision during replay persistence",
                    {"plan_id": plan["plan_id"]},
                )
            return
        self.conn.execute(
            "INSERT INTO _lab_plans(plan_id, normalized_sql, plan_json, signature) "
            "VALUES(?, ?, ?, ?)",
            (
                plan["plan_id"],
                plan["normalized_sql"],
                serialized,
                json_digest({key: value for key, value in plan.items() if key != "tree"}),
            ),
        )

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT plan_json FROM _lab_plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        if row is None:
            raise LabError(E_REPLAY, "logical plan is not registered", {"plan_id": plan_id})
        return json.loads(row["plan_json"])

    def estimate(
        self,
        query_text: str,
        stats_version: str | None = None,
    ) -> dict[str, Any]:
        plan = self.build_plan(query_text)
        stats = self._load_stats(stats_version)
        self._require_same_data_version(stats)
        result = estimate_plan(plan, stats)
        return {"plan": plan, "estimate": result}

    # ------------------------------------------------------------------
    # Actual row counts and deviation attribution
    # ------------------------------------------------------------------
    def _collect_actual_rows(
        self, query: Query, plan: dict[str, Any]
    ) -> tuple[dict[str, int], int]:
        nodes = node_map(plan)
        actual: dict[str, int] = {}
        try:
            for op_id in plan["topological_order"]:
                node = nodes[op_id]
                if node["kind"] == "scan":
                    sql = scan_count_sql(node["table"])
                elif node["kind"] == "join":
                    sql = query_count_sql(query, include_join=True, include_where=False)
                else:
                    sql = query_count_sql(query, include_join=True, include_where=True)
                row = self.conn.execute(sql).fetchone()
                actual[op_id] = int(row[0])
            final_row = self.conn.execute(
                query_count_sql(query, include_join=True, include_where=True)
            ).fetchone()
        except sqlite3.Error as exc:
            raise LabError(E_DATA, "failed to collect actual rows", {"sqlite_error": str(exc)}) from exc
        return actual, int(final_row[0])

    def _combine_analysis(
        self,
        plan: dict[str, Any],
        query: Query,
        stats: dict[str, Any],
        actual_rows: dict[str, int],
        final_actual_rows: int,
    ) -> dict[str, Any]:
        estimate_result = estimate_plan(plan, stats)
        nodes = node_map(plan)
        estimate_by_id = {item["op_id"]: item for item in estimate_result["operators"]}
        operators: list[dict[str, Any]] = []
        for op_id in plan["topological_order"]:
            node = nodes[op_id]
            estimated = int(estimate_by_id[op_id]["estimated_rows"])
            actual = int(actual_rows[op_id])
            score = deviation_score(estimated, actual)
            record = {
                "op_id": op_id,
                "kind": node["kind"],
                "label": self._operator_label(node),
                "table": node.get("table"),
                "estimated_rows": estimated,
                "actual_rows": actual,
                "estimated_to_actual_ratio": round(estimated / actual, 6)
                if actual > 0
                else "ZERO_ACTUAL",
                "ratio_ppm": ratio_ppm(estimated, actual),
                "zero_actual_sentinel": "ZERO_ACTUAL" if actual == 0 else None,
                "deviation_score": score,
                "heat_band": heat_band(score),
                "statistics": estimate_by_id[op_id],
            }
            operators.append(record)
        primary = None
        for record in operators:
            if primary is None or record["deviation_score"] > primary["deviation_score"]:
                primary = record
        attribution_chain = [
            {
                "chain_index": index,
                "op_id": record["op_id"],
                "kind": record["kind"],
                "label": record["label"],
                "estimated_rows": record["estimated_rows"],
                "actual_rows": record["actual_rows"],
                "deviation_score": record["deviation_score"],
                "primary": record["op_id"] == primary["op_id"] if primary else False,
            }
            for index, record in enumerate(operators)
        ]
        return {
            "plan_id": plan["plan_id"],
            "normalized_sql": plan["normalized_sql"],
            "stats_version": stats["stats_version"],
            "data_version": stats["data_version"],
            "root_estimated_rows": estimate_result["root_estimated_rows"],
            "root_actual_rows": final_actual_rows,
            "operators": operators,
            "attribution_chain": attribution_chain,
            "attribution_direction": "leaf_to_root",
            "primary_operator_id": primary["op_id"] if primary else None,
            "heatmap": {
                "plan_id": plan["plan_id"],
                "stats_version": stats["stats_version"],
                "data_version": stats["data_version"],
                "replacement": "whole_set",
                "rows": operators,
            },
        }

    def _operator_label(self, node: dict[str, Any]) -> str:
        if node["kind"] == "scan":
            return f"SCAN {node['table']} AS {node['alias']}"
        if node["kind"] == "join":
            return f"JOIN {node.get('predicate_text', '')}"
        return f"FILTER {node.get('predicate_text', '')}"

    def _replace_heatmap(self, analysis: dict[str, Any]) -> None:
        self.conn.execute("DELETE FROM _lab_heatmap")
        for index, row in enumerate(analysis["heatmap"]["rows"]):
            self.conn.execute(
                "INSERT INTO _lab_heatmap(ordinal, payload_json) VALUES(?, ?)",
                (index, canonical_json(row)),
            )

    def load_heatmap(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT payload_json FROM _lab_heatmap ORDER BY ordinal"
        ).fetchall()
        return [json.loads(row["payload_json"]) for row in rows]

    def analyze(
        self,
        query_text: str,
        stats_version: str | None = None,
        record: bool = True,
    ) -> dict[str, Any]:
        query = parse_sql(query_text)
        try:
            if record:
                self.conn.execute("BEGIN IMMEDIATE")
            else:
                self.conn.execute("BEGIN")
            stats = self._load_stats(stats_version)
            self._require_same_data_version(stats)
            plan = build_logical_plan(query, self._schemas())
            actual_rows, final_actual = self._collect_actual_rows(query, plan)
            analysis = self._combine_analysis(plan, query, stats, actual_rows, final_actual)
            if record:
                self._persist_plan(plan)
                ordinal = len(self._state.get("plan_history", [])) + 1
                entry = {
                    "ordinal": ordinal,
                    "normalized_sql": plan["normalized_sql"],
                    "plan_id": plan["plan_id"],
                    "stats_version": stats["stats_version"],
                    "data_version": stats["data_version"],
                    "result": analysis,
                }
                self._state.setdefault("plan_history", []).append(entry)
                self._state["data_version"] = stats["data_version"]
                self._state["stats_version"] = stats["stats_version"]
                self._state["last_analysis"] = analysis
                self._save_session()
                self._replace_heatmap(analysis)
                self.conn.commit()
            else:
                self.conn.rollback()
            return analysis
        except Exception:
            self.conn.rollback()
            self._state = self._load_session()
            raise

    def compare_stats_versions(
        self,
        query_text: str,
        old_stats_version: str,
        new_stats_version: str,
    ) -> dict[str, Any]:
        old_stats = self._load_stats(old_stats_version)
        new_stats = self._load_stats(new_stats_version)
        current = self._require_same_data_version(old_stats)
        if new_stats["data_version"] != current:
            raise LabError(
                E_VERSION,
                "new statistics version does not belong to current data version",
                {"new_stats_version": new_stats_version},
            )
        query = parse_sql(query_text)
        try:
            self.conn.execute("BEGIN")
            plan = build_logical_plan(query, self._schemas())
            old_estimate = estimate_plan(plan, old_stats)
            new_estimate = estimate_plan(plan, new_stats)
            actual_rows, _ = self._collect_actual_rows(query, plan)
        finally:
            self.conn.rollback()
        old_by_id = {item["op_id"]: item for item in old_estimate["operators"]}
        new_by_id = {item["op_id"]: item for item in new_estimate["operators"]}
        nodes = node_map(plan)
        changed = []
        for op_id in plan["topological_order"]:
            old_est = int(old_by_id[op_id]["estimated_rows"])
            new_est = int(new_by_id[op_id]["estimated_rows"])
            actual = int(actual_rows[op_id])
            old_score = deviation_score(old_est, actual)
            new_score = deviation_score(new_est, actual)
            if old_est == new_est and old_score == new_score:
                continue
            node = nodes[op_id]
            changed.append(
                {
                    "op_id": op_id,
                    "kind": node["kind"],
                    "label": self._operator_label(node),
                    "actual_rows": actual,
                    "old_estimated_rows": old_est,
                    "new_estimated_rows": new_est,
                    "estimated_delta": new_est - old_est,
                    "old_deviation_score": old_score,
                    "new_deviation_score": new_score,
                    "deviation_delta": new_score - old_score,
                }
            )
        return {
            "plan_id": plan["plan_id"],
            "data_version": current,
            "old_stats_version": old_stats_version,
            "new_stats_version": new_stats_version,
            "versions_differ": old_stats_version != new_stats_version,
            "changed_operator_count": len(changed),
            "changed_operators": changed,
        }

    def validate_plan_replay(
        self, plan: dict[str, Any], simulate_mismatch: bool = False
    ) -> dict[str, Any]:
        candidate = copy.deepcopy(plan)
        if simulate_mismatch:
            candidate["plan_id"] = str(candidate.get("plan_id", "")) + "_injected_mismatch"
        try:
            rebuilt = build_logical_plan(parse_sql(candidate["normalized_sql"]), self._schemas())
        except LabError as exc:
            raise LabError(
                E_REPLAY,
                "stored plan could not be replayed",
                {"reason": exc.code},
            ) from exc
        expected = candidate.get("plan_id")
        if rebuilt["plan_id"] != expected:
            raise LabError(
                E_REPLAY,
                "replayed plan identity differs from stored identity",
                {
                    "stored_plan_id": expected,
                    "replayed_plan_id": rebuilt["plan_id"],
                    "simulated": simulate_mismatch,
                },
            )
        if rebuilt["normalized_sql"] != candidate.get("normalized_sql"):
            raise LabError(
                E_REPLAY,
                "replayed normalized SQL differs from stored SQL",
                {"plan_id": expected},
            )
        return {
            "ok": True,
            "plan_id": expected,
            "normalized_sql": rebuilt["normalized_sql"],
        }

    # ------------------------------------------------------------------
    # Snapshots and restoration
    # ------------------------------------------------------------------
    def _snapshot_payload(self, name: str) -> dict[str, Any]:
        session = copy.deepcopy(self._state)
        stats_versions: set[str] = set()
        if session.get("stats_version"):
            stats_versions.add(session["stats_version"])
        for entry in session.get("plan_history", []):
            stats_versions.add(entry["stats_version"])
        for version in sorted(stats_versions):
            try:
                stats = self._load_stats(version)
            except LabError as exc:
                raise LabError(
                    E_SNAPSHOT,
                    "cannot snapshot a session with a missing statistics version",
                    {"stats_version": version, "cause": exc.code},
                ) from exc
            if stats["data_version"] != session.get("data_version"):
                raise LabError(
                    E_SNAPSHOT,
                    "session history mixes data versions",
                    {"stats_version": version},
                )
        return {
            "format": 1,
            "name": name,
            "session": session,
            "tables": self.list_tables(),
        }

    def save_snapshot(self, name: str) -> dict[str, Any]:
        if not name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise LabError(E_SNAPSHOT, "snapshot name must use letters, digits, dot, dash or underscore")
        payload = self._snapshot_payload(name)
        serialized = canonical_json(payload)
        checksum = json_digest(payload)
        self.conn.execute(
            "INSERT INTO _lab_snapshots(name, payload_json, checksum) VALUES(?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET payload_json=excluded.payload_json, checksum=excluded.checksum",
            (name, serialized, checksum),
        )
        return {
            "ok": True,
            "name": name,
            "checksum": checksum,
            "data_version": payload["session"].get("data_version"),
            "stats_version": payload["session"].get("stats_version"),
            "plan_count": len(payload["session"].get("plan_history", [])),
        }

    def list_snapshots(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT name, payload_json, checksum FROM _lab_snapshots ORDER BY name"
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            session = payload.get("session", {})
            result.append(
                {
                    "name": row["name"],
                    "checksum": row["checksum"],
                    "data_version": session.get("data_version"),
                    "stats_version": session.get("stats_version"),
                    "plan_count": len(session.get("plan_history", [])),
                }
            )
        return result

    def _history_verification(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        schemas = self._schemas()
        reports: list[dict[str, Any]] = []
        for entry in session.get("plan_history", []):
            try:
                stats = self._load_stats(entry["stats_version"])
            except LabError as exc:
                raise LabError(
                    E_SNAPSHOT,
                    "snapshot history references missing statistics",
                    {"ordinal": entry.get("ordinal"), "cause": exc.code},
                ) from exc
            if stats["data_version"] != session.get("data_version"):
                raise LabError(
                    E_SNAPSHOT,
                    "snapshot history statistics use a different data version",
                    {"ordinal": entry.get("ordinal")},
                )
            query = parse_sql(entry["normalized_sql"])
            rebuilt = build_logical_plan(query, schemas)
            if rebuilt["plan_id"] != entry["plan_id"]:
                raise LabError(
                    E_SNAPSHOT,
                    "snapshot plan cannot be replayed consistently",
                    {"ordinal": entry.get("ordinal")},
                )
            actual_rows, final_actual = self._collect_actual_rows(query, rebuilt)
            recomputed = self._combine_analysis(rebuilt, query, stats, actual_rows, final_actual)
            old_operators = {
                item["op_id"]: item for item in entry["result"].get("operators", [])
            }
            mismatches = []
            for item in recomputed["operators"]:
                old = old_operators.get(item["op_id"])
                if old is None or old.get("deviation_score") != item["deviation_score"]:
                    mismatches.append(item["op_id"])
            if mismatches:
                raise LabError(
                    E_SNAPSHOT,
                    "restored history does not reproduce stored deviation scores",
                    {"ordinal": entry.get("ordinal"), "operators": sorted(mismatches)},
                )
            reports.append(
                {
                    "ordinal": entry.get("ordinal"),
                    "plan_id": rebuilt["plan_id"],
                    "stats_version": stats["stats_version"],
                    "operators_verified": len(recomputed["operators"]),
                    "same": True,
                }
            )
        return reports

    def restore_snapshot(self, name: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT payload_json, checksum FROM _lab_snapshots WHERE name = ?", (name,)
        ).fetchone()
        if row is None:
            raise LabError(E_SNAPSHOT, "snapshot does not exist", {"name": name})
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise LabError(E_SNAPSHOT, "snapshot payload is corrupted", {"name": name}) from exc
        if json_digest(payload) != row["checksum"]:
            raise LabError(E_SNAPSHOT, "snapshot checksum mismatch", {"name": name})
        if payload.get("format") != 1 or not isinstance(payload.get("session"), dict):
            raise LabError(E_SNAPSHOT, "unsupported snapshot format", {"name": name})
        session = payload["session"]
        if session.get("data_version") != self.current_data_version:
            raise LabError(
                E_SNAPSHOT,
                "snapshot data version is not current",
                {"snapshot_data_version": session.get("data_version"), "current": self.current_data_version},
            )
        if payload.get("tables") != self.list_tables():
            raise LabError(E_SNAPSHOT, "snapshot table metadata differs from current database")
        session_stats_version = session.get("stats_version")
        if session_stats_version:
            try:
                session_stats = self._load_stats(session_stats_version)
            except LabError as exc:
                raise LabError(
                    E_SNAPSHOT,
                    "snapshot current statistics version is missing or corrupted",
                    {"stats_version": session_stats_version, "cause": exc.code},
                ) from exc
            if session_stats["data_version"] != session.get("data_version"):
                raise LabError(
                    E_SNAPSHOT,
                    "snapshot statistics and session data versions disagree",
                    {"stats_version": session_stats_version},
                )
        try:
            verification = self._history_verification(session)
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("DELETE FROM _lab_session_state")
            self.conn.execute(
                "INSERT INTO _lab_session_state(id, payload_json) VALUES(1, ?)",
                (canonical_json(session),),
            )
            self.conn.execute("DELETE FROM _lab_heatmap")
            last = session.get("last_analysis")
            if isinstance(last, dict) and isinstance(last.get("heatmap"), dict):
                for index, heat_row in enumerate(last["heatmap"].get("rows", [])):
                    self.conn.execute(
                        "INSERT INTO _lab_heatmap(ordinal, payload_json) VALUES(?, ?)",
                        (index, canonical_json(heat_row)),
                    )
            self.conn.commit()
            self._state = copy.deepcopy(session)
        except LabError:
            self.conn.rollback()
            raise
        except Exception as exc:
            self.conn.rollback()
            raise LabError(E_SNAPSHOT, "snapshot restore failed", {"error": str(exc)}) from exc
        return {
            "ok": True,
            "name": name,
            "replaced": True,
            "data_version": session.get("data_version"),
            "stats_version": session.get("stats_version"),
            "plan_count": len(session.get("plan_history", [])),
            "history_verification": verification,
        }

    def verify_restored_history(self) -> list[dict[str, Any]]:
        try:
            return self._history_verification(self._state)
        except LabError as exc:
            if exc.code == E_SNAPSHOT:
                raise
            raise LabError(E_SNAPSHOT, "history verification failed", {"cause": exc.code}) from exc

    # ------------------------------------------------------------------
    # Controlled anomaly injection
    # ------------------------------------------------------------------
    def _skew_markers(self) -> list[dict[str, Any]]:
        markers = []
        for table in self.list_tables():
            for marker in table.get("markers", []):
                if marker.get("kind") == "skew":
                    markers.append(marker)
        return sorted(markers, key=lambda item: canonical_json(item))

    def inject_skew_anomaly(self, threshold: float = 0.95) -> None:
        markers = self._skew_markers()
        if not markers:
            raise LabError(E_SKEW, "no skew marker is available", {"reason": "no_marker"})
        marker = markers[0]
        table = marker["table"]
        column = marker["column"]
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                f"SELECT {sql_quote_identifier(column)} AS value, COUNT(*) AS n "
                f"FROM {sql_quote_identifier(table)} GROUP BY {sql_quote_identifier(column)} "
                "ORDER BY n DESC, value ASC LIMIT 1"
            ).fetchone()
            if row is None:
                raise LabError(E_SKEW, "skew anomaly could not find rows", {"table": table})
            dominant = row["value"]
            self.conn.execute(
                f"UPDATE {sql_quote_identifier(table)} SET {sql_quote_identifier(column)} = ?",
                (dominant,),
            )
            total_row = self.conn.execute(
                f"SELECT COUNT(*) FROM {sql_quote_identifier(table)}"
            ).fetchone()
            total = int(total_row[0])
            share_row = self.conn.execute(
                f"SELECT COUNT(*) FROM {sql_quote_identifier(table)} "
                f"WHERE {sql_quote_identifier(column)} = ?",
                (dominant,),
            ).fetchone()
            dominant_count = int(share_row[0])
            share_ppm = round_half_up(dominant_count * 1_000_000 / max(1, total))
            if share_ppm < round_half_up(threshold * 1_000_000):
                raise LabError(
                    E_SKEW,
                    "skew amplification did not exceed threshold",
                    {"share_ppm": share_ppm, "threshold_ppm": round_half_up(threshold * 1_000_000)},
                )
            raise LabError(
                E_SKEW,
                "data skew amplification exceeded the safe threshold",
                {
                    "anomaly": "skew",
                    "table": table,
                    "column": column,
                    "row_count": total,
                    "dominant_count": dominant_count,
                    "dominant_share_ppm": share_ppm,
                    "threshold_ppm": round_half_up(threshold * 1_000_000),
                },
            )
        except LabError:
            self.conn.rollback()
            raise
        except Exception as exc:
            self.conn.rollback()
            raise LabError(E_SKEW, "skew anomaly injection failed", {"error": str(exc)}) from exc

    def inject_stats_anomaly(self, stats_version: str | None = None) -> None:
        version = stats_version or self._state.get("stats_version")
        if not version:
            raise LabError(E_STATS, "no statistics version is selected")
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            row = self.conn.execute(
                "SELECT stats_json, data_version FROM _lab_stats WHERE version = ?", (version,)
            ).fetchone()
            if row is None:
                raise LabError(E_STATS, "statistics version is missing", {"stats_version": version})
            payload = json.loads(row["stats_json"])
            corrupt = {
                "format": payload.get("format", 1),
                "data_version": payload.get("data_version"),
                "stats_version": payload.get("stats_version", version),
                "parameters": payload.get("parameters", {}),
            }
            self.conn.execute(
                "UPDATE _lab_stats SET stats_json = ? WHERE version = ?",
                (canonical_json(corrupt), version),
            )
            self._load_stats(version)
            raise LabError(
                E_STATS,
                "statistics corruption was not detected",
                {"stats_version": version},
            )
        except LabError:
            self.conn.rollback()
            raise
        except Exception as exc:
            self.conn.rollback()
            raise LabError(E_STATS, "statistics anomaly injection failed", {"error": str(exc)}) from exc

    def inject_replay_anomaly(self, query_text: str | None = None) -> None:
        if query_text is None:
            history = self._state.get("plan_history", [])
            if history:
                query_text = history[-1]["normalized_sql"]
        if query_text is None:
            raise LabError(E_REPLAY, "no query is available for replay anomaly")
        plan = self.build_plan(query_text)
        self.validate_plan_replay(plan, simulate_mismatch=True)
        raise LabError(E_REPLAY, "plan replay mismatch was not detected")

    # ------------------------------------------------------------------
    # Structured logs
    # ------------------------------------------------------------------
    def record_log(self, event_key: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {"event": event_key, "payload": payload}
        serialized = canonical_json(event)
        event_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO _lab_logs(event_id, event_key, payload_json) VALUES(?, ?, ?)",
            (event_id, event_key, serialized),
        )
        return {"event_id": event_id, "event": event_key, "payload": payload}

    def list_logs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT event_id, event_key, payload_json FROM _lab_logs"
        ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "event_id": row["event_id"],
                    "event_key": row["event_key"],
                    **json.loads(row["payload_json"]),
                }
            )
        result.sort(key=lambda item: (item["event"], item["event_id"], item["event_key"]))
        return result
