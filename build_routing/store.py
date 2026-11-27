from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .engine import ReplayEngine, canonical_json, project_hash
from .model import NormalizedProject, SelectionResult
from .normalize import project_from_storage
from .stats import build_availability_stats

SCHEMA_VERSION = 1

TABLES = (
    "switch_events",
    "prefix_snapshots",
    "prefix_current",
    "committed_events",
    "runtime_state",
    "meta",
)


class WorkbenchStore:
    """SQLite persistence for event replay and read-only snapshots."""

    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        if self.db_path != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS committed_events (
                seq INTEGER PRIMARY KEY,
                at INTEGER NOT NULL,
                type TEXT NOT NULL,
                event_json TEXT NOT NULL,
                committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS prefix_current (
                prefix_id TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                converged_at INTEGER NOT NULL,
                last_change_at INTEGER NOT NULL,
                last_event_seq INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS prefix_snapshots (
                prefix_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL,
                observed_at INTEGER NOT NULL,
                node_id TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                converged_at INTEGER NOT NULL,
                last_change_at INTEGER NOT NULL,
                PRIMARY KEY (prefix_id, event_seq)
            );
            CREATE INDEX IF NOT EXISTS idx_snapshots_at
                ON prefix_snapshots (observed_at, prefix_id, event_seq);
            CREATE TABLE IF NOT EXISTS switch_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prefix_id TEXT NOT NULL,
                event_seq INTEGER NOT NULL,
                at INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                change_kind TEXT NOT NULL,
                old_best_next_hop_id TEXT,
                new_best_next_hop_id TEXT,
                old_backup_next_hop_id TEXT,
                new_backup_next_hop_id TEXT,
                old_best_json TEXT,
                new_best_json TEXT,
                old_backup_json TEXT,
                new_backup_json TEXT,
                details_json TEXT NOT NULL,
                UNIQUE (prefix_id, event_seq, change_kind)
            );
            CREATE INDEX IF NOT EXISTS idx_switches_prefix_at
                ON switch_events (prefix_id, at, event_seq);
            CREATE TABLE IF NOT EXISTS runtime_state (
                key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL
            );
            """
        )

    def _drop_schema(self, connection: sqlite3.Connection) -> None:
        for table in TABLES:
            connection.execute(f"DROP TABLE IF EXISTS {table}")

    def ensure_schema(self) -> None:
        with self._connection() as connection:
            self._create_schema(connection)
            connection.commit()

    @staticmethod
    def _set_meta(
        connection: sqlite3.Connection, key: str, value: Any
    ) -> None:
        encoded = value if isinstance(value, str) else canonical_json(value)
        connection.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, encoded),
        )

    @staticmethod
    def _get_meta(
        connection: sqlite3.Connection, key: str, default: Any = None
    ) -> Any:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return default
        return row["value"]

    @staticmethod
    def _get_json_meta(
        connection: sqlite3.Connection, key: str, default: Any = None
    ) -> Any:
        value = WorkbenchStore._get_meta(connection, key, None)
        if value is None:
            return default
        return json.loads(value)

    def initialize(
        self,
        project: NormalizedProject,
        events: Sequence[Mapping[str, Any]],
        *,
        reset: bool = False,
        initial_time: int = 0,
    ) -> ReplayEngine:
        engine = ReplayEngine(
            project, events, initial_time=initial_time, validate=True
        )
        events_hash = hashlib.sha256(
            canonical_json(engine.events).encode("utf-8")
        ).hexdigest()
        with self._connection() as connection:
            if reset:
                self._drop_schema(connection)
            self._create_schema(connection)
            existing_hash = self._get_meta(connection, "project_hash")
            if existing_hash and not reset:
                if existing_hash != project_hash(project):
                    raise ValueError(
                        "database belongs to a different normalized project; "
                        "use reset or a new database"
                    )
                existing_events_hash = self._get_meta(connection, "events_hash")
                if existing_events_hash != events_hash:
                    raise ValueError(
                        "database event stream differs from the supplied events"
                    )
                connection.commit()
                return engine

            with connection:
                self._set_meta(connection, "schema_version", SCHEMA_VERSION)
                self._set_meta(connection, "project_id", project.project_id)
                self._set_meta(connection, "project_hash", project_hash(project))
                self._set_meta(
                    connection, "project_json", canonical_json(project.to_dict())
                )
                self._set_meta(connection, "events_hash", events_hash)
                self._set_meta(connection, "events_json", canonical_json(engine.events))
                self._set_meta(connection, "initial_time", int(initial_time))
                self._set_meta(connection, "last_event_seq", 0)
                self._set_meta(connection, "converged_at", int(initial_time))
                runtime = engine.runtime_state()
                connection.execute(
                    """
                    INSERT INTO runtime_state (key, value_json)
                    VALUES ('engine', ?)
                    ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                    """,
                    (canonical_json(runtime),),
                )
                for prefix in project.prefixes:
                    state = engine.states[prefix.id]
                    snapshot_json = canonical_json(state.to_dict())
                    connection.execute(
                        """
                        INSERT INTO prefix_current (
                            prefix_id, node_id, snapshot_json, converged_at,
                            last_change_at, last_event_seq
                        ) VALUES (?, ?, ?, ?, ?, 0)
                        ON CONFLICT(prefix_id) DO UPDATE SET
                            node_id = excluded.node_id,
                            snapshot_json = excluded.snapshot_json,
                            converged_at = excluded.converged_at,
                            last_change_at = excluded.last_change_at,
                            last_event_seq = excluded.last_event_seq
                        """,
                        (
                            prefix.id,
                            prefix.build_node_id,
                            snapshot_json,
                            state.converged_at,
                            state.last_change_at,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO prefix_snapshots (
                            prefix_id, event_seq, observed_at, node_id,
                            snapshot_json, converged_at, last_change_at
                        ) VALUES (?, 0, ?, ?, ?, ?, ?)
                        """,
                        (
                            prefix.id,
                            int(initial_time),
                            prefix.build_node_id,
                            snapshot_json,
                            state.converged_at,
                            state.last_change_at,
                        ),
                    )
        return engine

    def _commit_event(
        self,
        engine: ReplayEngine,
        report: Mapping[str, Any],
    ) -> None:
        event = report["event"]
        seq = int(event["seq"])
        affected = list(report["affected_prefix_ids"])
        with self._connection() as connection:
            with connection:
                for prefix_id in affected:
                    prefix = engine.project.prefix_by_id()[prefix_id]
                    state = engine.states[prefix_id]
                    snapshot_json = canonical_json(state.to_dict())
                    connection.execute(
                        """
                        INSERT INTO prefix_current (
                            prefix_id, node_id, snapshot_json, converged_at,
                            last_change_at, last_event_seq
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(prefix_id) DO UPDATE SET
                            node_id = excluded.node_id,
                            snapshot_json = excluded.snapshot_json,
                            converged_at = excluded.converged_at,
                            last_change_at = excluded.last_change_at,
                            last_event_seq = excluded.last_event_seq
                        """,
                        (
                            prefix_id,
                            prefix.build_node_id,
                            snapshot_json,
                            state.converged_at,
                            state.last_change_at,
                            seq,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT INTO prefix_snapshots (
                            prefix_id, event_seq, observed_at, node_id,
                            snapshot_json, converged_at, last_change_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(prefix_id, event_seq) DO UPDATE SET
                            observed_at = excluded.observed_at,
                            node_id = excluded.node_id,
                            snapshot_json = excluded.snapshot_json,
                            converged_at = excluded.converged_at,
                            last_change_at = excluded.last_change_at
                        """,
                        (
                            prefix_id,
                            seq,
                            int(event["at"]),
                            prefix.build_node_id,
                            snapshot_json,
                            state.converged_at,
                            state.last_change_at,
                        ),
                    )
                    for switch in engine.switches[prefix_id]:
                        if switch.event_seq != seq:
                            continue
                        old_best_json = (
                            canonical_json(switch.old_best)
                            if switch.old_best is not None
                            else None
                        )
                        new_best_json = (
                            canonical_json(switch.new_best)
                            if switch.new_best is not None
                            else None
                        )
                        old_backup_json = (
                            canonical_json(switch.old_backup)
                            if switch.old_backup is not None
                            else None
                        )
                        new_backup_json = (
                            canonical_json(switch.new_backup)
                            if switch.new_backup is not None
                            else None
                        )
                        connection.execute(
                            """
                            INSERT OR IGNORE INTO switch_events (
                                prefix_id, event_seq, at, event_type, change_kind,
                                old_best_next_hop_id, new_best_next_hop_id,
                                old_backup_next_hop_id, new_backup_next_hop_id,
                                old_best_json, new_best_json,
                                old_backup_json, new_backup_json, details_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                prefix_id,
                                seq,
                                switch.at,
                                switch.event_type,
                                switch.change_kind,
                                (
                                    switch.old_best.get("next_hop_id")
                                    if switch.old_best
                                    else None
                                ),
                                (
                                    switch.new_best.get("next_hop_id")
                                    if switch.new_best
                                    else None
                                ),
                                (
                                    switch.old_backup.get("next_hop_id")
                                    if switch.old_backup
                                    else None
                                ),
                                (
                                    switch.new_backup.get("next_hop_id")
                                    if switch.new_backup
                                    else None
                                ),
                                old_best_json,
                                new_best_json,
                                old_backup_json,
                                new_backup_json,
                                canonical_json(switch.details),
                            ),
                        )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO committed_events (
                        seq, at, type, event_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        seq,
                        int(event["at"]),
                        str(event["type"]),
                        canonical_json(event),
                    ),
                )
                runtime = engine.runtime_state()
                connection.execute(
                    """
                    INSERT INTO runtime_state (key, value_json)
                    VALUES ('engine', ?)
                    ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json
                    """,
                    (canonical_json(runtime),),
                )
                self._set_meta(connection, "last_event_seq", seq)
                self._set_meta(connection, "converged_at", engine.converged_at)

    def load_project(self) -> NormalizedProject:
        with self._connection() as connection:
            raw = self._get_json_meta(connection, "project_json")
        if raw is None:
            raise ValueError("database is not initialized")
        return project_from_storage(raw)

    def load_events(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            events = self._get_json_meta(connection, "events_json")
        if events is None:
            raise ValueError("database is not initialized")
        return [dict(item) for item in events]

    def get_runtime_state(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM runtime_state WHERE key = 'engine'"
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])

    def _committed_integrity(
        self, connection: sqlite3.Connection, expected_events: Sequence[Mapping[str, Any]]
    ) -> int:
        rows = connection.execute(
            "SELECT seq, at, type, event_json FROM committed_events ORDER BY seq"
        ).fetchall()
        by_seq = {int(item["seq"]): item for item in expected_events}
        last = 0
        for row in rows:
            seq = int(row["seq"])
            expected = by_seq.get(seq)
            if expected is None:
                raise ValueError(f"committed event seq {seq} is absent from stream")
            if (
                int(expected["at"]) != int(row["at"])
                or str(expected["type"]) != str(row["type"])
                or canonical_json(expected) != str(row["event_json"])
            ):
                raise ValueError(f"committed event seq {seq} differs from event stream")
            last = seq
        if rows:
            seqs = [int(item["seq"]) for item in rows]
            if seqs != list(range(1, max(seqs) + 1)):
                raise ValueError("committed event sequence is not contiguous")
        return last

    def replay(
        self,
        project: NormalizedProject | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
        *,
        max_new_events: int | None = None,
    ) -> dict[str, Any]:
        if project is None:
            project = self.load_project()
        if events is None:
            events = self.load_events()
        self.initialize(project, events)
        runtime = self.get_runtime_state()
        if runtime is None:
            raise ValueError("database runtime state is missing")
        with self._connection() as connection:
            last_committed = self._committed_integrity(connection, events)
        engine = ReplayEngine(project, events)
        engine.restore_runtime(runtime)
        if engine.last_event_seq != last_committed:
            raise ValueError(
                "runtime state and committed event log disagree at the resume point"
            )
        pending = [
            event for event in engine.events if int(event["seq"]) > last_committed
        ]
        if max_new_events is not None:
            pending = pending[: int(max_new_events)]
        reports = []
        for event in pending:
            report = engine.apply_event(event)
            self._commit_event(engine, report)
            reports.append(report)
        return {
            "resumed_from_event_seq": last_committed,
            "processed_event_count": len(reports),
            "last_event_seq": engine.last_event_seq,
            "converged_at": engine.converged_at,
            "event_reports": reports,
            "current": engine.current_report(),
        }

    def bootstrap(
        self,
        project: NormalizedProject,
        events: Sequence[Mapping[str, Any]],
        *,
        reset: bool = True,
        initial_time: int = 0,
        max_new_events: int | None = None,
    ) -> dict[str, Any]:
        self.initialize(
            project, events, reset=reset, initial_time=initial_time
        )
        return self.replay(
            project, events, max_new_events=max_new_events
        )

    def last_committed_event(self) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT seq, at, type, event_json, committed_at
                FROM committed_events
                ORDER BY seq DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return {
            "seq": int(row["seq"]),
            "at": int(row["at"]),
            "type": str(row["type"]),
            "event": json.loads(row["event_json"]),
            "committed_at": str(row["committed_at"]),
        }

    def _prefix_metadata(
        self, project: NormalizedProject
    ) -> dict[str, dict[str, Any]]:
        nodes = project.node_by_id()
        regions = project.region_by_id()
        result: dict[str, dict[str, Any]] = {}
        for prefix in project.prefixes:
            node = nodes.get(prefix.build_node_id)
            region = regions.get(node.region_id) if node else None
            result[prefix.id] = {
                "cidr": prefix.cidr,
                "repository": prefix.repository,
                "node_id": prefix.build_node_id,
                "node_name": node.name if node else prefix.build_node_id,
                "region_id": node.region_id if node else None,
                "region_name": region.name if region else None,
            }
        return result

    def _row_snapshot(
        self,
        row: sqlite3.Row,
        metadata: Mapping[str, Any],
        *,
        source: str,
        observed_at: int,
        event_seq: int,
        last_event_seq: int | None = None,
    ) -> dict[str, Any]:
        state = json.loads(row["snapshot_json"])
        result = state["result"]
        return {
            "prefix_id": row["prefix_id"],
            **dict(metadata),
            "observed_at": int(observed_at),
            "event_seq": int(event_seq),
            "last_event_seq": (
                int(last_event_seq) if last_event_seq is not None else int(event_seq)
            ),
            "converged_at": int(row["converged_at"]),
            "last_change_at": int(row["last_change_at"]),
            "status": result["status"],
            "best": result["best"],
            "backup": result["backup"],
            "basis": result["basis"],
            "assessments": result["assessments"],
            "result": result,
            "snapshot_source": source,
        }

    def snapshots(
        self,
        *,
        at: int | None = None,
        node_id: str | None = None,
        prefix_id: str | None = None,
    ) -> list[dict[str, Any]]:
        project = self.load_project()
        metadata = self._prefix_metadata(project)
        with self._connection() as connection:
            if at is None:
                sql = "SELECT * FROM prefix_current"
                clauses: list[str] = []
                params: list[Any] = []
                if node_id is not None:
                    clauses.append("node_id = ?")
                    params.append(node_id)
                if prefix_id is not None:
                    clauses.append("prefix_id = ?")
                    params.append(prefix_id)
                if clauses:
                    sql += " WHERE " + " AND ".join(clauses)
                sql += " ORDER BY prefix_id"
                rows = connection.execute(sql, params).fetchall()
                source = "sqlite.prefix_current"
                convergence_row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'converged_at'"
                ).fetchone()
                current_at = (
                    int(convergence_row["value"])
                    if convergence_row is not None
                    else 0
                )
                return [
                    self._row_snapshot(
                        row,
                        metadata.get(row["prefix_id"], {}),
                        source=source,
                        observed_at=current_at,
                        event_seq=int(row["last_event_seq"]),
                    )
                    for row in rows
                ]

            sql = """
                SELECT s.*
                FROM prefix_snapshots s
                JOIN (
                    SELECT prefix_id, MAX(event_seq) AS event_seq
                    FROM prefix_snapshots
                    WHERE observed_at <= ?
                    GROUP BY prefix_id
                ) latest
                  ON latest.prefix_id = s.prefix_id
                 AND latest.event_seq = s.event_seq
            """
            params = [int(at)]
            clauses = []
            if node_id is not None:
                clauses.append("s.node_id = ?")
                params.append(node_id)
            if prefix_id is not None:
                clauses.append("s.prefix_id = ?")
                params.append(prefix_id)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY s.prefix_id"
            rows = connection.execute(sql, params).fetchall()
        return [
            self._row_snapshot(
                row,
                metadata.get(row["prefix_id"], {}),
                source="sqlite.prefix_snapshots",
                observed_at=int(row["observed_at"]),
                event_seq=int(row["event_seq"]),
            )
            for row in rows
        ]

    def snapshot_at(
        self,
        at: int,
        *,
        node_id: str | None = None,
        prefix_id: str | None = None,
    ) -> dict[str, Any]:
        snapshots = self.snapshots(at=at, node_id=node_id, prefix_id=prefix_id)
        return {
            "at": int(at),
            "source": "sqlite.prefix_snapshots",
            "recomputed": False,
            "converged_at": max(
                (item["converged_at"] for item in snapshots), default=0
            ),
            "prefixes": snapshots,
        }

    def switch_history(
        self, prefix_id: str, *, at: int | None = None
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if at is None:
                rows = connection.execute(
                    """
                    SELECT * FROM switch_events
                    WHERE prefix_id = ?
                    ORDER BY at, event_seq, id
                    """,
                    (prefix_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM switch_events
                    WHERE prefix_id = ? AND at <= ?
                    ORDER BY at, event_seq, id
                    """,
                    (prefix_id, int(at)),
                ).fetchall()
        return [
            {
                "prefix_id": row["prefix_id"],
                "event_seq": int(row["event_seq"]),
                "at": int(row["at"]),
                "event_type": row["event_type"],
                "change_kind": row["change_kind"],
                "old_best_next_hop_id": row["old_best_next_hop_id"],
                "new_best_next_hop_id": row["new_best_next_hop_id"],
                "old_backup_next_hop_id": row["old_backup_next_hop_id"],
                "new_backup_next_hop_id": row["new_backup_next_hop_id"],
                "old_best": (
                    json.loads(row["old_best_json"])
                    if row["old_best_json"]
                    else None
                ),
                "new_best": (
                    json.loads(row["new_best_json"])
                    if row["new_best_json"]
                    else None
                ),
                "old_backup": (
                    json.loads(row["old_backup_json"])
                    if row["old_backup_json"]
                    else None
                ),
                "new_backup": (
                    json.loads(row["new_backup_json"])
                    if row["new_backup_json"]
                    else None
                ),
                "details": json.loads(row["details_json"]),
            }
            for row in rows
        ]

    def prefix_detail(
        self, prefix_id: str, *, at: int | None = None
    ) -> dict[str, Any]:
        snapshots = self.snapshots(at=at, prefix_id=prefix_id)
        if not snapshots:
            raise KeyError(prefix_id)
        snapshot = snapshots[0]
        history = self.switch_history(prefix_id, at=at)
        return {
            **snapshot,
            "history": history,
            "history_count": len(history),
        }

    def nodes(self) -> list[dict[str, Any]]:
        project = self.load_project()
        regions = project.region_by_id()
        prefix_counts: dict[str, int] = {}
        for prefix in project.prefixes:
            prefix_counts[prefix.build_node_id] = (
                prefix_counts.get(prefix.build_node_id, 0) + 1
            )
        return [
            {
                "node_id": node.id,
                "node_name": node.name,
                "region_id": node.region_id,
                "region_name": regions[node.region_id].name
                if node.region_id in regions
                else None,
                "prefix_count": prefix_counts.get(node.id, 0),
            }
            for node in sorted(project.build_nodes, key=lambda item: item.id)
        ]

    def stats_at(self, at: int | None = None) -> dict[str, Any]:
        project = self.load_project()
        snapshots = self.snapshots(at=at)
        results = {
            item["prefix_id"]: SelectionResult.from_dict(item["result"])
            for item in snapshots
        }
        stats = build_availability_stats(project, results)
        stats["snapshot_at"] = at
        stats["snapshot_source"] = (
            "sqlite.prefix_current"
            if at is None
            else "sqlite.prefix_snapshots"
        )
        stats["converged_at"] = max(
            (item["converged_at"] for item in snapshots), default=0
        )
        return stats

    def events(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT seq, at, type, event_json, committed_at "
                "FROM committed_events ORDER BY seq"
            ).fetchall()
        return [
            {
                "seq": int(row["seq"]),
                "at": int(row["at"]),
                "type": str(row["type"]),
                "event": json.loads(row["event_json"]),
                "committed_at": str(row["committed_at"]),
            }
            for row in rows
        ]

    def metadata(self) -> dict[str, Any]:
        with self._connection() as connection:
            meta = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM meta ORDER BY key"
                ).fetchall()
            }
            runtime = self.get_runtime_state()
            last = self.last_committed_event()
        project = self.load_project()
        return {
            "project": {
                "id": project.project_id,
                "name": project.name,
                "schema_version": project.schema_version,
                "source_schema": project.source_schema,
                "seed": project.seed,
            },
            "database": self.db_path,
            "schema_version": SCHEMA_VERSION,
            "last_event_seq": int(meta.get("last_event_seq", 0)),
            "converged_at": int(meta.get("converged_at", 0)),
            "last_committed_event": last,
            "runtime_converged_at": (
                runtime.get("converged_at") if runtime is not None else None
            ),
            "counts": {
                **self._table_counts(),
            },
        }

    def _table_counts(self) -> dict[str, int]:
        with self._connection() as connection:
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                )
                for table in (
                    "committed_events",
                    "prefix_current",
                    "prefix_snapshots",
                    "switch_events",
                )
            }

    def integrity_check(self) -> dict[str, Any]:
        with self._connection() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()[0]
            counts = self._table_counts()
        return {"ok": result == "ok", "sqlite": result, "counts": counts}
