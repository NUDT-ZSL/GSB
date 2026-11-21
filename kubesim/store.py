from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

from .errors import ConflictError, StorageError
from .jsonutil import canonical_json, digest, pretty_json
from .models import (
    Dataset,
    Pod,
    Resources,
    ScheduleConfig,
    labels_match,
    parse_dataset,
    parse_pod,
    topology_domain_keys,
)
from .runtime import BoundPod, ClusterState, NodeState

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - Unix
    msvcrt = None


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


def _thread_lock(path: str) -> threading.RLock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(path)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[path] = lock
        return lock


class _AdvisoryLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None
        self.thread_lock = _thread_lock(str(path.resolve()))

    def __enter__(self) -> "_AdvisoryLock":
        self.thread_lock.acquire()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        if fcntl is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.handle is not None:
            if fcntl is not None:
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                self.handle.seek(0)
                try:
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            self.handle.close()
            self.handle = None
        self.thread_lock.release()


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_text = str(self.db_path)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_text,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with _AdvisoryLock(self.db_path.with_suffix(self.db_path.suffix + ".init.lock")):
            connection = self.connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS datasets (
                        digest TEXT PRIMARY KEY,
                        dataset_json TEXT NOT NULL,
                        created_seq INTEGER NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS nodes (
                        name TEXT PRIMARY KEY,
                        capacity_cpu INTEGER NOT NULL,
                        capacity_memory INTEGER NOT NULL,
                        capacity_gpu INTEGER NOT NULL,
                        remaining_cpu INTEGER NOT NULL,
                        remaining_memory INTEGER NOT NULL,
                        remaining_gpu INTEGER NOT NULL,
                        labels_json TEXT NOT NULL,
                        taints_json TEXT NOT NULL,
                        resource_version INTEGER NOT NULL DEFAULT 0,
                        updated_clock INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS pods (
                        name TEXT PRIMARY KEY,
                        spec_digest TEXT NOT NULL,
                        spec_json TEXT NOT NULL,
                        labels_json TEXT NOT NULL,
                        phase TEXT NOT NULL DEFAULT 'PENDING',
                        last_round TEXT,
                        updated_clock INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS rounds (
                        round_id TEXT PRIMARY KEY,
                        round_number INTEGER NOT NULL UNIQUE,
                        batch_digest TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL,
                        input_digest TEXT NOT NULL,
                        content_digest TEXT NOT NULL DEFAULT '',
                        config_json TEXT NOT NULL,
                        config_digest TEXT NOT NULL,
                        initial_versions_json TEXT NOT NULL,
                        start_clock_ms INTEGER NOT NULL,
                        clock_ms INTEGER NOT NULL,
                        end_clock_ms INTEGER,
                        budget_ms INTEGER NOT NULL,
                        report_json TEXT,
                        report_digest TEXT,
                        created_seq INTEGER NOT NULL,
                        committed_seq INTEGER
                    );

                    CREATE TABLE IF NOT EXISTS round_pods (
                        round_id TEXT NOT NULL,
                        pod_name TEXT NOT NULL,
                        pod_version TEXT NOT NULL,
                        schedule_order INTEGER NOT NULL,
                        priority INTEGER NOT NULL,
                        workload TEXT NOT NULL,
                        spec_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        outcome TEXT,
                        reason_code TEXT,
                        reason TEXT,
                        selected_node TEXT,
                        started_at_ms INTEGER,
                        finished_at_ms INTEGER,
                        chain_json TEXT,
                        chain_digest TEXT,
                        decision_digest TEXT,
                        recovery_json TEXT,
                        PRIMARY KEY (round_id, pod_name, pod_version),
                        UNIQUE (round_id, schedule_order),
                        FOREIGN KEY (round_id) REFERENCES rounds(round_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_round_pods_pending
                    ON round_pods(round_id, status, schedule_order);

                    CREATE TABLE IF NOT EXISTS bindings (
                        binding_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        round_id TEXT NOT NULL,
                        pod_name TEXT NOT NULL,
                        pod_version TEXT NOT NULL,
                        node_name TEXT NOT NULL,
                        cpu_millicores INTEGER NOT NULL,
                        memory_mib INTEGER NOT NULL,
                        gpu INTEGER NOT NULL,
                        resource_version_before INTEGER NOT NULL,
                        resource_version_after INTEGER NOT NULL,
                        topology_delta_json TEXT NOT NULL,
                        decision_digest TEXT NOT NULL,
                        committed_clock_ms INTEGER NOT NULL,
                        UNIQUE (round_id, pod_name, pod_version),
                        FOREIGN KEY (round_id) REFERENCES rounds(round_id) ON DELETE CASCADE,
                        FOREIGN KEY (node_name) REFERENCES nodes(name)
                    );

                    CREATE INDEX IF NOT EXISTS idx_bindings_node
                    ON bindings(node_name);

                    CREATE TABLE IF NOT EXISTS topology_domains (
                        topology_key TEXT NOT NULL,
                        domain TEXT NOT NULL,
                        pod_count INTEGER NOT NULL,
                        version INTEGER NOT NULL,
                        PRIMARY KEY (topology_key, domain)
                    );

                    CREATE TABLE IF NOT EXISTS round_snapshots (
                        round_id TEXT PRIMARY KEY,
                        round_number INTEGER NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        snapshot_digest TEXT NOT NULL,
                        logical_clock_ms INTEGER NOT NULL,
                        created_seq INTEGER NOT NULL,
                        FOREIGN KEY (round_id) REFERENCES rounds(round_id) ON DELETE CASCADE
                    );
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', '1')"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('logical_clock_ms', '0')"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES('sequence', '0')"
                )
            finally:
                connection.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        lock_path = self.db_path.with_suffix(self.db_path.suffix + ".write.lock")
        with _AdvisoryLock(lock_path):
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    yield connection
                except Exception:
                    connection.execute("ROLLBACK")
                    raise
                else:
                    connection.execute("COMMIT")
            finally:
                connection.close()

    def execution_lock(self, round_id: str) -> _AdvisoryLock:
        return _AdvisoryLock(
            self.db_path.with_suffix(self.db_path.suffix + f".round-{round_id}.lock")
        )

    @staticmethod
    def _next_seq(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT value FROM meta WHERE key='sequence'").fetchone()
        sequence = int(row["value"]) + 1
        connection.execute(
            "UPDATE meta SET value=? WHERE key='sequence'",
            (str(sequence),),
        )
        return sequence

    @staticmethod
    def _meta_int(connection: sqlite3.Connection, key: str) -> int:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return int(row["value"]) if row is not None else 0

    @staticmethod
    def _set_meta_int(connection: sqlite3.Connection, key: str, value: int) -> None:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def import_dataset(self, dataset: Dataset) -> dict[str, Any]:
        self.initialize()
        dataset_json = canonical_json(dataset.to_dict())
        dataset_digest = digest(dataset.to_dict())
        with self._transaction() as connection:
            bindings_exist = (
                connection.execute("SELECT 1 FROM bindings LIMIT 1").fetchone() is not None
            )
            for node in dataset.nodes:
                existing = connection.execute(
                    "SELECT * FROM nodes WHERE name=?", (node.name,)
                ).fetchone()
                labels_json = canonical_json(node.labels)
                taints_json = canonical_json([taint.to_dict() for taint in node.taints])
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO nodes(
                            name, capacity_cpu, capacity_memory, capacity_gpu,
                            remaining_cpu, remaining_memory, remaining_gpu,
                            labels_json, taints_json, resource_version, updated_clock
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
                        """,
                        (
                            node.name,
                            node.resources.cpu_millicores,
                            node.resources.memory_mib,
                            node.resources.gpu,
                            node.resources.cpu_millicores,
                            node.resources.memory_mib,
                            node.resources.gpu,
                            labels_json,
                            taints_json,
                        ),
                    )
                else:
                    same_static = (
                        existing["capacity_cpu"] == node.resources.cpu_millicores
                        and existing["capacity_memory"] == node.resources.memory_mib
                        and existing["capacity_gpu"] == node.resources.gpu
                        and existing["labels_json"] == labels_json
                        and existing["taints_json"] == taints_json
                    )
                    if not same_static:
                        if bindings_exist:
                            raise ConflictError(
                                "E_STATE_CONFLICT",
                                "cannot mutate node static state after bindings exist",
                                {"node": node.name},
                            )
                        connection.execute(
                            """
                            UPDATE nodes SET
                                capacity_cpu=?, capacity_memory=?, capacity_gpu=?,
                                remaining_cpu=?, remaining_memory=?, remaining_gpu=?,
                                labels_json=?, taints_json=?
                            WHERE name=?
                            """,
                            (
                                node.resources.cpu_millicores,
                                node.resources.memory_mib,
                                node.resources.gpu,
                                node.resources.cpu_millicores,
                                node.resources.memory_mib,
                                node.resources.gpu,
                                labels_json,
                                taints_json,
                                node.name,
                            ),
                        )
            for pod in dataset.pods:
                spec_digest = pod.version
                spec_json = canonical_json(pod.to_dict())
                labels_json = canonical_json(pod.labels)
                existing = connection.execute(
                    "SELECT * FROM pods WHERE name=?", (pod.name,)
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO pods(
                            name, spec_digest, spec_json, labels_json,
                            phase, last_round, updated_clock
                        ) VALUES (?, ?, ?, ?, 'PENDING', NULL, 0)
                        """,
                        (pod.name, spec_digest, spec_json, labels_json),
                    )
                elif existing["spec_digest"] != spec_digest:
                    bound = connection.execute(
                        "SELECT 1 FROM bindings WHERE pod_name=? LIMIT 1",
                        (pod.name,),
                    ).fetchone()
                    if bound is not None:
                        raise ConflictError(
                            "E_STATE_CONFLICT",
                            "cannot mutate a bound pod specification",
                            {"pod": pod.name},
                        )
                    connection.execute(
                        """
                        UPDATE pods SET spec_digest=?, spec_json=?, labels_json=?
                        WHERE name=?
                        """,
                        (spec_digest, spec_json, labels_json, pod.name),
                    )
            sequence = self._next_seq(connection)
            connection.execute(
                "INSERT OR IGNORE INTO datasets(digest, dataset_json, created_seq) VALUES (?, ?, ?)",
                (dataset_digest, dataset_json, sequence),
            )
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('latest_dataset', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (dataset_digest,),
            )
        return {
            "dataset_digest": dataset_digest,
            "nodes": len(dataset.nodes),
            "pods": len(dataset.pods),
            "filters": list(dataset.config.filters),
            "logical_clock_ms": self.logical_clock(),
        }

    def logical_clock(self) -> int:
        if not self.db_path.exists():
            return 0
        connection = self.connect()
        try:
            return self._meta_int(connection, "logical_clock_ms")
        finally:
            connection.close()

    def latest_dataset(self) -> Dataset:
        if not self.db_path.exists():
            raise StorageError("E_NOT_FOUND", "database does not exist")
        connection = self.connect()
        try:
            latest = connection.execute(
                "SELECT value FROM meta WHERE key='latest_dataset'"
            ).fetchone()
            if latest is None:
                raise StorageError("E_NOT_FOUND", "no dataset has been loaded")
            row = connection.execute(
                "SELECT dataset_json FROM datasets WHERE digest=?",
                (latest["value"],),
            ).fetchone()
            if row is None:
                raise StorageError("E_NOT_FOUND", "latest dataset is missing")
            payload = json.loads(row["dataset_json"])
        finally:
            connection.close()
        return parse_dataset(
            payload["nodes"],
            payload["pods"],
            payload["config"],
            payload.get("injections", []),
        )

    def get_dataset(self, dataset_digest: str) -> Dataset:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT dataset_json FROM datasets WHERE digest=?",
                (dataset_digest,),
            ).fetchone()
            if row is None:
                raise StorageError(
                    "E_NOT_FOUND",
                    "dataset is not loaded",
                    {"dataset_digest": dataset_digest},
                )
            payload = json.loads(row["dataset_json"])
        finally:
            connection.close()
        return parse_dataset(
            payload["nodes"],
            payload["pods"],
            payload["config"],
            payload.get("injections", []),
        )

    def cluster_state(self) -> ClusterState:
        connection = self.connect()
        try:
            node_rows = connection.execute(
                "SELECT * FROM nodes ORDER BY name"
            ).fetchall()
            binding_rows = connection.execute(
                """
                SELECT b.pod_name, b.node_name, p.labels_json
                FROM bindings b
                JOIN pods p ON p.name=b.pod_name
                ORDER BY b.binding_id
                """
            ).fetchall()
        finally:
            connection.close()
        nodes: list[NodeState] = []
        for row in node_rows:
            nodes.append(_node_state_from_row(row))
        bound_pods = tuple(
            BoundPod(
                name=row["pod_name"],
                node_name=row["node_name"],
                labels=json.loads(row["labels_json"]),
            )
            for row in binding_rows
        )
        return ClusterState(tuple(nodes), bound_pods)

    def topology_state(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM topology_domains ORDER BY topology_key, domain"
            ).fetchall()
        finally:
            connection.close()
        return [
            {
                "topology_key": row["topology_key"],
                "domain": row["domain"],
                "pod_count": int(row["pod_count"]),
                "version": int(row["version"]),
            }
            for row in rows
        ]

    def round_ledger(self, round_id: str) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """
                SELECT pod_name, pod_version, outcome, selected_node,
                       reason_code, reason, chain_digest, decision_digest
                FROM round_pods WHERE round_id=? ORDER BY schedule_order
                """,
                (round_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def node_versions(self) -> dict[str, int]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT name, resource_version FROM nodes ORDER BY name"
            ).fetchall()
        finally:
            connection.close()
        return {row["name"]: int(row["resource_version"]) for row in rows}

    def compute_input_digest(
        self,
        batch_digest: str,
        config: ScheduleConfig,
        pods: tuple[Pod, ...],
        versions: dict[str, int],
    ) -> str:
        return digest(
            {
                "batch_digest": batch_digest,
                "config_digest": digest(config.to_dict()),
                "pod_versions": {pod.name: pod.version for pod in pods},
                "initial_resource_versions": dict(sorted(versions.items())),
            }
        )

    def get_round(self, round_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone()
            if row is None:
                return None
            pods = connection.execute(
                "SELECT * FROM round_pods WHERE round_id=? ORDER BY schedule_order",
                (round_id,),
            ).fetchall()
        finally:
            connection.close()
        result = dict(row)
        result["pods"] = [dict(pod_row) for pod_row in pods]
        return result

    def get_round_by_batch(self, batch_digest: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT round_id FROM rounds WHERE batch_digest=?",
                (batch_digest,),
            ).fetchone()
        finally:
            connection.close()
        return self.get_round(row["round_id"]) if row is not None else None

    def create_round(
        self,
        *,
        round_id: str,
        batch_digest: str,
        input_digest: str,
        config: ScheduleConfig,
        pods_in_order: list[Pod],
        versions: dict[str, int],
        start_clock_ms: int,
    ) -> tuple[dict[str, Any], bool]:
        self.initialize()
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM rounds WHERE round_id=? OR batch_digest=? ORDER BY round_number LIMIT 1",
                (round_id, batch_digest),
            ).fetchone()
            if existing is not None:
                if existing["round_id"] != round_id and existing["batch_digest"] != batch_digest:
                    raise ConflictError(
                        "E_ROUND_DIGEST_MISMATCH",
                        "round id belongs to a different batch",
                        {"round_id": round_id},
                    )
                if existing["batch_digest"] != batch_digest:
                    raise ConflictError(
                        "E_ROUND_DIGEST_MISMATCH",
                        "round batch digest does not match persisted state",
                        {
                            "round_id": existing["round_id"],
                            "expected": existing["batch_digest"],
                            "actual": batch_digest,
                        },
                    )
                created = False
            else:
                sequence = self._next_seq(connection)
                round_number_row = connection.execute(
                    "SELECT COALESCE(MAX(round_number), 0) AS n FROM rounds"
                ).fetchone()
                round_number = int(round_number_row["n"]) + 1
                connection.execute(
                    """
                    INSERT INTO rounds(
                        round_id, round_number, batch_digest, status,
                        input_digest, content_digest, config_json, config_digest,
                        initial_versions_json, start_clock_ms, clock_ms,
                        end_clock_ms, budget_ms, report_json, report_digest,
                        created_seq, committed_seq
                    ) VALUES (?, ?, ?, 'RUNNING', ?, '', ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, NULL)
                    """,
                    (
                        round_id,
                        round_number,
                        batch_digest,
                        input_digest,
                        canonical_json(config.to_dict()),
                        digest(config.to_dict()),
                        canonical_json(dict(sorted(versions.items()))),
                        start_clock_ms,
                        start_clock_ms,
                        config.budget_ms,
                        sequence,
                    ),
                )
                for order, pod in enumerate(pods_in_order):
                    connection.execute(
                        """
                        INSERT INTO round_pods(
                            round_id, pod_name, pod_version, schedule_order,
                            priority, workload, spec_json, status, stage,
                            outcome, reason_code, reason, selected_node,
                            started_at_ms, finished_at_ms, chain_json,
                            chain_digest, decision_digest, recovery_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 'PENDING',
                                  NULL, NULL, NULL, NULL, NULL, NULL, NULL,
                                  NULL, NULL, NULL)
                        """,
                        (
                            round_id,
                            pod.name,
                            pod.version,
                            order,
                            pod.priority,
                            pod.workload,
                            canonical_json(pod.to_dict()),
                        ),
                    )
                created = True
                existing = connection.execute(
                    "SELECT * FROM rounds WHERE round_id=?", (round_id,)
                ).fetchone()
            persisted_pods = connection.execute(
                "SELECT * FROM round_pods WHERE round_id=? ORDER BY schedule_order",
                (existing["round_id"],),
            ).fetchall()
        result = dict(existing)
        result["pods"] = [dict(row) for row in persisted_pods]
        return result, created

    def next_clock(self) -> int:
        return self.logical_clock()

    def update_stage(
        self,
        *,
        round_id: str,
        pod_name: str,
        pod_version: str,
        stage: str,
        clock_ms: int,
        chain: dict[str, Any] | None = None,
    ) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT status FROM round_pods
                WHERE round_id=? AND pod_name=? AND pod_version=?
                """,
                (round_id, pod_name, pod_version),
            ).fetchone()
            if row is None:
                raise StorageError(
                    "E_NOT_FOUND",
                    "round pod does not exist",
                    {"pod": pod_name, "round_id": round_id},
                )
            if row["status"] == "TERMINAL":
                return False
            chain_json = canonical_json(chain) if chain is not None else None
            chain_digest = digest(chain) if chain is not None else None
            connection.execute(
                """
                UPDATE round_pods SET
                    status='RUNNING', stage=?,
                    chain_json=COALESCE(?, chain_json),
                    chain_digest=COALESCE(?, chain_digest)
                WHERE round_id=? AND pod_name=? AND pod_version=?
                  AND status!='TERMINAL'
                """,
                (stage, chain_json, chain_digest, round_id, pod_name, pod_version),
            )
            connection.execute(
                "UPDATE rounds SET clock_ms=MAX(clock_ms, ?) WHERE round_id=?",
                (clock_ms, round_id),
            )
            connection.execute(
                """
                UPDATE pods SET phase=?, last_round=?, updated_clock=MAX(updated_clock, ?)
                WHERE name=?
                """,
                (stage, round_id, clock_ms, pod_name),
            )
            return True

    def reset_pod_for_replay(
        self,
        *,
        round_id: str,
        pod_name: str,
        pod_version: str,
        previous_stage: str,
    ) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT status, stage, recovery_json FROM round_pods
                WHERE round_id=? AND pod_name=? AND pod_version=?
                """,
                (round_id, pod_name, pod_version),
            ).fetchone()
            if row is None or row["status"] == "TERMINAL":
                return
            recovery = json.loads(row["recovery_json"]) if row["recovery_json"] else {}
            replayed = list(recovery.get("replayed_after_uncommitted_stage", []))
            if previous_stage not in {"PENDING", "RECOVERED_REPLAY"}:
                replayed.append(previous_stage)
            recovery["replayed_after_uncommitted_stage"] = replayed
            connection.execute(
                """
                UPDATE round_pods SET
                    status='PENDING', stage='RECOVERED_REPLAY',
                    chain_json=NULL, chain_digest=NULL, recovery_json=?
                WHERE round_id=? AND pod_name=? AND pod_version=?
                  AND status!='TERMINAL'
                """,
                (canonical_json(recovery), round_id, pod_name, pod_version),
            )

    def mark_unstarted(
        self,
        *,
        round_id: str,
        pod_name: str,
        pod_version: str,
        clock_ms: int,
        reason_code: str,
        reason: str,
    ) -> bool:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT status FROM round_pods
                WHERE round_id=? AND pod_name=? AND pod_version=?
                """,
                (round_id, pod_name, pod_version),
            ).fetchone()
            if row is None:
                raise StorageError("E_NOT_FOUND", "round pod does not exist")
            if row["status"] == "TERMINAL":
                return False
            binding = connection.execute(
                "SELECT 1 FROM bindings WHERE round_id=? AND pod_name=? AND pod_version=?",
                (round_id, pod_name, pod_version),
            ).fetchone()
            if binding is not None:
                return False
            connection.execute(
                """
                UPDATE round_pods SET
                    status='TERMINAL', stage='UNSTARTED',
                    outcome='BUDGET_UNSTARTED', reason_code=?, reason=?,
                    finished_at_ms=?
                WHERE round_id=? AND pod_name=? AND pod_version=?
                  AND status!='TERMINAL'
                """,
                (reason_code, reason, clock_ms, round_id, pod_name, pod_version),
            )
            connection.execute(
                """
                UPDATE pods SET phase='BUDGET_UNSTARTED', last_round=?,
                    updated_clock=MAX(updated_clock, ?)
                WHERE name=?
                """,
                (round_id, clock_ms, pod_name),
            )
            connection.execute(
                "UPDATE rounds SET clock_ms=MAX(clock_ms, ?) WHERE round_id=?",
                (clock_ms, round_id),
            )
            return True

    def finish_pod(
        self,
        *,
        round_id: str,
        round_input_digest: str,
        pod_name: str,
        pod_version: str,
        outcome: str,
        reason_code: str,
        reason: str,
        clock_ms: int,
        chain: dict[str, Any],
        started_at_ms: int | None,
    ) -> bool:
        chain_digest = digest(chain)
        with self._transaction() as connection:
            round_row = connection.execute(
                "SELECT input_digest, status FROM rounds WHERE round_id=?",
                (round_id,),
            ).fetchone()
            if round_row is None:
                raise StorageError("E_NOT_FOUND", "round does not exist")
            if round_row["input_digest"] != round_input_digest:
                raise ConflictError(
                    "E_ROUND_DIGEST_MISMATCH",
                    "round input digest does not match persisted state",
                    {
                        "expected": round_row["input_digest"],
                        "actual": round_input_digest,
                    },
                )
            row = connection.execute(
                """
                SELECT status FROM round_pods
                WHERE round_id=? AND pod_name=? AND pod_version=?
                """,
                (round_id, pod_name, pod_version),
            ).fetchone()
            if row is None:
                raise StorageError("E_NOT_FOUND", "round pod does not exist")
            binding = connection.execute(
                "SELECT * FROM bindings WHERE round_id=? AND pod_name=? AND pod_version=?",
                (round_id, pod_name, pod_version),
            ).fetchone()
            if binding is not None:
                if outcome == "BOUND":
                    return False
                raise ConflictError(
                    "E_DECISION_CONFLICT",
                    "pod already has a committed binding",
                    {"pod": pod_name},
                )
            if row["status"] == "TERMINAL":
                return False
            connection.execute(
                """
                UPDATE round_pods SET
                    status='TERMINAL', stage=?, outcome=?, reason_code=?,
                    reason=?, started_at_ms=COALESCE(started_at_ms, ?),
                    finished_at_ms=?, chain_json=?, chain_digest=?
                WHERE round_id=? AND pod_name=? AND pod_version=?
                  AND status!='TERMINAL'
                """,
                (
                    outcome,
                    outcome,
                    reason_code,
                    reason,
                    started_at_ms,
                    clock_ms,
                    canonical_json(chain),
                    chain_digest,
                    round_id,
                    pod_name,
                    pod_version,
                ),
            )
            connection.execute(
                """
                UPDATE pods SET phase=?, last_round=?, updated_clock=MAX(updated_clock, ?)
                WHERE name=?
                """,
                (outcome, round_id, clock_ms, pod_name),
            )
            connection.execute(
                "UPDATE rounds SET clock_ms=MAX(clock_ms, ?) WHERE round_id=?",
                (clock_ms, round_id),
            )
            return True

    def commit_binding(
        self,
        *,
        round_id: str,
        round_input_digest: str,
        pod_name: str,
        pod_version: str,
        node_name: str,
        resources: Resources,
        expected_node_version: int,
        topology_domains: dict[str, str],
        clock_ms: int,
        chain: dict[str, Any],
        started_at_ms: int | None,
    ) -> dict[str, Any]:
        chain_digest = digest(chain)
        with self._transaction() as connection:
            round_row = connection.execute(
                "SELECT * FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone()
            if round_row is None:
                raise StorageError("E_NOT_FOUND", "round does not exist")
            if round_row["input_digest"] != round_input_digest:
                raise ConflictError(
                    "E_ROUND_DIGEST_MISMATCH",
                    "round input digest does not match persisted state",
                    {
                        "expected": round_row["input_digest"],
                        "actual": round_input_digest,
                    },
                )

            existing_binding = connection.execute(
                """
                SELECT * FROM bindings
                WHERE round_id=? AND pod_name=? AND pod_version=?
                """,
                (round_id, pod_name, pod_version),
            ).fetchone()
            if existing_binding is not None:
                if (
                    existing_binding["node_name"] == node_name
                    and existing_binding["cpu_millicores"] == resources.cpu_millicores
                    and existing_binding["memory_mib"] == resources.memory_mib
                    and existing_binding["gpu"] == resources.gpu
                ):
                    candidate_digest = digest(
                        {
                            "pod": pod_name,
                            "pod_version": pod_version,
                            "node": node_name,
                            "resources": resources.to_dict(),
                            "resource_version_before": existing_binding[
                                "resource_version_before"
                            ],
                            "resource_version_after": existing_binding[
                                "resource_version_after"
                            ],
                            "topology_delta": json.loads(
                                existing_binding["topology_delta_json"]
                            ),
                            "chain_digest": chain_digest,
                            "clock_ms": existing_binding["committed_clock_ms"],
                        }
                    )
                    if candidate_digest != existing_binding["decision_digest"]:
                        raise ConflictError(
                            "E_IDEMPOTENCY_CONFLICT",
                            "the same binding key has a different decision chain",
                            {"pod": pod_name, "round_id": round_id},
                        )
                    return {
                        "idempotent": True,
                        "binding_id": existing_binding["binding_id"],
                        "binding": dict(existing_binding),
                        "decision_digest": existing_binding["decision_digest"],
                    }
                raise ConflictError(
                    "E_IDEMPOTENCY_CONFLICT",
                    "the same pod/version is already bound with a different payload",
                    {"pod": pod_name, "round_id": round_id},
                )

            pod_row = connection.execute(
                """
                SELECT * FROM round_pods
                WHERE round_id=? AND pod_name=? AND pod_version=?
                """,
                (round_id, pod_name, pod_version),
            ).fetchone()
            if pod_row is None:
                raise StorageError("E_NOT_FOUND", "round pod does not exist")
            pod_spec = json.loads(pod_row["spec_json"])
            requested = pod_spec["resources"]
            if (
                int(requested["cpu_millicores"]) != resources.cpu_millicores
                or int(requested["memory_mib"]) != resources.memory_mib
                or int(requested["gpu"]) != resources.gpu
            ):
                raise ConflictError(
                    "E_BINDING_RESOURCE_MISMATCH",
                    "binding reservation does not equal the pod request",
                    {
                        "pod": pod_name,
                        "expected": requested,
                        "actual": resources.to_dict(),
                    },
                )
            if pod_row["status"] == "TERMINAL":
                raise ConflictError(
                    "E_DECISION_CONFLICT",
                    "pod already reached a terminal non-binding decision",
                    {"pod": pod_name, "outcome": pod_row["outcome"]},
                )
            if round_row["status"] != "RUNNING":
                raise ConflictError(
                    "E_ROUND_NOT_RUNNING",
                    "round is not accepting bindings",
                    {"status": round_row["status"]},
                )

            earlier = connection.execute(
                """
                SELECT pod_name FROM round_pods
                WHERE round_id=? AND schedule_order < ? AND status!='TERMINAL'
                ORDER BY schedule_order LIMIT 1
                """,
                (round_id, pod_row["schedule_order"]),
            ).fetchone()
            if earlier is not None:
                raise ConflictError(
                    "E_ORDER_BLOCKED",
                    "an earlier pod has not reached a terminal decision",
                    {"blocking_pod": earlier["pod_name"]},
                )

            node_row = connection.execute(
                "SELECT * FROM nodes WHERE name=?", (node_name,)
            ).fetchone()
            if node_row is None:
                raise StorageError("E_NOT_FOUND", "node does not exist", {"node": node_name})
            actual_version = int(node_row["resource_version"])
            if actual_version != expected_node_version:
                raise ConflictError(
                    "E_STALE_RESOURCE_VERSION",
                    "node resource snapshot is stale",
                    {
                        "node": node_name,
                        "expected": expected_node_version,
                        "actual": actual_version,
                    },
                )
            if resources.cpu_millicores > node_row["remaining_cpu"]:
                raise ConflictError(
                    "E_INSUFFICIENT_CPU",
                    "node no longer has enough CPU",
                    {"node": node_name},
                )
            if resources.memory_mib > node_row["remaining_memory"]:
                raise ConflictError(
                    "E_INSUFFICIENT_MEMORY",
                    "node no longer has enough memory",
                    {"node": node_name},
                )
            if resources.gpu > node_row["remaining_gpu"]:
                raise ConflictError(
                    "E_INSUFFICIENT_GPU",
                    "node no longer has enough GPU",
                    {"node": node_name},
                )

            labels = json.loads(node_row["labels_json"])
            pod = parse_pod(pod_spec, 0)
            all_node_rows = connection.execute(
                "SELECT name, labels_json FROM nodes ORDER BY name"
            ).fetchall()
            node_labels = {
                row["name"]: json.loads(row["labels_json"]) for row in all_node_rows
            }
            committed_rows = connection.execute(
                """
                SELECT b.pod_name, b.node_name, p.labels_json
                FROM bindings b
                JOIN pods p ON p.name=b.pod_name
                ORDER BY b.binding_id
                """
            ).fetchall()
            for anti_rule in pod.anti_affinity:
                candidate_domain = labels.get(anti_rule.topology_key)
                if candidate_domain is None:
                    raise ConflictError(
                        "E_ANTI_AFFINITY_CONFLICT",
                        "candidate node lacks an anti-affinity topology key",
                        {
                            "node": node_name,
                            "topology_key": anti_rule.topology_key,
                        },
                    )
                for committed in committed_rows:
                    if committed["pod_name"] == pod.name:
                        continue
                    committed_labels = json.loads(committed["labels_json"])
                    if not labels_match(
                        committed_labels,
                        anti_rule.match_labels,
                        anti_rule.match_expressions,
                    ):
                        continue
                    committed_node_labels = node_labels.get(committed["node_name"], {})
                    if (
                        committed_node_labels.get(anti_rule.topology_key)
                        == candidate_domain
                    ):
                        raise ConflictError(
                            "E_ANTI_AFFINITY_CONFLICT",
                            "a concurrent binding now violates pod anti-affinity",
                            {
                                "node": node_name,
                                "topology_key": anti_rule.topology_key,
                                "conflicting_pod": committed["pod_name"],
                            },
                        )
            for constraint in pod.topology_spread:
                if constraint.when_unsatisfiable != "DoNotSchedule":
                    continue
                candidate_domain = labels.get(constraint.topology_key)
                if candidate_domain is None:
                    raise ConflictError(
                        "E_TOPOLOGY_SKEW",
                        "candidate node lacks a topology spread key",
                        {
                            "node": node_name,
                            "topology_key": constraint.topology_key,
                        },
                    )
                domains = sorted(
                    {
                        node_label[constraint.topology_key]
                        for node_label in node_labels.values()
                        if constraint.topology_key in node_label
                    }
                )
                if candidate_domain not in domains:
                    raise ConflictError(
                        "E_TOPOLOGY_SKEW",
                        "candidate domain is not eligible",
                        {
                            "node": node_name,
                            "domain": candidate_domain,
                        },
                    )
                if len(domains) < constraint.min_domains:
                    raise ConflictError(
                        "E_TOPOLOGY_SKEW",
                        "not enough topology domains are available",
                        {
                            "domains": len(domains),
                            "min_domains": constraint.min_domains,
                        },
                    )
                counts = {domain: 0 for domain in domains}
                for committed in committed_rows:
                    if committed["pod_name"] == pod.name:
                        continue
                    committed_labels = json.loads(committed["labels_json"])
                    if not labels_match(
                        committed_labels,
                        constraint.match_labels,
                        constraint.match_expressions,
                    ):
                        continue
                    committed_node_labels = node_labels.get(committed["node_name"], {})
                    domain = committed_node_labels.get(constraint.topology_key)
                    if domain in counts:
                        counts[domain] += 1
                counts[candidate_domain] += 1
                skew = max(counts.values()) - min(counts.values())
                if skew > constraint.max_skew:
                    raise ConflictError(
                        "E_TOPOLOGY_SKEW",
                        "a concurrent binding now exceeds topology max skew",
                        {
                            "node": node_name,
                            "topology_key": constraint.topology_key,
                            "skew": skew,
                            "max_skew": constraint.max_skew,
                        },
                    )
            delta: dict[str, dict[str, int]] = {}
            for topology_key, domain in sorted(topology_domains.items()):
                if labels.get(topology_key) != domain:
                    raise ConflictError(
                        "E_TOPOLOGY_MISMATCH",
                        "topology domain does not match node labels",
                        {
                            "node": node_name,
                            "topology_key": topology_key,
                            "domain": domain,
                        },
                    )
                delta[topology_key] = {"domain": domain, "delta": 1}

            before_version = actual_version
            after_version = before_version + 1
            new_remaining_cpu = int(node_row["remaining_cpu"]) - resources.cpu_millicores
            new_remaining_memory = int(node_row["remaining_memory"]) - resources.memory_mib
            new_remaining_gpu = int(node_row["remaining_gpu"]) - resources.gpu
            connection.execute(
                """
                UPDATE nodes SET
                    remaining_cpu=?, remaining_memory=?, remaining_gpu=?,
                    resource_version=?, updated_clock=MAX(updated_clock, ?)
                WHERE name=? AND resource_version=?
                """,
                (
                    new_remaining_cpu,
                    new_remaining_memory,
                    new_remaining_gpu,
                    after_version,
                    clock_ms,
                    node_name,
                    before_version,
                ),
            )
            for topology_key, topology_value in delta.items():
                domain = topology_value["domain"]
                connection.execute(
                    """
                    INSERT INTO topology_domains(topology_key, domain, pod_count, version)
                    VALUES (?, ?, 1, 1)
                    ON CONFLICT(topology_key, domain) DO UPDATE SET
                        pod_count=topology_domains.pod_count + 1,
                        version=topology_domains.version + 1
                    """,
                    (topology_key, domain),
                )

            decision_payload = {
                "pod": pod_name,
                "pod_version": pod_version,
                "node": node_name,
                "resources": resources.to_dict(),
                "resource_version_before": before_version,
                "resource_version_after": after_version,
                "topology_delta": delta,
                "chain_digest": chain_digest,
                "clock_ms": clock_ms,
            }
            decision_digest = digest(decision_payload)
            cursor = connection.execute(
                """
                INSERT INTO bindings(
                    round_id, pod_name, pod_version, node_name,
                    cpu_millicores, memory_mib, gpu,
                    resource_version_before, resource_version_after,
                    topology_delta_json, decision_digest, committed_clock_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    round_id,
                    pod_name,
                    pod_version,
                    node_name,
                    resources.cpu_millicores,
                    resources.memory_mib,
                    resources.gpu,
                    before_version,
                    after_version,
                    canonical_json(delta),
                    decision_digest,
                    clock_ms,
                ),
            )
            binding_id = cursor.lastrowid
            connection.execute(
                """
                UPDATE round_pods SET
                    status='TERMINAL', stage='BOUND', outcome='BOUND',
                    reason_code=NULL, reason=NULL, selected_node=?,
                    started_at_ms=COALESCE(started_at_ms, ?),
                    finished_at_ms=?, chain_json=?, chain_digest=?,
                    decision_digest=?
                WHERE round_id=? AND pod_name=? AND pod_version=?
                  AND status!='TERMINAL'
                """,
                (
                    node_name,
                    started_at_ms,
                    clock_ms,
                    canonical_json(chain),
                    chain_digest,
                    decision_digest,
                    round_id,
                    pod_name,
                    pod_version,
                ),
            )
            connection.execute(
                """
                UPDATE pods SET phase='BOUND', last_round=?,
                    updated_clock=MAX(updated_clock, ?)
                WHERE name=?
                """,
                (round_id, clock_ms, pod_name),
            )
            connection.execute(
                "UPDATE rounds SET clock_ms=MAX(clock_ms, ?) WHERE round_id=?",
                (clock_ms, round_id),
            )
            return {
                "idempotent": False,
                "binding_id": binding_id,
                "node": node_name,
                "resource_version_before": before_version,
                "resource_version_after": after_version,
                "remaining": {
                    "cpu_millicores": new_remaining_cpu,
                    "memory_mib": new_remaining_memory,
                    "gpu": new_remaining_gpu,
                },
                "decision_digest": decision_digest,
            }

    def finalize_round(
        self,
        *,
        round_id: str,
        expected_input_digest: str,
        expected_ledger_digest: str,
        report: dict[str, Any],
        end_clock_ms: int,
    ) -> dict[str, Any]:
        with self._transaction() as connection:
            round_row = connection.execute(
                "SELECT * FROM rounds WHERE round_id=?", (round_id,)
            ).fetchone()
            if round_row is None:
                raise StorageError("E_NOT_FOUND", "round does not exist")
            if round_row["input_digest"] != expected_input_digest:
                raise ConflictError(
                    "E_ROUND_DIGEST_MISMATCH",
                    "round input digest changed before finalization",
                    {
                        "expected": round_row["input_digest"],
                        "actual": expected_input_digest,
                    },
                )
            if round_row["status"] == "COMMITTED":
                return json.loads(round_row["report_json"])
            if round_row["status"] != "RUNNING":
                raise ConflictError(
                    "E_ROUND_NOT_RUNNING",
                    "round cannot be finalized",
                    {"status": round_row["status"]},
                )
            incomplete = connection.execute(
                """
                SELECT pod_name, status, stage FROM round_pods
                WHERE round_id=? AND status!='TERMINAL'
                ORDER BY schedule_order LIMIT 1
                """,
                (round_id,),
            ).fetchone()
            if incomplete is not None:
                raise ConflictError(
                    "E_ROUND_INCOMPLETE",
                    "round has unfinished pods",
                    {
                        "pod": incomplete["pod_name"],
                        "status": incomplete["status"],
                        "stage": incomplete["stage"],
                    },
                )
            ledger_rows = connection.execute(
                """
                SELECT pod_name, pod_version, outcome, selected_node,
                       reason_code, reason, chain_digest, decision_digest
                FROM round_pods WHERE round_id=? ORDER BY schedule_order
                """,
                (round_id,),
            ).fetchall()
            ledger = [dict(row) for row in ledger_rows]
            actual_ledger_digest = digest(ledger)
            if actual_ledger_digest != expected_ledger_digest:
                raise ConflictError(
                    "E_ROUND_DIGEST_MISMATCH",
                    "persisted decision ledger does not match the round report",
                    {
                        "expected": expected_ledger_digest,
                        "actual": actual_ledger_digest,
                    },
                )
            report_pods = {
                item.get("pod"): item for item in report.get("pods", [])
            }
            if len(report_pods) != len(ledger) or set(report_pods) != {
                item["pod_name"] for item in ledger
            }:
                raise ConflictError(
                    "E_ROUND_DIGEST_MISMATCH",
                    "report pod set does not match the persisted decision ledger",
                )
            chain_by_pod = {
                row["pod_name"]: row["chain_json"]
                for row in connection.execute(
                    "SELECT pod_name, chain_json FROM round_pods WHERE round_id=?",
                    (round_id,),
                ).fetchall()
            }
            for ledger_row in ledger_rows:
                report_pod = report_pods[ledger_row["pod_name"]]
                chain_json = chain_by_pod.get(ledger_row["pod_name"])
                chain = json.loads(chain_json) if chain_json else {}
                decision_mismatch = (
                    report_pod.get("outcome") != ledger_row["outcome"]
                    or report_pod.get("selected_node") != ledger_row["selected_node"]
                    or report_pod.get("reason_code") != ledger_row["reason_code"]
                    or report_pod.get("chain_digest") != ledger_row["chain_digest"]
                    or report_pod.get("decision_digest")
                    != ledger_row["decision_digest"]
                )
                if chain_json:
                    decision_mismatch = decision_mismatch or (
                        report_pod.get("filters", []) != chain.get("filters", [])
                        or report_pod.get("scoring", {}) != chain.get("scoring", {})
                    )
                if decision_mismatch:
                    raise ConflictError(
                        "E_ROUND_DIGEST_MISMATCH",
                        "report decision does not match the persisted decision ledger",
                        {"pod": ledger_row["pod_name"]},
                    )
            if report.get("decision_ledger_digest") != actual_ledger_digest:
                raise ConflictError(
                    "E_ROUND_DIGEST_MISMATCH",
                    "report ledger digest does not match persisted decisions",
                )

            node_rows = connection.execute("SELECT * FROM nodes ORDER BY name").fetchall()
            topology_rows = connection.execute(
                "SELECT * FROM topology_domains ORDER BY topology_key, domain"
            ).fetchall()
            snapshot = {
                "round_id": round_id,
                "round_number": round_row["round_number"],
                "logical_clock_ms": end_clock_ms,
                "nodes": [
                    {
                        "name": row["name"],
                        "remaining": {
                            "cpu_millicores": row["remaining_cpu"],
                            "memory_mib": row["remaining_memory"],
                            "gpu": row["remaining_gpu"],
                        },
                        "resource_version": row["resource_version"],
                    }
                    for row in node_rows
                ],
                "topology_domains": [
                    {
                        "topology_key": row["topology_key"],
                        "domain": row["domain"],
                        "pod_count": row["pod_count"],
                        "version": row["version"],
                    }
                    for row in topology_rows
                ],
            }
            snapshot_json = canonical_json(snapshot)
            snapshot_digest = digest(snapshot)
            full_report = dict(report)
            content_body = {
                key: value for key, value in full_report.items() if key != "content_digest"
            }
            content_digest = digest(content_body)
            full_report["content_digest"] = content_digest
            report_json = pretty_json(full_report)
            sequence = self._next_seq(connection)
            connection.execute(
                """
                UPDATE rounds SET
                    status='COMMITTED', content_digest=?, clock_ms=?,
                    end_clock_ms=?, report_json=?, report_digest=?,
                    committed_seq=?
                WHERE round_id=?
                """,
                (
                    content_digest,
                    end_clock_ms,
                    end_clock_ms,
                    report_json,
                    content_digest,
                    sequence,
                    round_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO round_snapshots(
                    round_id, round_number, snapshot_json,
                    snapshot_digest, logical_clock_ms, created_seq
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    round_id,
                    round_row["round_number"],
                    snapshot_json,
                    snapshot_digest,
                    end_clock_ms,
                    sequence,
                ),
            )
            watermark = max(
                self._meta_int(connection, "logical_clock_ms"),
                end_clock_ms,
            )
            self._set_meta_int(connection, "logical_clock_ms", watermark)
            return full_report

    def get_report(self, round_id: str) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT status, report_json FROM rounds WHERE round_id=?",
                (round_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or row["status"] != "COMMITTED" or not row["report_json"]:
            return None
        return json.loads(row["report_json"])

    def latest_committed_report(self) -> dict[str, Any] | None:
        if not self.db_path.exists():
            return None
        connection = self.connect()
        try:
            row = connection.execute(
                """
                SELECT report_json FROM rounds
                WHERE status='COMMITTED' AND report_json IS NOT NULL
                ORDER BY round_number DESC LIMIT 1
                """
            ).fetchone()
        finally:
            connection.close()
        return json.loads(row["report_json"]) if row is not None else None

    def list_running_rounds(self) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT round_id FROM rounds WHERE status='RUNNING' ORDER BY round_number"
            ).fetchall()
        finally:
            connection.close()
        return [self.get_round(row["round_id"]) for row in rows]

    def status(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "nodes": [],
                "topology_domains": [],
                "logical_clock_ms": 0,
                "rounds": [],
                "snapshot_round_id": None,
            }
        connection = self.connect()
        try:
            snapshot_row = connection.execute(
                """
                SELECT snapshot_json, logical_clock_ms, round_id
                FROM round_snapshots ORDER BY round_number DESC LIMIT 1
                """
            ).fetchone()
            rounds = connection.execute(
                """
                SELECT round_id, round_number, status, input_digest,
                       content_digest, start_clock_ms, end_clock_ms, budget_ms
                FROM rounds ORDER BY round_number
                """
            ).fetchall()
            watermark = self._meta_int(connection, "logical_clock_ms")
            if snapshot_row is None:
                node_rows = connection.execute(
                    "SELECT * FROM nodes ORDER BY name"
                ).fetchall()
                snapshot = {
                    "nodes": [
                        {
                            "name": row["name"],
                            "remaining": {
                                "cpu_millicores": row["capacity_cpu"],
                                "memory_mib": row["capacity_memory"],
                                "gpu": row["capacity_gpu"],
                            },
                            "resource_version": 0,
                        }
                        for row in node_rows
                    ],
                    "topology_domains": [],
                }
                snapshot_round_id = None
            else:
                snapshot = json.loads(snapshot_row["snapshot_json"])
                snapshot_round_id = snapshot_row["round_id"]
        finally:
            connection.close()
        return {
            "nodes": snapshot["nodes"],
            "topology_domains": snapshot["topology_domains"],
            "logical_clock_ms": watermark,
            "snapshot_round_id": snapshot_round_id,
            "rounds": [dict(row) for row in rounds],
        }

    def explain(self, pod_name: str, round_id: str | None = None) -> dict[str, Any]:
        if round_id is not None:
            report = self.get_report(round_id)
        else:
            report = self.latest_committed_report()
        if report is None:
            raise StorageError("E_NOT_FOUND", "no committed round report is available")
        for pod in report.get("pods", []):
            if pod.get("pod") == pod_name:
                return {
                    "round_id": report["round_id"],
                    "round_number": report["round_number"],
                    "logical_clock": report["logical_clock"],
                    "pod": pod,
                }
        raise StorageError(
            "E_NOT_FOUND",
            "pod is not present in the selected round report",
            {"pod": pod_name, "round_id": report["round_id"]},
        )

    def verify(self, report_path: str | None = None) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        def check(name: str, ok: bool, detail: Any = None) -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})
            if not ok:
                errors.append({"check": name, "detail": detail})

        if not self.db_path.exists():
            return {
                "ok": False,
                "checks": [],
                "errors": [{"check": "database_exists", "detail": "database missing"}],
            }
        connection = self.connect()
        try:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            check("sqlite_integrity", integrity == "ok", integrity)
            node_rows = connection.execute("SELECT * FROM nodes ORDER BY name").fetchall()
            binding_rows = connection.execute(
                "SELECT * FROM bindings ORDER BY binding_id"
            ).fetchall()
            pod_rows = connection.execute("SELECT * FROM pods ORDER BY name").fetchall()
            round_rows = connection.execute(
                "SELECT * FROM rounds ORDER BY round_number"
            ).fetchall()
            snapshot_rows = connection.execute(
                "SELECT * FROM round_snapshots ORDER BY round_number"
            ).fetchall()
            topology_rows = connection.execute(
                "SELECT * FROM topology_domains ORDER BY topology_key, domain"
            ).fetchall()
            pods_by_name = {row["name"]: row for row in pod_rows}
            bindings_by_node: dict[str, list[sqlite3.Row]] = {}
            for row in binding_rows:
                bindings_by_node.setdefault(row["node_name"], []).append(row)

            node_ok = True
            node_details: list[dict[str, Any]] = []
            for row in node_rows:
                rows = bindings_by_node.get(row["name"], [])
                expected_cpu = int(row["capacity_cpu"]) - sum(
                    int(binding["cpu_millicores"]) for binding in rows
                )
                expected_memory = int(row["capacity_memory"]) - sum(
                    int(binding["memory_mib"]) for binding in rows
                )
                expected_gpu = int(row["capacity_gpu"]) - sum(
                    int(binding["gpu"]) for binding in rows
                )
                expected_version = len(rows)
                ok = (
                    expected_cpu == int(row["remaining_cpu"])
                    and expected_memory == int(row["remaining_memory"])
                    and expected_gpu == int(row["remaining_gpu"])
                    and expected_version == int(row["resource_version"])
                )
                node_ok = node_ok and ok
                node_details.append(
                    {
                        "node": row["name"],
                        "ok": ok,
                        "remaining": {
                            "cpu_millicores": row["remaining_cpu"],
                            "memory_mib": row["remaining_memory"],
                            "gpu": row["remaining_gpu"],
                        },
                        "resource_version": row["resource_version"],
                    }
                )
            check("node_resource_conservation", node_ok, node_details)

            binding_ok = True
            binding_details: list[dict[str, Any]] = []
            for row in binding_rows:
                pod_row = pods_by_name.get(row["pod_name"])
                if pod_row is None:
                    binding_ok = False
                    binding_details.append({"binding_id": row["binding_id"], "error": "pod missing"})
                    continue
                spec = json.loads(pod_row["spec_json"])
                resources = spec["resources"]
                ok = (
                    int(resources["cpu_millicores"]) == int(row["cpu_millicores"])
                    and int(resources["memory_mib"]) == int(row["memory_mib"])
                    and int(resources["gpu"]) == int(row["gpu"])
                )
                binding_ok = binding_ok and ok
                if not ok:
                    binding_details.append(
                        {
                            "binding_id": row["binding_id"],
                            "pod": row["pod_name"],
                            "error": "binding resources differ from pod request",
                        }
                    )
            check("binding_matches_pod_request", binding_ok, binding_details)

            expected_topology: dict[tuple[str, str], int] = {}
            for row in binding_rows:
                pod_row = pods_by_name.get(row["pod_name"])
                node_row = next(
                    (node for node in node_rows if node["name"] == row["node_name"]),
                    None,
                )
                if pod_row is None or node_row is None:
                    continue
                labels = json.loads(node_row["labels_json"])
                spec = json.loads(pod_row["spec_json"])
                topology_keys = {
                    item["topology_key"] for item in spec.get("topology_spread", [])
                }
                topology_keys.update(
                    item["topology_key"] for item in spec.get("anti_affinity", [])
                )
                topology_keys.update(topology_domain_keys(labels))
                for key in topology_keys:
                    domain = labels.get(key)
                    if domain is not None:
                        expected_topology[(key, domain)] = (
                            expected_topology.get((key, domain), 0) + 1
                        )
            actual_topology = {
                (row["topology_key"], row["domain"]): int(row["pod_count"])
                for row in topology_rows
                if int(row["pod_count"]) != 0
            }
            check(
                "topology_counts",
                actual_topology == expected_topology,
                {
                    "expected": [
                        {"topology_key": key, "domain": domain, "pod_count": count}
                        for (key, domain), count in sorted(expected_topology.items())
                    ],
                    "actual": [
                        {"topology_key": key, "domain": domain, "pod_count": count}
                        for (key, domain), count in sorted(actual_topology.items())
                    ],
                },
            )

            round_ok = True
            round_details: list[dict[str, Any]] = []
            for row in round_rows:
                if row["status"] != "COMMITTED":
                    continue
                if not row["report_json"] or not row["report_digest"]:
                    round_ok = False
                    round_details.append({"round_id": row["round_id"], "error": "missing report"})
                    continue
                report = json.loads(row["report_json"])
                body = {key: value for key, value in report.items() if key != "content_digest"}
                actual_digest = digest(body)
                declared = report.get("content_digest")
                ok = actual_digest == declared == row["report_digest"]
                pod_rows_for_round = connection.execute(
                    """
                    SELECT * FROM round_pods WHERE round_id=?
                    ORDER BY schedule_order
                    """,
                    (row["round_id"],),
                ).fetchall()
                ok = ok and all(item["status"] == "TERMINAL" for item in pod_rows_for_round)
                round_ok = round_ok and ok
                round_details.append(
                    {
                        "round_id": row["round_id"],
                        "ok": ok,
                        "not_terminal": [
                            item["pod_name"]
                            for item in pod_rows_for_round
                            if item["status"] != "TERMINAL"
                        ],
                    }
                )
            check("committed_round_reports", round_ok, round_details)

            snapshot_ok = True
            snapshot_details: list[dict[str, Any]] = []
            for row in snapshot_rows:
                snapshot = json.loads(row["snapshot_json"])
                actual_digest = digest(snapshot)
                prefix_bindings = connection.execute(
                    """
                    SELECT b.* FROM bindings b
                    JOIN rounds r ON r.round_id=b.round_id
                    WHERE r.round_number <= ?
                    ORDER BY b.binding_id
                    """,
                    (row["round_number"],),
                ).fetchall()
                by_node: dict[str, list[sqlite3.Row]] = {}
                for binding in prefix_bindings:
                    by_node.setdefault(binding["node_name"], []).append(binding)
                snapshot_nodes = {item["name"]: item for item in snapshot["nodes"]}
                ok = (
                    actual_digest == row["snapshot_digest"]
                    and int(row["logical_clock_ms"]) == int(snapshot["logical_clock_ms"])
                )
                for node in node_rows:
                    item = snapshot_nodes.get(node["name"])
                    rows = by_node.get(node["name"], [])
                    if item is None:
                        ok = False
                        continue
                    expected = {
                        "cpu_millicores": int(node["capacity_cpu"])
                        - sum(int(binding["cpu_millicores"]) for binding in rows),
                        "memory_mib": int(node["capacity_memory"])
                        - sum(int(binding["memory_mib"]) for binding in rows),
                        "gpu": int(node["capacity_gpu"])
                        - sum(int(binding["gpu"]) for binding in rows),
                    }
                    ok = (
                        ok
                        and item["remaining"] == expected
                        and int(item["resource_version"]) == len(rows)
                    )
                expected_snapshot_topology: dict[tuple[str, str], int] = {}
                for binding in prefix_bindings:
                    pod_row = pods_by_name.get(binding["pod_name"])
                    node_row = next(
                        (
                            node
                            for node in node_rows
                            if node["name"] == binding["node_name"]
                        ),
                        None,
                    )
                    if pod_row is None or node_row is None:
                        continue
                    labels = json.loads(node_row["labels_json"])
                    spec = json.loads(pod_row["spec_json"])
                    topology_keys = {
                        entry["topology_key"]
                        for entry in spec.get("topology_spread", [])
                    }
                    topology_keys.update(
                        entry["topology_key"]
                        for entry in spec.get("anti_affinity", [])
                    )
                    topology_keys.update(topology_domain_keys(labels))
                    for key in topology_keys:
                        domain = labels.get(key)
                        if domain is not None:
                            expected_snapshot_topology[(key, domain)] = (
                                expected_snapshot_topology.get((key, domain), 0) + 1
                            )
                actual_snapshot_topology = {
                    (item["topology_key"], item["domain"]): int(item["pod_count"])
                    for item in snapshot.get("topology_domains", [])
                    if int(item["pod_count"]) != 0
                }
                ok = ok and actual_snapshot_topology == expected_snapshot_topology
                snapshot_ok = snapshot_ok and ok
                snapshot_details.append(
                    {"round_id": row["round_id"], "ok": ok}
                )
            check("round_snapshots", snapshot_ok, snapshot_details)

            orphan_checks = connection.execute(
                """
                SELECT COUNT(*) AS n FROM round_pods rp
                LEFT JOIN bindings b
                  ON b.round_id=rp.round_id
                 AND b.pod_name=rp.pod_name
                 AND b.pod_version=rp.pod_version
                WHERE rp.outcome='BOUND' AND b.binding_id IS NULL
                """
            ).fetchone()["n"]
            check("bound_decisions_have_bindings", int(orphan_checks) == 0, orphan_checks)
        finally:
            connection.close()

        if report_path is not None:
            try:
                supplied = json.loads(Path(report_path).read_text(encoding="utf-8"))
                supplied_digest = digest(
                    {key: value for key, value in supplied.items() if key != "content_digest"}
                )
                expected = self.get_report(supplied.get("round_id", ""))
                report_ok = expected is not None and canonical_json(expected) == canonical_json(
                    supplied
                )
                check(
                    "decision_report_matches_database",
                    report_ok,
                    {
                        "report_digest": supplied_digest,
                        "round_id": supplied.get("round_id"),
                    },
                )
            except (OSError, json.JSONDecodeError) as exc:
                check("decision_report_matches_database", False, str(exc))

        return {"ok": not errors, "checks": checks, "errors": errors}


def _node_state_from_row(row: sqlite3.Row) -> NodeState:
    from .models import Taint

    return NodeState(
        name=row["name"],
        capacity=Resources(
            int(row["capacity_cpu"]),
            int(row["capacity_memory"]),
            int(row["capacity_gpu"]),
        ),
        remaining=Resources(
            int(row["remaining_cpu"]),
            int(row["remaining_memory"]),
            int(row["remaining_gpu"]),
        ),
        resource_version=int(row["resource_version"]),
        labels=json.loads(row["labels_json"]),
        taints=tuple(
            Taint(item["key"], item["value"], item["effect"])
            for item in json.loads(row["taints_json"])
        ),
    )


def ledger_digest(round_id: str, ledger: list[dict[str, Any]]) -> str:
    return digest(ledger)
