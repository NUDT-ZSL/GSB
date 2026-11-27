"""SQLite persistence for the reuse kernel."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import SCHEMA_VERSION, TABLE_COLUMNS
from .normalize import NormalizedEvent, provenance_rows
from .util import canonical_json

ALL_TABLES = (
    "ledger_incident_index",
    "ledger",
    "consistency_reports",
    "query_results",
    "materialized",
    "candidates",
    "queries",
    "field_provenance",
    "incidents",
    "vehicles",
    "water_sources",
    "raw_v1",
    "raw_v2",
    "raw_v3",
    "update_batches",
    "meta",
)


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.db_path),
            isolation_level=None,
            timeout=30,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self._in_transaction = False
        self.ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        if self._in_transaction:
            raise RuntimeError("nested Store.transaction is not supported")
        self._in_transaction = True
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._in_transaction = False

    def ensure_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raw_v1 (
                event_no TEXT PRIMARY KEY,
                reported_epoch_s INTEGER NOT NULL,
                alarm_type_code INTEGER NOT NULL,
                level_code INTEGER NOT NULL,
                address_line TEXT NOT NULL,
                person_names TEXT NOT NULL,
                station_code TEXT NOT NULL,
                vehicle_code TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raw_v2 (
                call_id TEXT PRIMARY KEY,
                timestamp_ms INTEGER NOT NULL,
                incident_kind TEXT NOT NULL,
                priority_text TEXT NOT NULL,
                address_json TEXT NOT NULL,
                persons_json TEXT NOT NULL,
                station_id TEXT NOT NULL,
                vehicle_id TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS raw_v3 (
                incident_id TEXT PRIMARY KEY,
                document_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                occurred_at_ms INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                severity INTEGER NOT NULL,
                district TEXT NOT NULL,
                address_path TEXT NOT NULL,
                persons_json TEXT NOT NULL,
                station_id TEXT NOT NULL,
                vehicle_id TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                provenance_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_incidents_time
                ON incidents(occurred_at_ms);
            CREATE INDEX IF NOT EXISTS idx_incidents_source
                ON incidents(source_version);

            CREATE TABLE IF NOT EXISTS field_provenance (
                incident_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                source_version INTEGER NOT NULL,
                source_path TEXT NOT NULL,
                transform TEXT NOT NULL,
                PRIMARY KEY (incident_id, field_name),
                FOREIGN KEY (incident_id) REFERENCES incidents(incident_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS vehicles (
                vehicle_id TEXT PRIMARY KEY,
                station_id TEXT NOT NULL,
                status TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                source_version INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS water_sources (
                water_id TEXT PRIMARY KEY,
                district TEXT NOT NULL,
                capacity_tons INTEGER NOT NULL,
                pressure REAL NOT NULL,
                address_path TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS queries (
                query_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                spec_json TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                plan_fingerprint TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidates (
                fingerprint TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL UNIQUE,
                plan_json TEXT NOT NULL,
                refs_json TEXT NOT NULL,
                reference_count INTEGER NOT NULL,
                estimated_rows INTEGER NOT NULL,
                cost INTEGER NOT NULL,
                weight INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                selected INTEGER NOT NULL,
                reason TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS materialized (
                fingerprint TEXT PRIMARY KEY,
                blob BLOB NOT NULL,
                columns_json TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                lineage_json TEXT NOT NULL,
                version_vector_json TEXT NOT NULL,
                cost_estimate INTEGER NOT NULL,
                update_seq INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS query_results (
                query_id TEXT PRIMARY KEY,
                blob BLOB NOT NULL,
                columns_json TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                lineage_json TEXT NOT NULL,
                query_version INTEGER NOT NULL,
                data_versions_json TEXT NOT NULL,
                update_seq INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger (
                fingerprint TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                selected INTEGER NOT NULL,
                reference_count INTEGER NOT NULL,
                reuse_count INTEGER NOT NULL,
                materialized_rows INTEGER NOT NULL,
                cost INTEGER NOT NULL,
                saved_cost INTEGER NOT NULL,
                actual_cache_hits INTEGER NOT NULL,
                query_refs_json TEXT NOT NULL,
                decision_reason TEXT NOT NULL,
                update_seq INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger_incident_index (
                candidate_id TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                occurred_at_ms INTEGER NOT NULL,
                query_id TEXT NOT NULL,
                PRIMARY KEY (candidate_id, incident_id, query_id)
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_incident_id
                ON ledger_incident_index(incident_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_incident_time
                ON ledger_incident_index(occurred_at_ms);

            CREATE TABLE IF NOT EXISTS update_batches (
                seq INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('pending', 'applied')),
                applied_in_update INTEGER
            );

            CREATE TABLE IF NOT EXISTS consistency_reports (
                report_seq INTEGER PRIMARY KEY,
                report_json TEXT NOT NULL
            );
            """
        )
        if self.get_meta("schema_version") is None:
            self.set_meta("schema_version", SCHEMA_VERSION)

    def reset(self) -> None:
        for table in ALL_TABLES:
            self.conn.execute(f'DROP TABLE IF EXISTS "{table}"')
        self.ensure_schema()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return default
        return json.loads(row["value"])

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, canonical_json(value)),
        )

    def insert_raw_event(self, version: int, raw: dict[str, Any]) -> None:
        if version == 1:
            self.conn.execute(
                """
                INSERT INTO raw_v1(
                    event_no, reported_epoch_s, alarm_type_code, level_code,
                    address_line, person_names, station_code, vehicle_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw["event_no"],
                    raw["reported_epoch_s"],
                    raw["alarm_type_code"],
                    raw["level_code"],
                    raw["address_line"],
                    raw["person_names"],
                    raw["station_code"],
                    raw["vehicle_code"],
                ),
            )
        elif version == 2:
            self.conn.execute(
                """
                INSERT INTO raw_v2(
                    call_id, timestamp_ms, incident_kind, priority_text,
                    address_json, persons_json, station_id, vehicle_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw["call_id"],
                    raw["timestamp_ms"],
                    raw["incident_kind"],
                    raw["priority_text"],
                    raw["address_json"],
                    raw["persons_json"],
                    raw["station_id"],
                    raw["vehicle_id"],
                ),
            )
        elif version == 3:
            self.conn.execute(
                "INSERT INTO raw_v3(incident_id, document_json) VALUES (?, ?)",
                (raw["incident"]["id"], canonical_json(raw)),
            )
        else:
            raise ValueError(f"unsupported source version: {version}")

    def insert_normalized(self, event: NormalizedEvent) -> None:
        values = event.values
        self.conn.execute(
            """
            INSERT INTO incidents(
                incident_id, occurred_at_ms, event_type, severity, district,
                address_path, persons_json, station_id, vehicle_id,
                source_version, provenance_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                values["incident_id"],
                values["occurred_at_ms"],
                values["event_type"],
                values["severity"],
                values["district"],
                values["address_path"],
                values["persons_json"],
                values["station_id"],
                values["vehicle_id"],
                values["source_version"],
                canonical_json(event.provenance),
            ),
        )
        self.conn.executemany(
            """
            INSERT INTO field_provenance(
                incident_id, field_name, source_version, source_path, transform
            ) VALUES (?, ?, ?, ?, ?)
            """,
            provenance_rows(event),
        )

    def replace_vehicles(self, vehicles: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM vehicles")
        self.conn.executemany(
            """
            INSERT INTO vehicles(
                vehicle_id, station_id, status, capacity, source_version
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    item["vehicle_id"],
                    item["station_id"],
                    item["status"],
                    item["capacity"],
                    item["source_version"],
                )
                for item in vehicles
            ],
        )

    def replace_water_sources(self, water_sources: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM water_sources")
        self.conn.executemany(
            """
            INSERT INTO water_sources(
                water_id, district, capacity_tons, pressure, address_path
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    item["water_id"],
                    item["district"],
                    item["capacity_tons"],
                    item["pressure"],
                    item["address_path"],
                )
                for item in water_sources
            ],
        )

    def fetch_table(self, table: str) -> list[dict[str, Any]]:
        if table not in TABLE_COLUMNS:
            raise ValueError(f"unknown table: {table}")
        cursor = self.conn.execute(f'SELECT * FROM "{table}"')
        return [dict(row) for row in cursor.fetchall()]

    def table_row_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for table in TABLE_COLUMNS:
            row = self.conn.execute(f'SELECT COUNT(*) AS count FROM "{table}"').fetchone()
            result[table] = int(row["count"])
        return result

    def table_field_cardinalities(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for table, columns in TABLE_COLUMNS.items():
            for column in columns:
                row = self.conn.execute(
                    f'SELECT COUNT(DISTINCT "{column}") AS count FROM "{table}"'
                ).fetchone()
                result[f"{table}.{column}"] = int(row["count"])
        return result

    def upsert_query(self, spec: dict[str, Any], version: int, plan: Any) -> None:
        from .plan import plan_fingerprint, plan_to_json

        query_id = str(spec["query_id"])
        self.conn.execute(
            """
            INSERT INTO queries(
                query_id, version, spec_json, plan_json, plan_fingerprint
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(query_id) DO UPDATE SET
                version = excluded.version,
                spec_json = excluded.spec_json,
                plan_json = excluded.plan_json,
                plan_fingerprint = excluded.plan_fingerprint
            """,
            (
                query_id,
                version,
                canonical_json(spec),
                plan_to_json(plan),
                plan_fingerprint(plan),
            ),
        )

    def update_query_plan(self, query_id: str, plan: Any) -> None:
        from .plan import plan_fingerprint, plan_to_json

        self.conn.execute(
            "UPDATE queries SET plan_json = ?, plan_fingerprint = ? WHERE query_id = ?",
            (plan_to_json(plan), plan_fingerprint(plan), query_id),
        )

    def load_queries(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM queries ORDER BY query_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def get_query(self, query_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM queries WHERE query_id = ?",
            (query_id,),
        ).fetchone()
        return None if row is None else dict(row)

    def replace_candidates(self, decisions: list[Any]) -> None:
        self.conn.execute("DELETE FROM candidates")
        self.conn.executemany(
            """
            INSERT INTO candidates(
                fingerprint, candidate_id, plan_json, refs_json,
                reference_count, estimated_rows, cost, weight, rank,
                selected, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.candidate.fingerprint,
                    item.candidate.candidate_id,
                    item.candidate.plan_json,
                    canonical_json(list(item.candidate.refs)),
                    item.candidate.reference_count,
                    item.estimated_rows,
                    item.cost,
                    item.weight,
                    item.rank,
                    1 if item.selected else 0,
                    item.reason,
                )
                for item in decisions
            ],
        )

    def load_candidates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM candidates ORDER BY fingerprint"
        ).fetchall()
        return [dict(row) for row in rows]

    def load_materialized(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM materialized").fetchall()
        return {row["fingerprint"]: dict(row) for row in rows}

    def save_materialized(
        self,
        fingerprint: str,
        blob: bytes,
        columns: list[str],
        row_count: int,
        lineage: list[list[Any]],
        version_vector: dict[str, Any],
        cost_estimate: int,
        update_seq: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO materialized(
                fingerprint, blob, columns_json, row_count, lineage_json,
                version_vector_json, cost_estimate, update_seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                blob = excluded.blob,
                columns_json = excluded.columns_json,
                row_count = excluded.row_count,
                lineage_json = excluded.lineage_json,
                version_vector_json = excluded.version_vector_json,
                cost_estimate = excluded.cost_estimate,
                update_seq = excluded.update_seq
            """,
            (
                fingerprint,
                sqlite3.Binary(blob),
                canonical_json(columns),
                row_count,
                canonical_json(lineage),
                canonical_json(version_vector),
                cost_estimate,
                update_seq,
            ),
        )

    def delete_materialized(self, fingerprint: str) -> None:
        self.conn.execute(
            "DELETE FROM materialized WHERE fingerprint = ?",
            (fingerprint,),
        )

    def save_query_result(
        self,
        query_id: str,
        blob: bytes,
        columns: list[str],
        row_count: int,
        lineage: list[list[Any]],
        query_version: int,
        data_versions: dict[str, int],
        update_seq: int,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO query_results(
                query_id, blob, columns_json, row_count, lineage_json,
                query_version, data_versions_json, update_seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(query_id) DO UPDATE SET
                blob = excluded.blob,
                columns_json = excluded.columns_json,
                row_count = excluded.row_count,
                lineage_json = excluded.lineage_json,
                query_version = excluded.query_version,
                data_versions_json = excluded.data_versions_json,
                update_seq = excluded.update_seq
            """,
            (
                query_id,
                sqlite3.Binary(blob),
                canonical_json(columns),
                row_count,
                canonical_json(lineage),
                query_version,
                canonical_json(data_versions),
                update_seq,
            ),
        )

    def load_query_results(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM query_results ORDER BY query_id"
        ).fetchall()
        return {row["query_id"]: dict(row) for row in rows}

    def replace_ledger(
        self,
        entries: list[dict[str, Any]],
        incident_index: list[tuple[str, str, int, str]],
    ) -> None:
        self.conn.execute("DELETE FROM ledger")
        self.conn.execute("DELETE FROM ledger_incident_index")
        self.conn.executemany(
            """
            INSERT INTO ledger(
                fingerprint, candidate_id, selected, reference_count,
                reuse_count, materialized_rows, cost, saved_cost,
                actual_cache_hits, query_refs_json, decision_reason, update_seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item["fingerprint"],
                    item["candidate_id"],
                    1 if item["selected"] else 0,
                    item["reference_count"],
                    item["reuse_count"],
                    item["materialized_rows"],
                    item["cost"],
                    item["saved_cost"],
                    item["actual_cache_hits"],
                    canonical_json(item["query_refs"]),
                    item["decision_reason"],
                    item["update_seq"],
                )
                for item in entries
            ],
        )
        self.conn.executemany(
            """
            INSERT OR IGNORE INTO ledger_incident_index(
                candidate_id, incident_id, occurred_at_ms, query_id
            ) VALUES (?, ?, ?, ?)
            """,
            incident_index,
        )

    def load_ledger(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM ledger ORDER BY candidate_id"
        ).fetchall()
        return [dict(row) for row in rows]

    def lookup_ledger(
        self,
        incident_id: str | None = None,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if incident_id is not None:
            where.append("i.incident_id = ?")
            params.append(incident_id)
        if start_ms is not None:
            where.append("i.occurred_at_ms >= ?")
            params.append(int(start_ms))
        if end_ms is not None:
            where.append("i.occurred_at_ms < ?")
            params.append(int(end_ms))
        if not where:
            raise ValueError("provide incident_id, start_ms, or end_ms")
        rows = self.conn.execute(
            f"""
            SELECT DISTINCT
                l.candidate_id, l.fingerprint, l.selected, l.reference_count,
                l.reuse_count, l.materialized_rows, l.cost, l.saved_cost,
                l.actual_cache_hits, l.query_refs_json,
                i.incident_id, i.occurred_at_ms, i.query_id
            FROM ledger_incident_index i
            JOIN ledger l ON l.candidate_id = i.candidate_id
            WHERE {' AND '.join(where)}
            ORDER BY i.occurred_at_ms, l.candidate_id, i.query_id, i.incident_id
            """,
            params,
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["query_refs"] = json.loads(item.pop("query_refs_json"))
            item["selected"] = bool(item["selected"])
            result.append(item)
        return result

    def replace_update_batches(self, updates: list[dict[str, Any]]) -> None:
        self.conn.execute("DELETE FROM update_batches")
        self.conn.executemany(
            "INSERT INTO update_batches(seq, kind, payload_json, status) VALUES (?, ?, ?, 'pending')",
            [
                (index, str(update["kind"]), canonical_json(update))
                for index, update in enumerate(updates, start=1)
            ],
        )

    def next_pending_update(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            """
            SELECT * FROM update_batches
            WHERE status = 'pending'
            ORDER BY seq
            LIMIT 1
            """
        ).fetchone()
        if row is None:
            return None
        return {
            "seq": int(row["seq"]),
            "kind": row["kind"],
            "payload": json.loads(row["payload_json"]),
            "status": row["status"],
        }

    def mark_update_applied(self, seq: int, update_number: int) -> None:
        self.conn.execute(
            """
            UPDATE update_batches
            SET status = 'applied', applied_in_update = ?
            WHERE seq = ? AND status = 'pending'
            """,
            (update_number, seq),
        )

    def list_updates(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM update_batches ORDER BY seq"
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def save_consistency_report(self, report_seq: int, report: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO consistency_reports(report_seq, report_json)
            VALUES (?, ?)
            ON CONFLICT(report_seq) DO UPDATE SET report_json = excluded.report_json
            """,
            (report_seq, canonical_json(report)),
        )

    def load_consistency_report(self, report_seq: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT report_json FROM consistency_reports WHERE report_seq = ?",
            (report_seq,),
        ).fetchone()
        return None if row is None else json.loads(row["report_json"])

    def provenance(self, incident_id: str | None = None) -> list[dict[str, Any]]:
        if incident_id is None:
            rows = self.conn.execute(
                """
                SELECT incident_id, field_name, source_version, source_path, transform
                FROM field_provenance
                ORDER BY incident_id, field_name
                """
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT incident_id, field_name, source_version, source_path, transform
                FROM field_provenance
                WHERE incident_id = ?
                ORDER BY field_name
                """,
                (incident_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def normalized_event(self, incident_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (incident_id,),
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["persons"] = json.loads(result.pop("persons_json"))
        result["provenance"] = json.loads(result.pop("provenance_json"))
        return result

    def raw_counts(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for version in (1, 2, 3):
            table = f"raw_v{version}"
            row = self.conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
            result[f"v{version}"] = int(row["count"])
        return result
