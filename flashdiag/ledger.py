"""SQLite snapshot ledger with atomic whole-snapshot activation."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import (
    DiagnosticError,
    E_INPUT_INVALID,
    E_LEDGER_INTEGRITY,
    E_SNAPSHOT_MISSING,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from .hashing import sha256_json


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class NodeRecord:
    node_id: str
    node_type: str
    version: int
    cache_key: str
    content_sha256: str
    artifact_path: str
    dep_hashes: dict[str, Any]

    @property
    def dependency_sha256(self) -> str:
        return sha256_json(self.dep_hashes)

    def checksum_payload(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "version": self.version,
            "cache_key": self.cache_key,
            "content_sha256": self.content_sha256,
            "dependency_sha256": self.dependency_sha256,
        }


@dataclass(frozen=True)
class PlanRecord:
    head: int
    q_block: int
    q_start: int
    q_len: int
    kv_blocks: tuple[dict[str, int], ...]
    plan_sha256: str

    def checksum_payload(self) -> dict[str, Any]:
        return {
            "head": self.head,
            "q_block": self.q_block,
            "q_start": self.q_start,
            "q_len": self.q_len,
            "kv_blocks": [dict(item) for item in self.kv_blocks],
            "plan_sha256": self.plan_sha256,
        }


@dataclass(frozen=True)
class SnapshotRecord:
    case_id: str
    version: int
    weight_revision: int
    input_sha256: str
    output_sha256: str
    output_path: str
    previous_snapshot_id: int | None
    nodes: tuple[NodeRecord, ...]
    plans: tuple[PlanRecord, ...]
    details: dict[str, Any]

    @property
    def dependency_sha256(self) -> str:
        return sha256_json(
            {
                "input_sha256": self.input_sha256,
                "previous_snapshot_id": self.previous_snapshot_id,
                "node_dependencies": {
                    node.node_id: node.dependency_sha256
                    for node in sorted(self.nodes, key=lambda item: item.node_id)
                },
            }
        )

    @property
    def snapshot_sha256(self) -> str:
        return sha256_json(
            {
                "case_id": self.case_id,
                "version": self.version,
                "weight_revision": self.weight_revision,
                "input_sha256": self.input_sha256,
                "output_sha256": self.output_sha256,
                "previous_snapshot_id": self.previous_snapshot_id,
                "dependency_sha256": self.dependency_sha256,
                "plans": [
                    plan.checksum_payload()
                    for plan in sorted(
                        self.plans,
                        key=lambda item: (item.head, item.q_block),
                    )
                ],
                "nodes": [
                    node.checksum_payload()
                    for node in sorted(self.nodes, key=lambda item: item.node_id)
                ],
            }
        )


@dataclass(frozen=True)
class LedgerSnapshot:
    snapshot_id: int
    case_id: str
    version: int
    weight_revision: int
    input_sha256: str
    snapshot_sha256: str
    dependency_sha256: str
    output_sha256: str
    output_path: str
    created_at: str
    is_active: bool
    previous_snapshot_id: int | None
    details: dict[str, Any]
    nodes: tuple[dict[str, Any], ...] = ()
    plans: tuple[dict[str, Any], ...] = ()

    def to_dict(self, include_children: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "snapshot_id": self.snapshot_id,
            "case_id": self.case_id,
            "version": self.version,
            "weight_revision": self.weight_revision,
            "input_sha256": self.input_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "dependency_sha256": self.dependency_sha256,
            "output_sha256": self.output_sha256,
            "output_path": self.output_path,
            "created_at": self.created_at,
            "is_active": self.is_active,
            "previous_snapshot_id": self.previous_snapshot_id,
            "details": self.details,
        }
        if include_children:
            value["nodes"] = list(self.nodes)
            value["plans"] = list(self.plans)
        return value


class SnapshotLedger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=0.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 0")
        return connection

    def initialize(self) -> None:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS snapshots (
                    snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    weight_revision INTEGER NOT NULL,
                    input_sha256 TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    dependency_sha256 TEXT NOT NULL,
                    output_sha256 TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_active INTEGER NOT NULL CHECK (is_active IN (0, 1)),
                    previous_snapshot_id INTEGER,
                    details_json TEXT NOT NULL,
                    UNIQUE (case_id, version),
                    FOREIGN KEY (previous_snapshot_id) REFERENCES snapshots(snapshot_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS snapshots_one_active
                    ON snapshots(case_id) WHERE is_active = 1;

                CREATE TABLE IF NOT EXISTS ledger_nodes (
                    snapshot_id INTEGER NOT NULL,
                    node_id TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    cache_key TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    dependency_sha256 TEXT NOT NULL,
                    dep_hashes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, node_id),
                    FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS block_plans (
                    snapshot_id INTEGER NOT NULL,
                    case_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    head INTEGER NOT NULL,
                    q_block INTEGER NOT NULL,
                    q_start INTEGER NOT NULL,
                    q_len INTEGER NOT NULL,
                    kv_blocks_json TEXT NOT NULL,
                    plan_sha256 TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, head, q_block),
                    FOREIGN KEY (snapshot_id) REFERENCES snapshots(snapshot_id)
                        ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS snapshots_case_version
                    ON snapshots(case_id, version DESC);
                CREATE INDEX IF NOT EXISTS ledger_nodes_cache_key
                    ON ledger_nodes(cache_key);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
        finally:
            connection.close()

    def _snapshot_from_row(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_children: bool = False,
    ) -> LedgerSnapshot:
        nodes: tuple[dict[str, Any], ...] = ()
        plans: tuple[dict[str, Any], ...] = ()
        if include_children:
            node_rows = connection.execute(
                """
                SELECT node_id, node_type, version, cache_key, content_sha256,
                       artifact_path, dependency_sha256, dep_hashes_json, created_at
                FROM ledger_nodes
                WHERE snapshot_id = ?
                ORDER BY node_id
                """,
                (row["snapshot_id"],),
            ).fetchall()
            nodes = tuple(
                {
                    "node_id": node_row["node_id"],
                    "node_type": node_row["node_type"],
                    "version": node_row["version"],
                    "cache_key": node_row["cache_key"],
                    "content_sha256": node_row["content_sha256"],
                    "artifact_path": node_row["artifact_path"],
                    "dependency_sha256": node_row["dependency_sha256"],
                    "dep_hashes": json.loads(node_row["dep_hashes_json"]),
                    "created_at": node_row["created_at"],
                }
                for node_row in node_rows
            )
            plan_rows = connection.execute(
                """
                SELECT version, head, q_block, q_start, q_len, kv_blocks_json, plan_sha256
                FROM block_plans
                WHERE snapshot_id = ?
                ORDER BY head, q_block
                """,
                (row["snapshot_id"],),
            ).fetchall()
            plans = tuple(
                {
                    "version": plan_row["version"],
                    "head": plan_row["head"],
                    "q_block": plan_row["q_block"],
                    "q_start": plan_row["q_start"],
                    "q_len": plan_row["q_len"],
                    "kv_blocks": json.loads(plan_row["kv_blocks_json"]),
                    "plan_sha256": plan_row["plan_sha256"],
                }
                for plan_row in plan_rows
            )
        return LedgerSnapshot(
            snapshot_id=row["snapshot_id"],
            case_id=row["case_id"],
            version=row["version"],
            weight_revision=row["weight_revision"],
            input_sha256=row["input_sha256"],
            snapshot_sha256=row["snapshot_sha256"],
            dependency_sha256=row["dependency_sha256"],
            output_sha256=row["output_sha256"],
            output_path=row["output_path"],
            created_at=row["created_at"],
            is_active=bool(row["is_active"]),
            previous_snapshot_id=row["previous_snapshot_id"],
            details=json.loads(row["details_json"]),
            nodes=nodes,
            plans=plans,
        )

    def active_snapshot(
        self,
        case_id: str,
        *,
        include_children: bool = False,
    ) -> LedgerSnapshot | None:
        with closing(self._connect()) as connection:
            try:
                row = connection.execute(
                    """
                    SELECT * FROM snapshots
                    WHERE case_id = ? AND is_active = 1
                    """,
                    (case_id,),
                ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return None
                raise
            if row is None:
                return None
            return self._snapshot_from_row(
                connection,
                row,
                include_children=include_children,
            )

    def get_snapshot(
        self,
        case_id: str,
        *,
        version: int | None = None,
        include_children: bool = True,
    ) -> LedgerSnapshot:
        with closing(self._connect()) as connection:
            try:
                if version is None:
                    row = connection.execute(
                        """
                        SELECT * FROM snapshots
                        WHERE case_id = ? AND is_active = 1
                        """,
                        (case_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT * FROM snapshots
                        WHERE case_id = ? AND version = ?
                        """,
                        (case_id, version),
                    ).fetchone()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    row = None
                else:
                    raise
            if row is None:
                raise DiagnosticError(
                    E_SNAPSHOT_MISSING,
                    "snapshot does not exist",
                    case_id=case_id,
                    version=version,
                )
            return self._snapshot_from_row(
                connection,
                row,
                include_children=include_children,
            )

    def history(self, case_id: str) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            try:
                rows = connection.execute(
                    """
                    SELECT snapshot_id, case_id, version, weight_revision, input_sha256,
                           snapshot_sha256, dependency_sha256, output_sha256, output_path,
                           created_at, is_active, previous_snapshot_id, details_json
                    FROM snapshots
                    WHERE case_id = ?
                    ORDER BY version ASC
                    """,
                    (case_id,),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
            return [
                {
                    "snapshot_id": row["snapshot_id"],
                    "case_id": row["case_id"],
                    "version": row["version"],
                    "weight_revision": row["weight_revision"],
                    "input_sha256": row["input_sha256"],
                    "snapshot_sha256": row["snapshot_sha256"],
                    "dependency_sha256": row["dependency_sha256"],
                    "output_sha256": row["output_sha256"],
                    "output_path": row["output_path"],
                    "created_at": row["created_at"],
                    "is_active": bool(row["is_active"]),
                    "previous_snapshot_id": row["previous_snapshot_id"],
                    "details": json.loads(row["details_json"]),
                }
                for row in rows
            ]

    def commit(
        self,
        record: SnapshotRecord,
        *,
        expected_active_snapshot_id: int | None,
    ) -> LedgerSnapshot:
        """Atomically activate a complete snapshot or reject the stale writer."""

        node_ids = [node.node_id for node in record.nodes]
        if not record.nodes or len(node_ids) != len(set(node_ids)):
            raise DiagnosticError(
                E_INPUT_INVALID,
                "snapshot nodes must be non-empty and have unique node ids",
                case_id=record.case_id,
            )
        if any(node.version != record.version for node in record.nodes):
            raise DiagnosticError(
                E_INPUT_INVALID,
                "every node version must equal the snapshot version",
                case_id=record.case_id,
                version=record.version,
            )
        plan_keys = [(plan.head, plan.q_block) for plan in record.plans]
        if len(plan_keys) != len(set(plan_keys)):
            raise DiagnosticError(
                E_INPUT_INVALID,
                "snapshot block plans contain duplicate head/Q-block keys",
                case_id=record.case_id,
            )

        created_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        snapshot_sha = record.snapshot_sha256
        dependency_sha = record.dependency_sha256
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                raise DiagnosticError(
                    E_STAGE_BUSY,
                    "another snapshot commit is in progress",
                    case_id=record.case_id,
                ) from exc

            current_row = connection.execute(
                """
                SELECT * FROM snapshots
                WHERE case_id = ? AND is_active = 1
                """,
                (record.case_id,),
            ).fetchone()
            current_id = current_row["snapshot_id"] if current_row is not None else None
            if current_id != expected_active_snapshot_id:
                raise DiagnosticError(
                    E_STAGE_BUSY,
                    "active snapshot changed while this snapshot was being built",
                    case_id=record.case_id,
                    expected_active_snapshot_id=expected_active_snapshot_id,
                    actual_active_snapshot_id=current_id,
                )

            if current_row is not None:
                current_version = current_row["version"]
                current_revision = current_row["weight_revision"]
                if (
                    record.version < current_version
                    or record.weight_revision < current_revision
                ):
                    raise DiagnosticError(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot rollback is not permitted",
                        case_id=record.case_id,
                        requested_version=record.version,
                        current_version=current_version,
                        requested_weight_revision=record.weight_revision,
                        current_weight_revision=current_revision,
                    )
                if record.version == current_version:
                    if record.snapshot_sha256 != current_row["snapshot_sha256"]:
                        raise DiagnosticError(
                            E_INPUT_INVALID,
                            "duplicate snapshot version has a different checksum",
                            case_id=record.case_id,
                            version=record.version,
                        )
                    connection.execute("ROLLBACK")
                    return self._snapshot_from_row(
                        connection,
                        current_row,
                        include_children=True,
                    )
            elif record.version != 1:
                raise DiagnosticError(
                    E_SNAPSHOT_ROLLBACK,
                    "first snapshot version must be 1",
                    case_id=record.case_id,
                    requested_version=record.version,
                )

            cursor = connection.execute(
                """
                INSERT INTO snapshots(
                    case_id, version, weight_revision, input_sha256,
                    snapshot_sha256, dependency_sha256, output_sha256,
                    output_path, created_at, is_active, previous_snapshot_id,
                    details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    record.case_id,
                    record.version,
                    record.weight_revision,
                    record.input_sha256,
                    snapshot_sha,
                    dependency_sha,
                    record.output_sha256,
                    record.output_path,
                    created_at,
                    record.previous_snapshot_id,
                    json.dumps(record.details, sort_keys=True, separators=(",", ":")),
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            for node in sorted(record.nodes, key=lambda item: item.node_id):
                connection.execute(
                    """
                    INSERT INTO ledger_nodes(
                        snapshot_id, node_id, node_type, version, cache_key,
                        content_sha256, artifact_path, dependency_sha256,
                        dep_hashes_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        node.node_id,
                        node.node_type,
                        node.version,
                        node.cache_key,
                        node.content_sha256,
                        node.artifact_path,
                        node.dependency_sha256,
                        json.dumps(
                            node.dep_hashes,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        created_at,
                    ),
                )
            for plan in sorted(record.plans, key=lambda item: (item.head, item.q_block)):
                connection.execute(
                    """
                    INSERT INTO block_plans(
                        snapshot_id, case_id, version, head, q_block, q_start,
                        q_len, kv_blocks_json, plan_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        record.case_id,
                        record.version,
                        plan.head,
                        plan.q_block,
                        plan.q_start,
                        plan.q_len,
                        json.dumps(
                            [dict(item) for item in plan.kv_blocks],
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        plan.plan_sha256,
                    ),
                )

            if current_row is not None:
                connection.execute(
                    "UPDATE snapshots SET is_active = 0 WHERE snapshot_id = ?",
                    (current_id,),
                )
            connection.execute(
                "UPDATE snapshots SET is_active = 1 WHERE snapshot_id = ?",
                (snapshot_id,),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

        snapshot = self.get_snapshot(
            record.case_id,
            version=record.version,
            include_children=True,
        )
        if snapshot.snapshot_sha256 != snapshot_sha:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "committed snapshot checksum does not match reconstructed checksum",
                case_id=record.case_id,
                version=record.version,
            )
        return snapshot

    def replay(self, case_id: str, *, version: int | None = None) -> LedgerSnapshot:
        snapshot = self.get_snapshot(case_id, version=version, include_children=True)
        reconstruct_nodes = tuple(
            NodeRecord(
                node_id=node["node_id"],
                node_type=node["node_type"],
                version=node["version"],
                cache_key=node["cache_key"],
                content_sha256=node["content_sha256"],
                artifact_path=node["artifact_path"],
                dep_hashes=node["dep_hashes"],
            )
            for node in snapshot.nodes
        )
        reconstruct_plans = tuple(
            PlanRecord(
                head=plan["head"],
                q_block=plan["q_block"],
                q_start=plan["q_start"],
                q_len=plan["q_len"],
                kv_blocks=tuple(plan["kv_blocks"]),
                plan_sha256=plan["plan_sha256"],
            )
            for plan in snapshot.plans
        )
        record = SnapshotRecord(
            case_id=snapshot.case_id,
            version=snapshot.version,
            weight_revision=snapshot.weight_revision,
            input_sha256=snapshot.input_sha256,
            output_sha256=snapshot.output_sha256,
            output_path=snapshot.output_path,
            previous_snapshot_id=snapshot.previous_snapshot_id,
            nodes=reconstruct_nodes,
            plans=reconstruct_plans,
            details=snapshot.details,
        )
        if record.snapshot_sha256 != snapshot.snapshot_sha256:
            raise DiagnosticError(
                E_LEDGER_INTEGRITY,
                "ledger replay checksum mismatch",
                case_id=case_id,
                version=snapshot.version,
            )
        return snapshot

