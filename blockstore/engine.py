"""Snapshot chain, rollback, reference accounting, GC, and recovery engine."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .db import connect, initialize_db, transaction
from .errors import BusinessError
from .model import SampleSpec, canonical_visible_hash, normalize_digest
from .storage import BlockStorage


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _error(code: str, message: str, **details: Any) -> BusinessError:
    return BusinessError(code, message, details=details)


class BlockSnapshotStore:
    """A local snapshot-chain store with crash-recoverable SQLite metadata."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        recovery_timeout_seconds: float = 30.0,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.db_path = self.state_dir / "meta.sqlite3"
        self.staging_db_path = self.state_dir / "meta.sqlite3.staging"
        self.lock_path = self.state_dir / "store.lock"
        self.storage = BlockStorage(self.state_dir)
        self.recovery_timeout_seconds = recovery_timeout_seconds

    @contextmanager
    def _mutation_lock(self) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @property
    def _crash_point(self) -> str | None:
        return os.environ.get("BLOCKSTORE_CRASH_AT") or None

    def _maybe_crash(self, point: str) -> None:
        if self._crash_point == point:
            os._exit(97)

    def _connect_ro(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise _error("STATE_NOT_FOUND", "no initialized store exists at this path")
        try:
            return connect(self.db_path)
        except sqlite3.DatabaseError as exc:
            raise _error("STATE_CORRUPT", "store database cannot be opened") from exc

    def _connect_rw(self) -> sqlite3.Connection:
        conn = self._connect_ro()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
        except sqlite3.DatabaseError as exc:
            conn.close()
            raise _error("STATE_CORRUPT", "store database cannot enter WAL mode") from exc
        return conn

    @staticmethod
    def _meta(conn: sqlite3.Connection, key: str) -> str:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            raise _error("STATE_CORRUPT", "required metadata is missing", key=key)
        return str(row["value"])

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def load(self, sample: SampleSpec) -> dict[str, Any]:
        """Create a store from a fully validated sample.

        Validation happens before this method is called, so invalid samples never
        reach a filesystem write.
        """

        with self._mutation_lock():
            if self.db_path.is_file():
                conn = self._connect_ro()
                try:
                    stored_hash = self._meta(conn, "sample_hash")
                    volume_id = self._meta(conn, "volume_id")
                    chain_version = int(self._meta(conn, "chain_version"))
                    base_id = self._meta(conn, "base_snapshot_id")
                finally:
                    conn.close()
                if stored_hash != sample.sample_hash:
                    raise _error(
                        "STATE_CONFLICT",
                        "this state directory contains a different sample",
                        stored_sample_hash=stored_hash,
                        incoming_sample_hash=sample.sample_hash,
                    )
                return {
                    "ok": True,
                    "command": "load",
                    "status": "already_loaded",
                    "sample_hash": sample.sample_hash,
                    "volume_id": volume_id,
                    "base_snapshot_id": base_id,
                    "chain_version": chain_version,
                }

            self.storage.ensure_directories()
            for stale in (
                self.staging_db_path,
                Path(f"{self.staging_db_path}-journal"),
                Path(f"{self.staging_db_path}-wal"),
                Path(f"{self.staging_db_path}-shm"),
            ):
                try:
                    stale.unlink()
                except FileNotFoundError:
                    pass

            for block in sample.volume.blocks:
                self.storage.put(block.data)

            staging = initialize_db(self.staging_db_path, wal=False)
            try:
                visible_map = {
                    block.block_index: block.digest for block in sample.volume.blocks
                }
                visible_hash = canonical_visible_hash(visible_map)
                base_content_hash = self.storage.digest(
                    f"base:{sample.volume.volume_id}:{visible_hash}".encode("utf-8")
                )
                meta_values = {
                    "format_version": sample.format_version,
                    "sample_hash": sample.sample_hash,
                    "volume_id": sample.volume.volume_id,
                    "block_size": sample.volume.block_size,
                    "block_count": sample.volume.block_count,
                    "base_snapshot_id": sample.volume.base_snapshot_id,
                    "initial_clock_ms": sample.volume.initial_clock_ms,
                    "current_snapshot_id": sample.volume.base_snapshot_id,
                    "chain_version": 1,
                    "clock_watermark": sample.volume.initial_clock_ms,
                    "gc_sequence": 0,
                }
                with transaction(staging, immediate=True):
                    for key, value in meta_values.items():
                        staging.execute(
                            "INSERT INTO meta(key, value) VALUES(?, ?)", (key, str(value))
                        )
                    staging.execute(
                        """
                        INSERT INTO snapshots(
                            snapshot_id, sequence, parent_id, created_clock_ms,
                            status, is_base, content_hash, visible_hash,
                            created_operation
                        ) VALUES(?, 0, NULL, ?, 'alive', 1, ?, ?, 'load')
                        """,
                        (
                            sample.volume.base_snapshot_id,
                            sample.volume.initial_clock_ms,
                            base_content_hash,
                            visible_hash,
                        ),
                    )
                    staging.execute(
                        """
                        INSERT INTO snapshot_ranges(
                            snapshot_id, range_no, start_block, end_block
                        ) VALUES(?, 0, 0, ?)
                        """,
                        (sample.volume.base_snapshot_id, sample.volume.block_count - 1),
                    )
                    for block in sample.volume.blocks:
                        staging.execute(
                            """
                            INSERT INTO snapshot_writes(
                                snapshot_id, block_index, digest
                            ) VALUES(?, ?, ?)
                            """,
                            (
                                sample.volume.base_snapshot_id,
                                block.block_index,
                                block.digest,
                            ),
                        )
                        staging.execute(
                            """
                            INSERT INTO snapshot_blocks(
                                snapshot_id, block_index, digest, owner_snapshot_id
                            ) VALUES(?, ?, ?, ?)
                            """,
                            (
                                sample.volume.base_snapshot_id,
                                block.block_index,
                                block.digest,
                                sample.volume.base_snapshot_id,
                            ),
                        )
                        staging.execute(
                            """
                            INSERT INTO volume_blocks(
                                block_index, digest, owner_snapshot_id
                            ) VALUES(?, ?, ?)
                            """,
                            (
                                block.block_index,
                                block.digest,
                                sample.volume.base_snapshot_id,
                            ),
                        )
                    staging.execute(
                        """
                        INSERT INTO block_refs(
                            source_kind, source_id, block_index, digest
                        )
                        SELECT 'snapshot', snapshot_id, block_index, digest
                        FROM snapshot_blocks WHERE snapshot_id = ?
                        """,
                        (sample.volume.base_snapshot_id,),
                    )
                    staging.execute(
                        """
                        INSERT INTO block_refs(
                            source_kind, source_id, block_index, digest
                        )
                        SELECT 'volume', ?, block_index, digest
                        FROM volume_blocks
                        """,
                        (sample.volume.volume_id,),
                    )
                    for snapshot in sample.snapshots:
                        staging.execute(
                            """
                            INSERT INTO planned_snapshots(
                                sequence, snapshot_id, parent_id,
                                created_clock_ms, content_hash
                            ) VALUES(?, ?, ?, ?, ?)
                            """,
                            (
                                snapshot.sequence,
                                snapshot.snapshot_id,
                                snapshot.parent_id,
                                snapshot.clock_ms,
                                snapshot.content_hash,
                            ),
                        )
                        for range_no, item_range in enumerate(snapshot.ranges):
                            staging.execute(
                                """
                                INSERT INTO planned_ranges(
                                    sequence, range_no, start_block, end_block
                                ) VALUES(?, ?, ?, ?)
                                """,
                                (
                                    snapshot.sequence,
                                    range_no,
                                    item_range.start,
                                    item_range.end,
                                ),
                            )
                        for block in snapshot.writes.values():
                            staging.execute(
                                """
                                INSERT INTO planned_writes(
                                    sequence, block_index, digest, data
                                ) VALUES(?, ?, ?, ?)
                                """,
                                (
                                    snapshot.sequence,
                                    block.block_index,
                                    block.digest,
                                    sqlite3.Binary(block.data),
                                ),
                            )
                    for rollback in sample.rollbacks:
                        staging.execute(
                            """
                            INSERT INTO planned_rollbacks(
                                request_id, target_snapshot_id, clock_ms, retain_after
                            ) VALUES(?, ?, ?, ?)
                            """,
                            (
                                rollback.request_id,
                                rollback.target_snapshot_id,
                                rollback.clock_ms,
                                int(rollback.retain_after),
                            ),
                        )
                staging.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                staging.close()

            os.replace(self.staging_db_path, self.db_path)
            conn = self._connect_rw()
            conn.close()
            return {
                "ok": True,
                "command": "load",
                "status": "loaded",
                "sample_hash": sample.sample_hash,
                "volume_id": sample.volume.volume_id,
                "base_snapshot_id": sample.volume.base_snapshot_id,
                "chain_version": 1,
            }

    def _ancestors(
        self,
        conn: sqlite3.Connection,
        snapshot_id: str,
        *,
        include_self: bool = True,
    ) -> list[str]:
        rows = conn.execute(
            "SELECT snapshot_id, parent_id FROM snapshots"
        ).fetchall()
        parents = {str(row["snapshot_id"]): row["parent_id"] for row in rows}
        if snapshot_id not in parents:
            raise _error(
                "SNAPSHOT_NOT_FOUND",
                "snapshot does not exist",
                snapshot_id=snapshot_id,
            )
        result: list[str] = []
        seen: set[str] = set()
        current: str | None = snapshot_id
        while current is not None:
            if current in seen:
                raise _error("STATE_CORRUPT", "snapshot parent cycle detected")
            seen.add(current)
            if include_self or current != snapshot_id:
                result.append(current)
            parent = parents.get(current)
            current = str(parent) if parent is not None else None
        return result

    def _snapshot_block_map(
        self, conn: sqlite3.Connection, snapshot_id: str
    ) -> dict[int, tuple[str, str]]:
        rows = conn.execute(
            """
            SELECT block_index, digest, owner_snapshot_id
            FROM snapshot_blocks WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchall()
        return {
            int(row["block_index"]): (
                str(row["digest"]),
                str(row["owner_snapshot_id"]),
            )
            for row in rows
        }

    @staticmethod
    def _replace_refs(
        conn: sqlite3.Connection,
        source_kind: str,
        source_id: str,
        block_map: dict[int, str],
    ) -> None:
        conn.execute(
            "DELETE FROM block_refs WHERE source_kind = ? AND source_id = ?",
            (source_kind, source_id),
        )
        conn.executemany(
            """
            INSERT INTO block_refs(
                source_kind, source_id, block_index, digest
            ) VALUES(?, ?, ?, ?)
            """,
            [
                (source_kind, source_id, block_index, block_map[block_index])
                for block_index in sorted(block_map)
            ],
        )

    def _abort_pending_operations(
        self, conn: sqlite3.Connection
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT operation_id, kind, phase, snapshot_id, request_id
            FROM operations WHERE status = 'pending'
            ORDER BY operation_id
            """
        ).fetchall()
        aborted: list[dict[str, Any]] = []
        if not rows:
            return aborted
        with transaction(conn, immediate=True):
            for row in rows:
                operation_id = str(row["operation_id"])
                conn.execute(
                    "DELETE FROM operation_refs WHERE operation_id = ?",
                    (operation_id,),
                )
                conn.execute(
                    "DELETE FROM block_refs WHERE source_kind = 'transaction' "
                    "AND source_id = ?",
                    (operation_id,),
                )
                conn.execute(
                    """
                    UPDATE operations
                    SET status = 'aborted', phase = 'aborted_on_resume'
                    WHERE operation_id = ? AND status = 'pending'
                    """,
                    (operation_id,),
                )
                aborted.append(
                    {
                        "operation_id": operation_id,
                        "kind": str(row["kind"]),
                        "last_phase": str(row["phase"]),
                        "snapshot_id": row["snapshot_id"],
                        "request_id": row["request_id"],
                        "decision": "rolled_back",
                    }
                )
        return aborted

    def _complete_gc_run(
        self,
        conn: sqlite3.Connection,
        run_row: sqlite3.Row,
    ) -> dict[str, Any]:
        run_id = str(run_row["run_id"])
        candidate_rows = conn.execute(
            "SELECT digest FROM gc_candidates WHERE run_id = ? ORDER BY digest",
            (run_id,),
        ).fetchall()
        deleted: list[str] = []
        absent: list[str] = []
        retained_safety: list[str] = []
        for row in candidate_rows:
            digest = str(row["digest"])
            count_row = conn.execute(
                "SELECT COUNT(*) AS count FROM block_refs WHERE digest = ?",
                (digest,),
            ).fetchone()
            ref_count = int(count_row["count"]) if count_row is not None else 0
            if ref_count != 0:
                retained_safety.append(digest)
                continue
            if self.storage.quarantine_and_remove(digest, run_id):
                deleted.append(digest)
            else:
                absent.append(digest)

        with transaction(conn, immediate=True):
            for digest in deleted:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO gc_items(
                        run_id, digest, action, reason, ref_count
                    ) VALUES(?, ?, 'deleted', 'zero_references', 0)
                    """,
                    (run_id, digest),
                )
            for digest in absent:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO gc_items(
                        run_id, digest, action, reason, ref_count
                    ) VALUES(?, ?, 'absent', 'zero_references', 0)
                    """,
                    (run_id, digest),
                )
            for digest in retained_safety:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO gc_items(
                        run_id, digest, action, reason, ref_count
                    ) VALUES(?, ?, 'retained', 'reference_appeared_during_gc', 1)
                    """,
                    (run_id, digest),
                )
            conn.execute(
                """
                UPDATE gc_runs SET
                    status = 'completed',
                    reclaimed_count = (
                        SELECT COUNT(*) FROM gc_items
                        WHERE run_id = ? AND action IN ('deleted', 'absent')
                    ),
                    retained_count = (
                        SELECT COUNT(*) FROM gc_items
                        WHERE run_id = ? AND action = 'retained'
                    )
                WHERE run_id = ?
                """,
                (run_id, run_id, run_id),
            )
            version = int(self._meta(conn, "chain_version"))
            self._set_meta(conn, "chain_version", version + 1)
        return {
            "run_id": run_id,
            "deleted": deleted,
            "absent": absent,
            "retained_safety": retained_safety,
        }

    def _recover_locked(self, conn: sqlite3.Connection) -> dict[str, Any]:
        aborted = self._abort_pending_operations(conn)
        removed_temp = self.storage.clean_tmp()
        completed_runs: list[dict[str, Any]] = []
        while True:
            row = conn.execute(
                """
                SELECT run_id FROM gc_runs
                WHERE status = 'running'
                ORDER BY run_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                break
            completed_runs.append(self._complete_gc_run(conn, row))
        removed_trash = self.storage.clean_trash()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return {
            "aborted_operations": aborted,
            "completed_gc_runs": completed_runs,
            "removed_temp_files": removed_temp,
            "removed_trash_files": removed_trash,
        }

    def _recover_with_lock(self) -> dict[str, Any]:
        conn = self._connect_rw()
        try:
            return self._recover_locked(conn)
        finally:
            conn.close()

    def snapshot(self, sequence: int) -> dict[str, Any]:
        with self._mutation_lock():
            self._recover_with_lock()
            conn = self._connect_rw()
            try:
                plan = conn.execute(
                    "SELECT * FROM planned_snapshots WHERE sequence = ?", (sequence,)
                ).fetchone()
                if plan is None:
                    raise _error(
                        "SNAPSHOT_PLAN_NOT_FOUND",
                        "no planned snapshot has this sequence",
                        sequence=sequence,
                    )
                snapshot_id = str(plan["snapshot_id"])
                content_hash = str(plan["content_hash"])
                existing = conn.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,)
                ).fetchone()
                if existing is not None:
                    if str(existing["content_hash"]) == content_hash:
                        return {
                            "ok": True,
                            "command": "snapshot",
                            "status": "already_committed",
                            "snapshot_id": snapshot_id,
                            "snapshot_status": str(existing["status"]),
                            "chain_version": int(self._meta(conn, "chain_version")),
                        }
                    raise _error(
                        "SNAPSHOT_ID_CONFLICT",
                        "snapshot id was already committed with different content",
                        snapshot_id=snapshot_id,
                    )
                sequence_row = conn.execute(
                    "SELECT snapshot_id FROM snapshots WHERE sequence = ?", (sequence,)
                ).fetchone()
                if sequence_row is not None:
                    raise _error(
                        "SNAPSHOT_SEQUENCE_CONFLICT",
                        "snapshot sequence was already committed",
                        sequence=sequence,
                        snapshot_id=str(sequence_row["snapshot_id"]),
                    )

                parent_id = str(plan["parent_id"])
                parent = conn.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id = ?", (parent_id,)
                ).fetchone()
                if parent is None:
                    raise _error(
                        "PARENT_NOT_CREATED",
                        "snapshot parent has not been created in this state",
                        parent_id=parent_id,
                    )
                if str(parent["status"]) != "alive":
                    raise _error(
                        "PARENT_NOT_ALIVE",
                        "a new snapshot cannot use an orphan snapshot as parent",
                        parent_id=parent_id,
                    )
                clock_ms = int(plan["created_clock_ms"])
                if clock_ms <= int(parent["created_clock_ms"]):
                    raise _error(
                        "CLOCK_NOT_MONOTONIC",
                        "snapshot clock must be strictly later than its parent",
                        clock_ms=clock_ms,
                        parent_clock_ms=int(parent["created_clock_ms"]),
                    )
                watermark = int(self._meta(conn, "clock_watermark"))
                if clock_ms <= watermark:
                    raise _error(
                        "CLOCK_WATERMARK_REGRESSION",
                        "snapshot clock must advance the persisted logical clock",
                        clock_ms=clock_ms,
                        clock_watermark=watermark,
                    )

                write_rows = conn.execute(
                    """
                    SELECT block_index, digest, data FROM planned_writes
                    WHERE sequence = ? ORDER BY block_index
                    """,
                    (sequence,),
                ).fetchall()
                writes: dict[int, tuple[str, bytes]] = {}
                for row in write_rows:
                    digest = normalize_digest(row["digest"])
                    data = bytes(row["data"])
                    if self.storage.digest(data) != digest:
                        raise _error(
                            "PLAN_BLOCK_CORRUPTION",
                            "persisted snapshot plan data does not match its digest",
                            block=int(row["block_index"]),
                        )
                    writes[int(row["block_index"])] = (digest, data)

                parent_view = self._snapshot_block_map(conn, parent_id)
                block_count = int(self._meta(conn, "block_count"))
                if len(parent_view) != block_count:
                    raise _error(
                        "STATE_CORRUPT",
                        "parent snapshot does not contain a complete block view",
                        parent_id=parent_id,
                    )
                visible_owners = {
                    block_index: owner for block_index, (_digest, owner) in parent_view.items()
                }
                for block_index in writes:
                    visible_owners[block_index] = snapshot_id
                visible_map = {
                    block_index: parent_view[block_index][0] for block_index in parent_view
                }
                for block_index, (digest, _data) in writes.items():
                    visible_map[block_index] = digest
                visible_hash = canonical_visible_hash(visible_map)
                operation_id = f"snapshot:{snapshot_id}:{content_hash[:16]}"

                with transaction(conn, immediate=True):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO operations(
                            operation_id, kind, status, phase, payload_hash,
                            snapshot_id, request_id, clock_ms
                        ) VALUES(?, 'snapshot', 'pending', 'pending_refs', ?, ?, NULL, ?)
                        """,
                        (
                            operation_id,
                            content_hash,
                            snapshot_id,
                            clock_ms,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM operation_refs WHERE operation_id = ?",
                        (operation_id,),
                    )
                    conn.execute(
                        "DELETE FROM block_refs WHERE source_kind = 'transaction' "
                        "AND source_id = ?",
                        (operation_id,),
                    )
                    conn.executemany(
                        """
                        INSERT INTO operation_refs(
                            operation_id, block_index, digest
                        ) VALUES(?, ?, ?)
                        """,
                        [
                            (operation_id, block_index, writes[block_index][0])
                            for block_index in sorted(writes)
                        ],
                    )
                    conn.executemany(
                        """
                        INSERT INTO block_refs(
                            source_kind, source_id, block_index, digest
                        ) VALUES('transaction', ?, ?, ?)
                        """,
                        [
                            (operation_id, block_index, writes[block_index][0])
                            for block_index in sorted(writes)
                        ],
                    )
                self._maybe_crash("snapshot:after_pending")

                for block_index in sorted(writes):
                    _digest, data = writes[block_index]
                    self.storage.put(data)
                with transaction(conn, immediate=True):
                    conn.execute(
                        "UPDATE operations SET phase = 'blocks_materialized' "
                        "WHERE operation_id = ?",
                        (operation_id,),
                    )
                self._maybe_crash("snapshot:after_blocks")
                self._maybe_crash("snapshot:before_final_commit")

                with transaction(conn, immediate=True):
                    current_version = int(self._meta(conn, "chain_version"))
                    existing = conn.execute(
                        "SELECT snapshot_id FROM snapshots WHERE snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                    if existing is not None:
                        raise _error(
                            "CONCURRENT_CONFLICT",
                            "snapshot was committed by another writer",
                            snapshot_id=snapshot_id,
                        )
                    parent_now = conn.execute(
                        "SELECT status FROM snapshots WHERE snapshot_id = ?",
                        (parent_id,),
                    ).fetchone()
                    if parent_now is None or str(parent_now["status"]) != "alive":
                        raise _error(
                            "CONCURRENT_CONFLICT",
                            "snapshot parent changed before final commit",
                            parent_id=parent_id,
                        )
                    conn.execute(
                        """
                        INSERT INTO snapshots(
                            snapshot_id, sequence, parent_id, created_clock_ms,
                            status, is_base, content_hash, visible_hash,
                            created_operation
                        ) VALUES(?, ?, ?, ?, 'alive', 0, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            sequence,
                            parent_id,
                            clock_ms,
                            content_hash,
                            visible_hash,
                            operation_id,
                        ),
                    )
                    range_rows = conn.execute(
                        """
                        SELECT range_no, start_block, end_block
                        FROM planned_ranges WHERE sequence = ? ORDER BY range_no
                        """,
                        (sequence,),
                    ).fetchall()
                    for range_row in range_rows:
                        conn.execute(
                            """
                            INSERT INTO snapshot_ranges(
                                snapshot_id, range_no, start_block, end_block
                            ) VALUES(?, ?, ?, ?)
                            """,
                            (
                                snapshot_id,
                                int(range_row["range_no"]),
                                int(range_row["start_block"]),
                                int(range_row["end_block"]),
                            ),
                        )
                    for block_index in sorted(writes):
                        conn.execute(
                            """
                            INSERT INTO snapshot_writes(
                                snapshot_id, block_index, digest
                            ) VALUES(?, ?, ?)
                            """,
                            (snapshot_id, block_index, writes[block_index][0]),
                        )
                    conn.executemany(
                        """
                        INSERT INTO snapshot_blocks(
                            snapshot_id, block_index, digest, owner_snapshot_id
                        ) VALUES(?, ?, ?, ?)
                        """,
                        [
                            (
                                snapshot_id,
                                block_index,
                                visible_map[block_index],
                                visible_owners[block_index],
                            )
                            for block_index in sorted(visible_map)
                        ],
                    )
                    conn.execute("DELETE FROM volume_blocks")
                    conn.execute(
                        """
                        INSERT INTO volume_blocks(
                            block_index, digest, owner_snapshot_id
                        )
                        SELECT block_index, digest, owner_snapshot_id
                        FROM snapshot_blocks WHERE snapshot_id = ?
                        """,
                        (snapshot_id,),
                    )
                    self._replace_refs(
                        conn,
                        "snapshot",
                        snapshot_id,
                        {index: visible_map[index] for index in visible_map},
                    )
                    self._replace_refs(
                        conn,
                        "volume",
                        self._meta(conn, "volume_id"),
                        {index: visible_map[index] for index in visible_map},
                    )
                    self._set_meta(conn, "current_snapshot_id", snapshot_id)
                    self._set_meta(conn, "chain_version", current_version + 1)
                    self._set_meta(
                        conn,
                        "clock_watermark",
                        max(watermark, clock_ms),
                    )
                    conn.execute(
                        "UPDATE operations SET status = 'committed', "
                        "phase = 'final_committed' WHERE operation_id = ?",
                        (operation_id,),
                    )
                    conn.execute(
                        "DELETE FROM operation_refs WHERE operation_id = ?",
                        (operation_id,),
                    )
                    conn.execute(
                        "DELETE FROM block_refs WHERE source_kind = 'transaction' "
                        "AND source_id = ?",
                        (operation_id,),
                    )
                self._maybe_crash("snapshot:after_commit")
                return {
                    "ok": True,
                    "command": "snapshot",
                    "status": "committed",
                    "snapshot_id": snapshot_id,
                    "sequence": sequence,
                    "parent_id": parent_id,
                    "clock_ms": clock_ms,
                    "chain_version": int(current_version) + 1,
                    "current_snapshot_id": snapshot_id,
                    "written_blocks": [
                        {
                            "block": block_index,
                            "digest": writes[block_index][0],
                        }
                        for block_index in sorted(writes)
                    ],
                    "visible_hash": visible_hash,
                }
            finally:
                conn.close()

    def rollback(self, request_id: str) -> dict[str, Any]:
        with self._mutation_lock():
            self._recover_with_lock()
            conn = self._connect_rw()
            try:
                executed = conn.execute(
                    "SELECT result_json FROM rollback_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if executed is not None:
                    return json.loads(str(executed["result_json"]))

                plan = conn.execute(
                    "SELECT * FROM planned_rollbacks WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if plan is None:
                    raise _error(
                        "ROLLBACK_PLAN_NOT_FOUND",
                        "no planned rollback has this request id",
                        request_id=request_id,
                    )
                target_id = str(plan["target_snapshot_id"])
                clock_ms = int(plan["clock_ms"])
                retain_after = bool(plan["retain_after"])
                target = conn.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id = ?", (target_id,)
                ).fetchone()
                if target is None:
                    raise _error(
                        "SNAPSHOT_NOT_FOUND",
                        "rollback target does not exist",
                        snapshot_id=target_id,
                    )
                if str(target["status"]) != "alive":
                    raise _error(
                        "SNAPSHOT_NOT_ALIVE",
                        "rollback target is an orphan",
                        snapshot_id=target_id,
                        orphan_reason=target["orphan_reason"],
                    )
                watermark = int(self._meta(conn, "clock_watermark"))
                if clock_ms <= watermark:
                    raise _error(
                        "CLOCK_WATERMARK_REGRESSION",
                        "rollback clock must advance the persisted logical clock",
                        clock_ms=clock_ms,
                        clock_watermark=watermark,
                    )
                if clock_ms <= int(target["created_clock_ms"]):
                    raise _error(
                        "CLOCK_NOT_MONOTONIC",
                        "rollback clock must be later than the target snapshot",
                        clock_ms=clock_ms,
                        target_clock_ms=int(target["created_clock_ms"]),
                    )

                target_view_rows = conn.execute(
                    """
                    SELECT block_index, digest, owner_snapshot_id
                    FROM snapshot_blocks WHERE snapshot_id = ?
                    """,
                    (target_id,),
                ).fetchall()
                target_digests = {
                    int(row["block_index"]): str(row["digest"])
                    for row in target_view_rows
                }
                if len(target_digests) != int(self._meta(conn, "block_count")):
                    raise _error(
                        "STATE_CORRUPT",
                        "rollback target does not contain a complete block view",
                        snapshot_id=target_id,
                    )

                ancestor_ids = set(self._ancestors(conn, target_id))
                all_snapshots = conn.execute(
                    """
                    SELECT snapshot_id, sequence, status, is_base, created_clock_ms
                    FROM snapshots ORDER BY sequence
                    """
                ).fetchall()
                candidates = [
                    row
                    for row in all_snapshots
                    if str(row["snapshot_id"]) not in ancestor_ids
                    and int(row["is_base"]) == 0
                    and str(row["status"]) == "alive"
                    and int(row["created_clock_ms"]) > int(target["created_clock_ms"])
                ]
                operation_id = f"rollback:{request_id}"
                with transaction(conn, immediate=True):
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO operations(
                            operation_id, kind, status, phase, payload_hash,
                            snapshot_id, request_id, clock_ms
                        ) VALUES(?, 'rollback', 'pending', 'pending_refs', ?, ?, ?, ?)
                        """,
                        (
                            operation_id,
                            _stable_json(
                                {
                                    "request_id": request_id,
                                    "target": target_id,
                                    "clock_ms": clock_ms,
                                    "retain_after": retain_after,
                                }
                            ),
                            target_id,
                            request_id,
                            clock_ms,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM operation_refs WHERE operation_id = ?",
                        (operation_id,),
                    )
                    conn.execute(
                        "DELETE FROM block_refs WHERE source_kind = 'transaction' "
                        "AND source_id = ?",
                        (operation_id,),
                    )
                    conn.executemany(
                        """
                        INSERT INTO operation_refs(
                            operation_id, block_index, digest
                        ) VALUES(?, ?, ?)
                        """,
                        [
                            (operation_id, block_index, target_digests[block_index])
                            for block_index in sorted(target_digests)
                        ],
                    )
                    conn.executemany(
                        """
                        INSERT INTO block_refs(
                            source_kind, source_id, block_index, digest
                        ) VALUES('transaction', ?, ?, ?)
                        """,
                        [
                            (operation_id, block_index, target_digests[block_index])
                            for block_index in sorted(target_digests)
                        ],
                    )
                self._maybe_crash("rollback:after_pending")
                self._maybe_crash("rollback:before_final_commit")

                with transaction(conn, immediate=True):
                    current_version = int(self._meta(conn, "chain_version"))
                    target_now = conn.execute(
                        "SELECT status FROM snapshots WHERE snapshot_id = ?",
                        (target_id,),
                    ).fetchone()
                    if target_now is None or str(target_now["status"]) != "alive":
                        raise _error(
                            "CONCURRENT_CONFLICT",
                            "rollback target changed before final commit",
                            snapshot_id=target_id,
                        )
                    conn.execute("DELETE FROM volume_blocks")
                    conn.execute(
                        """
                        INSERT INTO volume_blocks(
                            block_index, digest, owner_snapshot_id
                        )
                        SELECT block_index, digest, owner_snapshot_id
                        FROM snapshot_blocks WHERE snapshot_id = ?
                        """,
                        (target_id,),
                    )
                    self._replace_refs(
                        conn,
                        "volume",
                        self._meta(conn, "volume_id"),
                        target_digests,
                    )
                    decisions: list[dict[str, str]] = []
                    for candidate in candidates:
                        candidate_id = str(candidate["snapshot_id"])
                        if retain_after:
                            action = "preserved"
                            reason = "ROLLBACK_RETAIN_AFTER"
                        else:
                            action = "orphaned"
                            reason = "ROLLBACK_BRANCH_INVALIDATED"
                            conn.execute(
                                """
                                UPDATE snapshots
                                SET status = 'orphan', orphan_reason = ?,
                                    orphaned_by = ?
                                WHERE snapshot_id = ? AND status = 'alive'
                                """,
                                (reason, request_id, candidate_id),
                            )
                            conn.execute(
                                """
                                DELETE FROM block_refs
                                WHERE source_kind = 'snapshot' AND source_id = ?
                                """,
                                (candidate_id,),
                            )
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO fork_decisions(
                                rollback_id, snapshot_id, action,
                                reason, previous_status
                            ) VALUES(?, ?, ?, ?, 'alive')
                            """,
                            (request_id, candidate_id, action, reason),
                        )
                        decisions.append(
                            {
                                "snapshot_id": candidate_id,
                                "action": action,
                                "reason": reason,
                            }
                        )

                    new_version = current_version + 1
                    self._set_meta(conn, "current_snapshot_id", target_id)
                    self._set_meta(conn, "chain_version", new_version)
                    self._set_meta(
                        conn,
                        "clock_watermark",
                        max(watermark, clock_ms),
                    )
                    result = {
                        "ok": True,
                        "command": "rollback",
                        "status": "applied",
                        "request_id": request_id,
                        "target_snapshot_id": target_id,
                        "clock_ms": clock_ms,
                        "retain_after": retain_after,
                        "chain_version": new_version,
                        "current_snapshot_id": target_id,
                        "fork_decisions": decisions,
                        "orphaned_snapshot_ids": [
                            item["snapshot_id"]
                            for item in decisions
                            if item["action"] == "orphaned"
                        ],
                        "preserved_snapshot_ids": [
                            item["snapshot_id"]
                            for item in decisions
                            if item["action"] == "preserved"
                        ],
                        "volume_hash": canonical_visible_hash(target_digests),
                    }
                    conn.execute(
                        """
                        INSERT INTO rollback_requests(
                            request_id, target_snapshot_id, clock_ms,
                            retain_after, view_version, result_json
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            target_id,
                            clock_ms,
                            int(retain_after),
                            new_version,
                            _stable_json(result),
                        ),
                    )
                    conn.execute(
                        "UPDATE operations SET status = 'committed', "
                        "phase = 'final_committed' WHERE operation_id = ?",
                        (operation_id,),
                    )
                    conn.execute(
                        "DELETE FROM operation_refs WHERE operation_id = ?",
                        (operation_id,),
                    )
                    conn.execute(
                        "DELETE FROM block_refs WHERE source_kind = 'transaction' "
                        "AND source_id = ?",
                        (operation_id,),
                    )
                self._maybe_crash("rollback:after_commit")
                return result
            finally:
                conn.close()

    def _reference_summary(
        self, conn: sqlite3.Connection
    ) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
        rows = conn.execute(
            """
            SELECT source_kind, source_id, block_index, digest
            FROM block_refs ORDER BY digest, source_kind, source_id, block_index
            """
        ).fetchall()
        counts: dict[str, int] = {}
        grouped: dict[tuple[str, str, str], list[int]] = {}
        for row in rows:
            digest = str(row["digest"])
            counts[digest] = counts.get(digest, 0) + 1
            key = (digest, str(row["source_kind"]), str(row["source_id"]))
            grouped.setdefault(key, []).append(int(row["block_index"]))
        sources: dict[str, list[dict[str, Any]]] = {}
        for (digest, kind, source_id), blocks in sorted(grouped.items()):
            sources.setdefault(digest, []).append(
                {
                    "kind": kind,
                    "id": source_id,
                    "blocks": sorted(blocks),
                }
            )
        return counts, sources

    def gc(self) -> dict[str, Any]:
        with self._mutation_lock():
            self._recover_with_lock()
            conn = self._connect_rw()
            try:
                version = int(self._meta(conn, "chain_version"))
                refs, reasons = self._reference_summary(conn)
                disk_digests = self.storage.list_digests()
                candidates = [digest for digest in disk_digests if refs.get(digest, 0) == 0]
                retained = [digest for digest in disk_digests if refs.get(digest, 0) > 0]
                gc_sequence = int(self._meta(conn, "gc_sequence")) + 1
                run_id = f"gc-v{version:08d}-{gc_sequence:04d}"
                with transaction(conn, immediate=True):
                    conn.execute(
                        """
                        INSERT INTO gc_runs(
                            run_id, view_version, status,
                            reclaimed_count, retained_count, scanned_count
                        ) VALUES(?, ?, 'running', 0, ?, ?)
                        """,
                        (run_id, version, len(retained), len(disk_digests)),
                    )
                    conn.executemany(
                        "INSERT INTO gc_candidates(run_id, digest) VALUES(?, ?)",
                        [(run_id, digest) for digest in candidates],
                    )
                    for digest in retained:
                        reason_payload = [
                            {
                                "kind": source["kind"],
                                "id": source["id"],
                            }
                            for source in reasons.get(digest, [])
                        ]
                        conn.execute(
                            """
                            INSERT INTO gc_items(
                                run_id, digest, action, reason, ref_count
                            ) VALUES(?, ?, 'retained', ?, ?)
                            """,
                            (
                                run_id,
                                digest,
                                _stable_json(reason_payload),
                                refs.get(digest, 0),
                            ),
                        )
                    self._set_meta(conn, "gc_sequence", gc_sequence)
                self._maybe_crash("gc:after_plan")

                deleted: list[str] = []
                absent: list[str] = []
                for index, digest in enumerate(candidates):
                    if self.storage.quarantine_and_remove(digest, run_id):
                        deleted.append(digest)
                    else:
                        absent.append(digest)
                    if index == 0:
                        self._maybe_crash("gc:mid_delete")
                self._maybe_crash("gc:before_finalize")

                with transaction(conn, immediate=True):
                    for digest in deleted:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO gc_items(
                                run_id, digest, action, reason, ref_count
                            ) VALUES(?, ?, 'deleted', 'zero_references', 0)
                            """,
                            (run_id, digest),
                        )
                    for digest in absent:
                        conn.execute(
                            """
                            INSERT OR REPLACE INTO gc_items(
                                run_id, digest, action, reason, ref_count
                            ) VALUES(?, ?, 'absent', 'zero_references', 0)
                            """,
                            (run_id, digest),
                        )
                    conn.execute(
                        """
                        UPDATE gc_runs
                        SET status = 'completed', reclaimed_count = ?,
                            retained_count = ?, scanned_count = ?
                        WHERE run_id = ?
                        """,
                        (len(deleted), len(retained), len(disk_digests), run_id),
                    )
                    new_version = version + 1
                    self._set_meta(conn, "chain_version", new_version)
                self._maybe_crash("gc:after_commit")
                return {
                    "ok": True,
                    "command": "gc",
                    "status": "completed",
                    "run_id": run_id,
                    "view_version": version,
                    "chain_version": new_version,
                    "scanned_blocks": len(disk_digests),
                    "reclaimed_blocks": len(deleted),
                    "already_absent_blocks": len(absent),
                    "retained_blocks": len(retained),
                    "reclaimed_digests": sorted(deleted),
                    "absent_digests": sorted(absent),
                    "retained_reasons": [
                        {
                            "digest": digest,
                            "ref_count": refs.get(digest, 0),
                            "sources": reasons.get(digest, []),
                        }
                        for digest in sorted(retained)
                    ],
                }
            finally:
                conn.close()

    def resume(self) -> dict[str, Any]:
        with self._mutation_lock():
            report = self._recover_with_lock()
            conn = self._connect_ro()
            try:
                version = int(self._meta(conn, "chain_version"))
                current = self._meta(conn, "current_snapshot_id")
                refs, _reasons = self._reference_summary(conn)
                disk = self.storage.list_digests()
                unreferenced = [digest for digest in disk if refs.get(digest, 0) == 0]
                pending = conn.execute(
                    """
                    SELECT operation_id, kind, phase FROM operations
                    WHERE status = 'pending' ORDER BY operation_id
                    """
                ).fetchall()
                running_gc = conn.execute(
                    "SELECT run_id FROM gc_runs WHERE status = 'running' "
                    "ORDER BY run_id"
                ).fetchall()
                return {
                    "ok": True,
                    "command": "resume",
                    "status": "recovered",
                    "aborted_operations": report["aborted_operations"],
                    "completed_gc_runs": report["completed_gc_runs"],
                    "removed_temp_files": report["removed_temp_files"],
                    "removed_trash_files": report["removed_trash_files"],
                    "unreferenced_block_count": len(unreferenced),
                    "unreferenced_digests": sorted(unreferenced),
                    "pending_operation_count": len(pending),
                    "running_gc_count": len(running_gc),
                    "chain_version": version,
                    "current_snapshot_id": current,
                }
            finally:
                conn.close()

    def status(self) -> dict[str, Any]:
        conn = self._connect_ro()
        try:
            with transaction(conn):
                version = int(self._meta(conn, "chain_version"))
                watermark = int(self._meta(conn, "clock_watermark"))
                current_id = self._meta(conn, "current_snapshot_id")
                snapshots = conn.execute(
                    "SELECT * FROM snapshots ORDER BY sequence, snapshot_id"
                ).fetchall()
                active_ids = set(self._ancestors(conn, current_id))
                snapshot_output = []
                for row in snapshots:
                    snapshot_id = str(row["snapshot_id"])
                    snapshot_output.append(
                        {
                            "snapshot_id": snapshot_id,
                            "sequence": int(row["sequence"]),
                            "parent_id": row["parent_id"],
                            "created_clock_ms": int(row["created_clock_ms"]),
                            "status": str(row["status"]),
                            "is_base": bool(row["is_base"]),
                            "visible_hash": str(row["visible_hash"]),
                            "orphan_reason": row["orphan_reason"],
                            "orphaned_by": row["orphaned_by"],
                            "on_current_branch": snapshot_id in active_ids,
                        }
                    )
                volume_rows = conn.execute(
                    "SELECT block_index, digest, owner_snapshot_id "
                    "FROM volume_blocks ORDER BY block_index"
                ).fetchall()
                counts, sources = self._reference_summary(conn)
                disk_digests = self.storage.list_digests()
                disk_set = set(disk_digests)
                ref_set = set(counts)
                pending = conn.execute(
                    """
                    SELECT operation_id, kind, phase, snapshot_id, request_id
                    FROM operations WHERE status = 'pending'
                    ORDER BY operation_id
                    """
                ).fetchall()
                gc_total_row = conn.execute(
                    "SELECT COALESCE(SUM(reclaimed_count), 0) AS total "
                    "FROM gc_runs WHERE status = 'completed'"
                ).fetchone()
                last_gc = conn.execute(
                    """
                    SELECT * FROM gc_runs WHERE status = 'completed'
                    ORDER BY view_version DESC, run_id DESC LIMIT 1
                    """
                ).fetchone()
                last_gc_items = []
                if last_gc is not None:
                    item_rows = conn.execute(
                        """
                        SELECT digest, action, reason, ref_count
                        FROM gc_items WHERE run_id = ?
                        ORDER BY digest, action
                        """,
                        (str(last_gc["run_id"]),),
                    ).fetchall()
                    last_gc_items = [
                        {
                            "digest": str(item["digest"]),
                            "action": str(item["action"]),
                            "reason": str(item["reason"]),
                            "ref_count": int(item["ref_count"]),
                        }
                        for item in item_rows
                    ]
                decision_rows = conn.execute(
                    """
                    SELECT rollback_id, snapshot_id, action, reason, previous_status
                    FROM fork_decisions
                    ORDER BY rollback_id, snapshot_id
                    """
                ).fetchall()
                rollback_rows = conn.execute(
                    """
                    SELECT request_id, target_snapshot_id, clock_ms,
                           retain_after, view_version
                    FROM rollback_requests ORDER BY request_id
                    """
                ).fetchall()
                decisions_by_rollback: dict[str, list[dict[str, str]]] = {}
                for row in decision_rows:
                    decisions_by_rollback.setdefault(str(row["rollback_id"]), []).append(
                        {
                            "snapshot_id": str(row["snapshot_id"]),
                            "action": str(row["action"]),
                            "reason": str(row["reason"]),
                            "previous_status": str(row["previous_status"]),
                        }
                    )
                rollback_history = [
                    {
                        "request_id": str(row["request_id"]),
                        "target_snapshot_id": str(row["target_snapshot_id"]),
                        "clock_ms": int(row["clock_ms"]),
                        "retain_after": bool(row["retain_after"]),
                        "view_version": int(row["view_version"]),
                        "fork_decisions": decisions_by_rollback.get(
                            str(row["request_id"]), []
                        ),
                    }
                    for row in rollback_rows
                ]
                return {
                    "ok": True,
                    "command": "status",
                    "volume": {
                        "id": self._meta(conn, "volume_id"),
                        "block_size": int(self._meta(conn, "block_size")),
                        "block_count": int(self._meta(conn, "block_count")),
                    },
                    "chain_version": version,
                    "clock_watermark": watermark,
                    "current_snapshot_id": current_id,
                    "active_branch": sorted(
                        active_ids,
                        key=lambda item: next(
                            int(row["sequence"])
                            for row in snapshots
                            if str(row["snapshot_id"]) == item
                        ),
                    ),
                    "snapshots": snapshot_output,
                    "volume_blocks": [
                        {
                            "block": int(row["block_index"]),
                            "digest": str(row["digest"]),
                            "owner_snapshot_id": str(row["owner_snapshot_id"]),
                        }
                        for row in volume_rows
                    ],
                    "reference_counts": [
                        {
                            "digest": digest,
                            "count": counts[digest],
                            "sources": sources.get(digest, []),
                        }
                        for digest in sorted(counts)
                    ],
                    "block_objects": {
                        "total": len(disk_digests),
                        "referenced": len(ref_set.intersection(disk_set)),
                        "unreferenced": len(disk_set - ref_set),
                        "missing_referenced": len(ref_set - disk_set),
                        "unreferenced_digests": sorted(disk_set - ref_set),
                        "missing_referenced_digests": sorted(ref_set - disk_set),
                    },
                    "gc": {
                        "reclaimed_blocks_total": int(gc_total_row["total"]),
                        "last_run": None
                        if last_gc is None
                        else {
                            "run_id": str(last_gc["run_id"]),
                            "view_version": int(last_gc["view_version"]),
                            "reclaimed_count": int(last_gc["reclaimed_count"]),
                            "retained_count": int(last_gc["retained_count"]),
                            "scanned_count": int(last_gc["scanned_count"]),
                            "items": last_gc_items,
                        },
                    },
                    "pending_operations": [
                        {
                            "operation_id": str(row["operation_id"]),
                            "kind": str(row["kind"]),
                            "phase": str(row["phase"]),
                            "snapshot_id": row["snapshot_id"],
                            "request_id": row["request_id"],
                        }
                        for row in pending
                    ],
                    "rollback_history": rollback_history,
                }
        finally:
            conn.close()

    def verify(self) -> dict[str, Any]:
        conn = self._connect_ro()
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []
        try:
            with transaction(conn):
                integrity = conn.execute("PRAGMA integrity_check").fetchall()
                integrity_ok = len(integrity) == 1 and str(integrity[0][0]) == "ok"
                checks.append(
                    {"name": "sqlite_integrity", "ok": integrity_ok}
                )
                if not integrity_ok:
                    errors.append(
                        {
                            "code": "SQLITE_INTEGRITY",
                            "details": [str(row[0]) for row in integrity],
                        }
                    )

                required_meta = {
                    "volume_id",
                    "block_size",
                    "block_count",
                    "base_snapshot_id",
                    "current_snapshot_id",
                    "chain_version",
                    "clock_watermark",
                }
                present_meta = {
                    str(row["key"])
                    for row in conn.execute("SELECT key FROM meta").fetchall()
                }
                missing_meta = sorted(required_meta - present_meta)
                checks.append(
                    {
                        "name": "required_metadata",
                        "ok": not missing_meta,
                    }
                )
                if missing_meta:
                    errors.append(
                        {
                            "code": "MISSING_METADATA",
                            "details": missing_meta,
                        }
                    )
                    raise _error(
                        "VERIFY_FAILED",
                        "store cannot be verified because metadata is missing",
                        errors=errors,
                    )

                block_count = int(self._meta(conn, "block_count"))
                block_size = int(self._meta(conn, "block_size"))
                current_id = self._meta(conn, "current_snapshot_id")
                snapshot_rows = conn.execute(
                    "SELECT * FROM snapshots ORDER BY sequence, snapshot_id"
                ).fetchall()
                snapshot_ids = {str(row["snapshot_id"]) for row in snapshot_rows}
                parents = {
                    str(row["snapshot_id"]): row["parent_id"] for row in snapshot_rows
                }
                statuses = {
                    str(row["snapshot_id"]): str(row["status"]) for row in snapshot_rows
                }
                current_row = next(
                    (row for row in snapshot_rows if str(row["snapshot_id"]) == current_id),
                    None,
                )
                current_ok = current_row is not None and statuses.get(current_id) == "alive"
                checks.append({"name": "current_snapshot_alive", "ok": current_ok})
                if not current_ok:
                    errors.append(
                        {"code": "CURRENT_SNAPSHOT_INVALID", "details": current_id}
                    )

                parent_ok = True
                for snapshot_id, parent_id in parents.items():
                    if parent_id is not None and str(parent_id) not in snapshot_ids:
                        parent_ok = False
                        errors.append(
                            {
                                "code": "MISSING_PARENT",
                                "details": {
                                    "snapshot_id": snapshot_id,
                                    "parent_id": parent_id,
                                },
                            }
                        )
                    if (
                        parent_id is not None
                        and statuses.get(str(parent_id)) == "orphan"
                        and statuses.get(snapshot_id) == "alive"
                    ):
                        parent_ok = False
                        errors.append(
                            {
                                "code": "ALIVE_CHILD_OF_ORPHAN",
                                "details": {
                                    "snapshot_id": snapshot_id,
                                    "parent_id": parent_id,
                                },
                            }
                        )
                checks.append({"name": "parent_links", "ok": parent_ok})

                writes_by_snapshot: dict[str, dict[int, str]] = {}
                for row in conn.execute(
                    "SELECT snapshot_id, block_index, digest FROM snapshot_writes"
                ).fetchall():
                    writes_by_snapshot.setdefault(
                        str(row["snapshot_id"]), {}
                    )[int(row["block_index"])] = str(row["digest"])
                stored_blocks: dict[str, dict[int, tuple[str, str]]] = {}
                for row in conn.execute(
                    """
                    SELECT snapshot_id, block_index, digest, owner_snapshot_id
                    FROM snapshot_blocks
                    """
                ).fetchall():
                    stored_blocks.setdefault(str(row["snapshot_id"]), {})[
                        int(row["block_index"])
                    ] = (str(row["digest"]), str(row["owner_snapshot_id"]))
                resolved: dict[str, dict[int, tuple[str, str]]] = {}
                resolving: set[str] = set()

                def resolve(snapshot_id: str) -> dict[int, tuple[str, str]]:
                    if snapshot_id in resolved:
                        return resolved[snapshot_id]
                    if snapshot_id in resolving:
                        raise _error(
                            "STATE_CORRUPT",
                            "snapshot parent cycle detected during verification",
                            snapshot_id=snapshot_id,
                        )
                    if snapshot_id not in snapshot_ids:
                        raise _error(
                            "STATE_CORRUPT",
                            "snapshot row is missing",
                            snapshot_id=snapshot_id,
                        )
                    resolving.add(snapshot_id)
                    parent_id = parents[snapshot_id]
                    if parent_id is None or str(parent_id) not in snapshot_ids:
                        inherited: dict[int, tuple[str, str]] = {}
                    else:
                        inherited = dict(resolve(str(parent_id)))
                    own = writes_by_snapshot.get(snapshot_id, {})
                    for block_index, digest in own.items():
                        inherited[block_index] = (digest, snapshot_id)
                    resolving.remove(snapshot_id)
                    resolved[snapshot_id] = inherited
                    return inherited

                chain_ok = True
                for row in snapshot_rows:
                    snapshot_id = str(row["snapshot_id"])
                    visible = resolve(snapshot_id)
                    visible_digest_map = {
                        block_index: visible[block_index][0]
                        for block_index in visible
                    }
                    expected_hash = canonical_visible_hash(visible_digest_map)
                    view = stored_blocks.get(snapshot_id, {})
                    if len(view) != block_count:
                        chain_ok = False
                        errors.append(
                            {
                                "code": "INCOMPLETE_SNAPSHOT_VIEW",
                                "details": {
                                    "snapshot_id": snapshot_id,
                                    "expected_blocks": block_count,
                                    "actual_blocks": len(view),
                                },
                            }
                        )
                    if view != visible:
                        chain_ok = False
                        errors.append(
                            {
                                "code": "VIEW_CHAIN_MISMATCH",
                                "details": {"snapshot_id": snapshot_id},
                            }
                        )
                    if str(row["visible_hash"]) != expected_hash:
                        chain_ok = False
                        errors.append(
                            {
                                "code": "VISIBLE_HASH_MISMATCH",
                                "details": {
                                    "snapshot_id": snapshot_id,
                                    "expected": expected_hash,
                                    "actual": str(row["visible_hash"]),
                                },
                            }
                        )
                checks.append({"name": "snapshot_chain_views", "ok": chain_ok})

                volume_rows = conn.execute(
                    """
                    SELECT block_index, digest, owner_snapshot_id
                    FROM volume_blocks ORDER BY block_index
                    """
                ).fetchall()
                volume_map = {
                    int(row["block_index"]): (
                        str(row["digest"]),
                        str(row["owner_snapshot_id"]),
                    )
                    for row in volume_rows
                }
                current_view = resolved.get(current_id, {})
                volume_ok = (
                    len(volume_map) == block_count and volume_map == current_view
                )
                checks.append({"name": "volume_matches_current", "ok": volume_ok})
                if not volume_ok:
                    errors.append(
                        {
                            "code": "VOLUME_CURRENT_MISMATCH",
                            "details": {"current_snapshot_id": current_id},
                        }
                    )

                expected_refs: set[tuple[str, str, int, str]] = set()
                volume_id = self._meta(conn, "volume_id")
                for block_index, (digest, _owner) in volume_map.items():
                    expected_refs.add(("volume", volume_id, block_index, digest))
                for snapshot_id, status in statuses.items():
                    if status != "alive":
                        continue
                    for block_index, (digest, _owner) in resolved.get(
                        snapshot_id, {}
                    ).items():
                        expected_refs.add(
                            ("snapshot", snapshot_id, block_index, digest)
                        )
                for row in conn.execute(
                    """
                    SELECT o.operation_id, r.block_index, r.digest
                    FROM operations o JOIN operation_refs r
                      ON o.operation_id = r.operation_id
                    WHERE o.status = 'pending'
                    """
                ).fetchall():
                    expected_refs.add(
                        (
                            "transaction",
                            str(row["operation_id"]),
                            int(row["block_index"]),
                            str(row["digest"]),
                        )
                    )
                actual_refs = {
                    (
                        str(row["source_kind"]),
                        str(row["source_id"]),
                        int(row["block_index"]),
                        str(row["digest"]),
                    )
                    for row in conn.execute(
                        """
                        SELECT source_kind, source_id, block_index, digest
                        FROM block_refs
                        """
                    ).fetchall()
                }
                refs_ok = actual_refs == expected_refs
                checks.append({"name": "reference_sources", "ok": refs_ok})
                if not refs_ok:
                    errors.append(
                        {
                            "code": "REFERENCE_MISMATCH",
                            "details": {
                                "missing": sorted(
                                    [list(item) for item in expected_refs - actual_refs]
                                ),
                                "unexpected": sorted(
                                    [list(item) for item in actual_refs - expected_refs]
                                ),
                            },
                        }
                    )

                referenced_digests = {item[3] for item in actual_refs}
                disk_digests = set(self.storage.list_digests())
                file_errors: list[dict[str, Any]] = []
                for digest in sorted(referenced_digests):
                    if digest not in disk_digests:
                        file_errors.append(
                            {"code": "MISSING_BLOCK_FILE", "digest": digest}
                        )
                    elif not self.storage.verify_file(digest, expected_size=block_size):
                        file_errors.append(
                            {"code": "BLOCK_FILE_HASH_MISMATCH", "digest": digest}
                        )
                unreferenced = sorted(disk_digests - referenced_digests)
                for digest in unreferenced:
                    if not self.storage.verify_file(digest, expected_size=block_size):
                        file_errors.append(
                            {
                                "code": "UNREFERENCED_BLOCK_FILE_HASH_MISMATCH",
                                "digest": digest,
                            }
                        )
                for name in self.storage.list_unexpected_files():
                    file_errors.append(
                        {"code": "UNEXPECTED_BLOCK_FILE", "name": name}
                    )
                checks.append(
                    {
                        "name": "block_files",
                        "ok": not file_errors,
                    }
                )
                errors.extend(file_errors)
                if unreferenced:
                    warnings.append(
                        {
                            "code": "UNREFERENCED_BLOCK_FILES",
                            "digests": unreferenced,
                        }
                    )

                orphan_refs = conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM block_refs r
                    JOIN snapshots s ON r.source_kind = 'snapshot'
                     AND r.source_id = s.snapshot_id
                    WHERE s.status = 'orphan'
                    """
                ).fetchone()
                orphan_ref_count = int(orphan_refs["count"])
                checks.append(
                    {
                        "name": "orphan_snapshot_refs_absent",
                        "ok": orphan_ref_count == 0,
                    }
                )
                if orphan_ref_count:
                    errors.append(
                        {
                            "code": "ORPHAN_REFS_PRESENT",
                            "details": orphan_ref_count,
                        }
                    )

                pending_count = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM operations "
                        "WHERE status = 'pending'"
                    ).fetchone()["count"]
                )
                running_gc_count = int(
                    conn.execute(
                        "SELECT COUNT(*) AS count FROM gc_runs "
                        "WHERE status = 'running'"
                    ).fetchone()["count"]
                )
                if pending_count:
                    warnings.append(
                        {
                            "code": "PENDING_OPERATIONS",
                            "count": pending_count,
                        }
                    )
                if running_gc_count:
                    warnings.append(
                        {
                            "code": "RUNNING_GC",
                            "count": running_gc_count,
                        }
                    )
                temp_files = (
                    len([item for item in self.storage.tmp_dir.iterdir() if item.is_file()])
                    if self.storage.tmp_dir.is_dir()
                    else 0
                )
                if temp_files:
                    warnings.append(
                        {
                            "code": "TEMPORARY_FILES_PRESENT",
                            "count": temp_files,
                        }
                    )

                result = {
                    "ok": not errors,
                    "command": "verify",
                    "checks": checks,
                    "errors": errors,
                    "warnings": warnings,
                    "stats": {
                        "chain_version": int(self._meta(conn, "chain_version")),
                        "snapshots": len(snapshot_rows),
                        "alive_snapshots": sum(
                            1 for status in statuses.values() if status == "alive"
                        ),
                        "orphan_snapshots": sum(
                            1 for status in statuses.values() if status == "orphan"
                        ),
                        "referenced_blocks": len(actual_refs),
                        "referenced_digests": len(referenced_digests),
                        "block_objects": len(disk_digests),
                        "unreferenced_block_objects": len(unreferenced),
                    },
                }
                return result
        finally:
            conn.close()
