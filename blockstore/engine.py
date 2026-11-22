from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .blockstore import ContentAddressedBlockStore
from .canonical import canonical_json, sha256_bytes, sha256_text
from .db import (
    connect,
    get_meta,
    initialize_schema,
    set_meta,
    table_exists,
)
from .errors import BlockStoreError, ErrorCode
from .filelock import InterProcessLock
from .models import (
    BlockData,
    PlanBundle,
    RollbackRequest,
    SnapshotSpec,
    VolumeSpec,
    base_snapshot_id,
    parse_rollback_document,
    parse_snapshot_document,
)


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def _maybe_crash(point: str) -> None:
    if os.environ.get("BLOCKSTORE_CRASH_POINT") == point:
        print(f"[fault-injection] crash at {point}", file=sys.stderr, flush=True)
        os._exit(97)


class SnapshotEngine:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.db_path = self.root / "state.sqlite3"
        self.blocks = ContentAddressedBlockStore(self.root)
        self.lock = InterProcessLock(self.root / ".lock")

    # ------------------------------------------------------------------
    # Initialization and plan loading
    # ------------------------------------------------------------------
    def load_plan(
        self,
        bundle: PlanBundle,
        *,
        apply_plan: bool = True,
    ) -> dict[str, Any]:
        with self.lock:
            initialized_now = self._initialize_if_needed_locked(bundle.volume)
            if not initialized_now:
                self._assert_volume_matches_locked(bundle.volume)
            recovered = self._recover_locked()
            applied: list[dict[str, Any]] = []
            if apply_plan:
                order = sorted(
                    bundle.snapshots,
                    key=lambda spec: (spec.created_clock, spec.seq, spec.snapshot_id),
                )
                for spec in order:
                    applied.append(self._snapshot_locked(spec))
            status = self._status_unlocked()
            return {
                "ok": True,
                "operation": "load",
                "initialized": initialized_now,
                "applied_snapshots": applied,
                "recovery": recovered,
                "status": status,
            }

    def _initialize_if_needed_locked(self, volume: VolumeSpec) -> bool:
        connection: sqlite3.Connection | None = None
        if self.db_path.exists():
            connection = connect(self.db_path)
            if table_exists(connection, "volumes"):
                volume_row = connection.execute(
                    "SELECT * FROM volumes LIMIT 1"
                ).fetchone()
                if volume_row is not None:
                    connection.close()
                    return False
        self.root.mkdir(parents=True, exist_ok=True)
        self.blocks.ensure_directories()
        if connection is None:
            connection = connect(self.db_path)
        try:
            initialize_schema(connection)
            volume_row = connection.execute(
                "SELECT * FROM volumes LIMIT 1"
            ).fetchone()
            if volume_row is not None:
                return False
            self._assert_volume_matches_locked(volume, allow_empty=True)
            for block in volume.blocks.values():
                self.blocks.materialize(block.digest, block.data)
            base_id = base_snapshot_id(volume.volume_id)
            base_manifest = {
                "snapshot_id": base_id,
                "seq": 0,
                "volume_id": volume.volume_id,
                "parent_snapshot_id": None,
                "created_clock": volume.created_clock,
                "covered_start": None,
                "covered_end": None,
                "blocks": [
                    {
                        "block_no": block_no,
                        "digest": volume.blocks[block_no].digest,
                    }
                    for block_no in sorted(volume.blocks)
                ],
            }
            base_manifest_json = canonical_json(base_manifest)
            with _transaction(connection):
                connection.execute(
                    "INSERT INTO volumes(volume_id, block_size, block_count, created_clock) "
                    "VALUES(?, ?, ?, ?)",
                    (
                        volume.volume_id,
                        volume.block_size,
                        volume.block_count,
                        volume.created_clock,
                    ),
                )
                connection.execute(
                    "INSERT INTO current_view("
                    "singleton, volume_id, view_version, logical_clock, anchor_snapshot_id"
                    ") VALUES(1, ?, 1, ?, ?)",
                    (volume.volume_id, volume.created_clock, base_id),
                )
                connection.execute(
                    "INSERT INTO snapshots("
                    "snapshot_id, volume_id, seq, parent_snapshot_id, created_clock, "
                    "status, is_base, covered_start, covered_end, manifest_json, "
                    "manifest_digest, view_version"
                    ") VALUES(?, ?, 0, NULL, ?, 'live', 1, NULL, NULL, ?, ?, 1)",
                    (
                        base_id,
                        volume.volume_id,
                        volume.created_clock,
                        base_manifest_json,
                        sha256_text(base_manifest_json),
                    ),
                )
                for block_no in sorted(volume.blocks):
                    block = volume.blocks[block_no]
                    connection.execute(
                        "INSERT INTO snapshot_blocks(snapshot_id, block_no, digest, source) "
                        "VALUES(?, ?, ?, 'base')",
                        (base_id, block_no, block.digest),
                    )
                    connection.execute(
                        "INSERT INTO volume_blocks("
                        "block_no, volume_id, digest, owner_snapshot_id"
                        ") VALUES(?, ?, ?, ?)",
                        (block_no, volume.volume_id, block.digest, base_id),
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO blocks(digest, size) VALUES(?, ?)",
                        (block.digest, len(block.data)),
                    )
                set_meta(connection, "volume_spec_digest", volume.digest)
                self._rebuild_refs(connection)
            return True
        finally:
            connection.close()

    def _assert_volume_matches_locked(
        self,
        volume: VolumeSpec,
        *,
        allow_empty: bool = False,
    ) -> None:
        if not self.db_path.exists():
            if allow_empty:
                return
            raise BlockStoreError(
                ErrorCode.STORAGE_NOT_INITIALIZED,
                "storage is not initialized",
                {"root": str(self.root)},
            )
        connection = connect(self.db_path)
        try:
            if not table_exists(connection, "volumes"):
                if allow_empty:
                    return
                raise BlockStoreError(
                    ErrorCode.STORAGE_NOT_INITIALIZED,
                    "storage database has no schema",
                    {"db_path": str(self.db_path)},
                )
            row = connection.execute("SELECT * FROM volumes LIMIT 1").fetchone()
            if row is None:
                if allow_empty:
                    return
                raise BlockStoreError(
                    ErrorCode.STORAGE_NOT_INITIALIZED,
                    "storage database has no volume",
                    {"db_path": str(self.db_path)},
                )
            actual = {
                "volume_id": row["volume_id"],
                "block_size": row["block_size"],
                "block_count": row["block_count"],
                "created_clock": row["created_clock"],
            }
            expected = {
                "volume_id": volume.volume_id,
                "block_size": volume.block_size,
                "block_count": volume.block_count,
                "created_clock": volume.created_clock,
            }
            if actual != expected:
                raise BlockStoreError(
                    ErrorCode.VOLUME_MISMATCH,
                    "input volume does not match initialized storage",
                    {"expected": actual, "actual": expected},
                )
            stored_digest = get_meta(connection, "volume_spec_digest")
            if stored_digest is not None and stored_digest != volume.digest:
                raise BlockStoreError(
                    ErrorCode.VOLUME_MISMATCH,
                    "input volume initial content does not match initialized storage",
                    {
                        "expected_volume_digest": stored_digest,
                        "actual_volume_digest": volume.digest,
                    },
                )
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Snapshot creation
    # ------------------------------------------------------------------
    def create_snapshot(self, spec: SnapshotSpec) -> dict[str, Any]:
        with self.lock:
            self._require_initialized_locked()
            recovered = self._recover_locked()
            result = self._snapshot_locked(spec)
            return {"ok": True, "operation": "snapshot", "recovery": recovered, **result}

    def _snapshot_locked(self, spec: SnapshotSpec) -> dict[str, Any]:
        connection = connect(self.db_path)
        try:
            existing = connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?",
                (spec.snapshot_id,),
            ).fetchone()
            if existing is not None:
                if existing["manifest_digest"] == spec.manifest_digest:
                    return {
                        "snapshot_id": spec.snapshot_id,
                        "seq": spec.seq,
                        "view_version": existing["view_version"],
                        "manifest_digest": spec.manifest_digest,
                        "idempotent": True,
                        "created": False,
                    }
                raise BlockStoreError(
                    ErrorCode.SNAPSHOT_CONFLICT,
                    "snapshot_id already exists with different content",
                    {
                        "snapshot_id": spec.snapshot_id,
                        "existing_manifest_digest": existing["manifest_digest"],
                        "incoming_manifest_digest": spec.manifest_digest,
                    },
                )
            self._validate_snapshot_spec(connection, spec)
            view = connection.execute("SELECT * FROM current_view").fetchone()
            assert view is not None
            seq_row = connection.execute(
                "SELECT snapshot_id FROM snapshots WHERE volume_id = ? AND seq = ?",
                (spec.volume_id, spec.seq),
            ).fetchone()
            if seq_row is not None:
                raise BlockStoreError(
                    ErrorCode.SNAPSHOT_CONFLICT,
                    "snapshot seq is already committed",
                    {
                        "seq": spec.seq,
                        "existing_snapshot_id": seq_row["snapshot_id"],
                    },
                )
            run = connection.execute(
                "SELECT * FROM snapshot_runs WHERE snapshot_id = ?",
                (spec.snapshot_id,),
            ).fetchone()
            if run is not None and run["manifest_digest"] != spec.manifest_digest:
                raise BlockStoreError(
                    ErrorCode.SNAPSHOT_CONFLICT,
                    "prepared snapshot run has different content",
                    {
                        "snapshot_id": spec.snapshot_id,
                        "existing_manifest_digest": run["manifest_digest"],
                        "incoming_manifest_digest": spec.manifest_digest,
                    },
                )
            if run is not None and run["phase"] == "prepared":
                return self._complete_snapshot_run(spec, run)
            if run is not None and run["phase"] == "applied":
                raise BlockStoreError(
                    ErrorCode.RECOVERY_FAILED,
                    "snapshot run is applied but snapshot metadata is missing",
                    {"snapshot_id": spec.snapshot_id},
                )
            for block in spec.blocks.values():
                if self.blocks.path_for(block.digest).exists():
                    self.blocks.verify(block.digest)
            parent_rows = connection.execute(
                "SELECT block_no, digest FROM snapshot_blocks "
                "WHERE snapshot_id = ? ORDER BY block_no",
                (spec.parent_snapshot_id,),
            ).fetchall()
            if not parent_rows:
                raise BlockStoreError(
                    ErrorCode.RECOVERY_FAILED,
                    "parent snapshot has no block ownership mapping",
                    {"parent_snapshot_id": spec.parent_snapshot_id},
                )
            prepared_visible = {
                int(row["block_no"]): str(row["digest"]) for row in parent_rows
            }
            for block_no, block in spec.blocks.items():
                prepared_visible[block_no] = block.digest
            prepared_spec_json = canonical_json(spec.spec_dict())
            prepared_view_version = int(view["view_version"])
            with _transaction(connection):
                connection.execute(
                    "INSERT INTO snapshot_runs("
                    "snapshot_id, volume_id, seq, parent_snapshot_id, created_clock, "
                    "manifest_digest, spec_json, phase, result_view_version"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, 'prepared', NULL)",
                    (
                        spec.snapshot_id,
                        spec.volume_id,
                        spec.seq,
                        spec.parent_snapshot_id,
                        spec.created_clock,
                        spec.manifest_digest,
                        prepared_spec_json,
                    ),
                )
                for block_no, digest in prepared_visible.items():
                    connection.execute(
                        "INSERT INTO transaction_refs(source_id, block_no, digest) "
                        "VALUES(?, ?, ?)",
                        (spec.snapshot_id, block_no, digest),
                    )
                self._rebuild_refs(connection)
            _maybe_crash("snapshot:prepared")
            for block_no in sorted(spec.blocks):
                block = spec.blocks[block_no]
                self.blocks.materialize(block.digest, block.data)
            _maybe_crash("snapshot:materialized")
            run = connection.execute(
                "SELECT * FROM snapshot_runs WHERE snapshot_id = ?",
                (spec.snapshot_id,),
            ).fetchone()
            assert run is not None
            result = self._complete_snapshot_run(spec, run)
            result["prepared_view_version"] = prepared_view_version
            _maybe_crash("snapshot:committed")
            return result
        finally:
            connection.close()

    def _validate_snapshot_spec(
        self,
        connection: sqlite3.Connection,
        spec: SnapshotSpec,
    ) -> None:
        volume = connection.execute("SELECT * FROM volumes LIMIT 1").fetchone()
        if volume is None:
            raise BlockStoreError(
                ErrorCode.STORAGE_NOT_INITIALIZED,
                "storage has no volume",
            )
        if spec.volume_id != volume["volume_id"]:
            raise BlockStoreError(
                ErrorCode.VOLUME_MISMATCH,
                "snapshot belongs to another volume",
                {
                    "expected_volume_id": volume["volume_id"],
                    "actual_volume_id": spec.volume_id,
                },
            )
        if spec.covered_start < 0 or spec.covered_end >= volume["block_count"]:
            raise BlockStoreError(
                ErrorCode.BLOCK_RANGE_INVALID,
                "snapshot covered range is outside the volume",
                {
                    "covered_start": spec.covered_start,
                    "covered_end": spec.covered_end,
                    "block_count": volume["block_count"],
                },
            )
        expected = set(range(spec.covered_start, spec.covered_end + 1))
        if set(spec.blocks) != expected:
            raise BlockStoreError(
                ErrorCode.COVERAGE_MISMATCH,
                "snapshot blocks do not match its covered range",
                {
                    "missing_blocks": sorted(expected - set(spec.blocks)),
                    "extra_blocks": sorted(set(spec.blocks) - expected),
                },
            )
        for block_no, block in spec.blocks.items():
            if block.block_no != block_no:
                raise BlockStoreError(
                    ErrorCode.INVALID_BLOCK_NUMBER,
                    "block entry is stored under the wrong block number",
                    {"mapping_key": block_no, "block_no": block.block_no},
                )
            if len(block.data) != volume["block_size"]:
                raise BlockStoreError(
                    ErrorCode.INVALID_BLOCK_DATA,
                    "snapshot block length does not equal volume block size",
                    {
                        "block_no": block_no,
                        "expected_length": volume["block_size"],
                        "actual_length": len(block.data),
                    },
                )
            actual = sha256_bytes(block.data)
            if actual != block.digest:
                raise BlockStoreError(
                    ErrorCode.DIGEST_MISMATCH,
                    "snapshot block digest does not match its content",
                    {"block_no": block_no, "expected": block.digest, "actual": actual},
                )
        parent = connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id = ?",
            (spec.parent_snapshot_id,),
        ).fetchone()
        if parent is None:
            raise BlockStoreError(
                ErrorCode.PARENT_NOT_FOUND,
                "parent snapshot does not exist",
                {
                    "snapshot_id": spec.snapshot_id,
                    "parent_snapshot_id": spec.parent_snapshot_id,
                },
            )
        if parent["status"] != "live":
            raise BlockStoreError(
                ErrorCode.PARENT_ORPHANED,
                "cannot create a snapshot from an orphaned parent",
                {
                    "snapshot_id": spec.snapshot_id,
                    "parent_snapshot_id": spec.parent_snapshot_id,
                },
            )
        self._verify_snapshot_block_files(connection, spec.parent_snapshot_id)
        if spec.created_clock <= parent["created_clock"]:
            raise BlockStoreError(
                ErrorCode.CLOCK_NOT_MONOTONIC,
                "snapshot clock must be strictly later than its parent",
                {
                    "created_clock": spec.created_clock,
                    "parent_clock": parent["created_clock"],
                },
            )
        view = connection.execute("SELECT * FROM current_view").fetchone()
        if view is not None and spec.created_clock < view["logical_clock"]:
            raise BlockStoreError(
                ErrorCode.CLOCK_NOT_MONOTONIC,
                "snapshot clock is behind the persisted logical clock watermark",
                {
                    "created_clock": spec.created_clock,
                    "logical_clock": view["logical_clock"],
                },
            )

    def _complete_snapshot_run(
        self,
        spec: SnapshotSpec,
        run: sqlite3.Row,
    ) -> dict[str, Any]:
        if run["phase"] == "applied":
            connection = connect(self.db_path)
            try:
                snapshot = connection.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id = ?",
                    (spec.snapshot_id,),
                ).fetchone()
            finally:
                connection.close()
            if snapshot is None:
                raise BlockStoreError(
                    ErrorCode.RECOVERY_FAILED,
                    "snapshot run is marked applied but snapshot is absent",
                    {"snapshot_id": spec.snapshot_id},
                )
            return {
                "snapshot_id": spec.snapshot_id,
                "seq": spec.seq,
                "view_version": snapshot["view_version"],
                "manifest_digest": spec.manifest_digest,
                "idempotent": True,
                "created": False,
            }
        for block_no in sorted(spec.blocks):
            block = spec.blocks[block_no]
            self.blocks.materialize(block.digest, block.data)
        connection = connect(self.db_path)
        try:
            with _transaction(connection):
                current_run = connection.execute(
                    "SELECT * FROM snapshot_runs WHERE snapshot_id = ?",
                    (spec.snapshot_id,),
                ).fetchone()
                if current_run is None:
                    raise BlockStoreError(
                        ErrorCode.RECOVERY_FAILED,
                        "prepared snapshot run disappeared",
                        {"snapshot_id": spec.snapshot_id},
                    )
                if current_run["phase"] == "applied":
                    snapshot = connection.execute(
                        "SELECT * FROM snapshots WHERE snapshot_id = ?",
                        (spec.snapshot_id,),
                    ).fetchone()
                    return {
                        "snapshot_id": spec.snapshot_id,
                        "seq": spec.seq,
                        "view_version": snapshot["view_version"],
                        "manifest_digest": spec.manifest_digest,
                        "idempotent": True,
                        "created": False,
                    }
                existing = connection.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id = ?",
                    (spec.snapshot_id,),
                ).fetchone()
                if existing is not None:
                    if existing["manifest_digest"] != spec.manifest_digest:
                        raise BlockStoreError(
                            ErrorCode.SNAPSHOT_CONFLICT,
                            "snapshot was committed with different content",
                            {"snapshot_id": spec.snapshot_id},
                        )
                    connection.execute(
                        "UPDATE snapshot_runs SET phase = 'applied', "
                        "result_view_version = ? WHERE snapshot_id = ?",
                        (existing["view_version"], spec.snapshot_id),
                    )
                    return {
                        "snapshot_id": spec.snapshot_id,
                        "seq": spec.seq,
                        "view_version": existing["view_version"],
                        "manifest_digest": spec.manifest_digest,
                        "idempotent": True,
                        "created": False,
                    }
                parent = connection.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id = ?",
                    (spec.parent_snapshot_id,),
                ).fetchone()
                if parent is None:
                    raise BlockStoreError(
                        ErrorCode.RECOVERY_FAILED,
                        "prepared snapshot parent no longer exists",
                        {"parent_snapshot_id": spec.parent_snapshot_id},
                    )
                if parent["status"] != "live":
                    raise BlockStoreError(
                        ErrorCode.PARENT_ORPHANED,
                        "prepared snapshot parent is orphaned",
                        {"parent_snapshot_id": spec.parent_snapshot_id},
                    )
                self._verify_snapshot_block_files(
                    connection, spec.parent_snapshot_id
                )
                parent_rows = connection.execute(
                    "SELECT block_no, digest FROM snapshot_blocks "
                    "WHERE snapshot_id = ? ORDER BY block_no",
                    (spec.parent_snapshot_id,),
                ).fetchall()
                parent_map = {
                    int(row["block_no"]): str(row["digest"]) for row in parent_rows
                }
                if not parent_map:
                    raise BlockStoreError(
                        ErrorCode.RECOVERY_FAILED,
                        "parent snapshot has no block ownership mapping",
                        {"parent_snapshot_id": spec.parent_snapshot_id},
                    )
                visible = dict(parent_map)
                for block_no, block in spec.blocks.items():
                    visible[block_no] = block.digest
                view = connection.execute("SELECT * FROM current_view").fetchone()
                assert view is not None
                next_version = int(view["view_version"]) + 1
                connection.execute(
                    "INSERT INTO snapshots("
                    "snapshot_id, volume_id, seq, parent_snapshot_id, created_clock, "
                    "status, is_base, covered_start, covered_end, manifest_json, "
                    "manifest_digest, view_version"
                    ") VALUES(?, ?, ?, ?, ?, 'live', 0, ?, ?, ?, ?, ?)",
                    (
                        spec.snapshot_id,
                        spec.volume_id,
                        spec.seq,
                        spec.parent_snapshot_id,
                        spec.created_clock,
                        spec.covered_start,
                        spec.covered_end,
                        spec.manifest_json,
                        spec.manifest_digest,
                        next_version,
                    ),
                )
                for block_no in sorted(visible):
                    if block_no in spec.blocks:
                        source = "cow"
                    else:
                        source = "inherit"
                    connection.execute(
                        "INSERT INTO snapshot_blocks(snapshot_id, block_no, digest, source) "
                        "VALUES(?, ?, ?, ?)",
                        (spec.snapshot_id, block_no, visible[block_no], source),
                    )
                for block_no, block in spec.blocks.items():
                    connection.execute(
                        "INSERT OR REPLACE INTO blocks(digest, size) VALUES(?, ?)",
                        (block.digest, len(block.data)),
                    )
                connection.execute(
                    "UPDATE snapshot_runs SET phase = 'applied', result_view_version = ? "
                    "WHERE snapshot_id = ?",
                    (next_version, spec.snapshot_id),
                )
                connection.execute(
                    "DELETE FROM transaction_refs WHERE source_id = ?",
                    (spec.snapshot_id,),
                )
                connection.execute(
                    "UPDATE current_view SET view_version = ?, "
                    "logical_clock = MAX(logical_clock, ?) WHERE singleton = 1",
                    (next_version, spec.created_clock),
                )
                self._rebuild_refs(connection)
                return {
                    "snapshot_id": spec.snapshot_id,
                    "seq": spec.seq,
                    "view_version": next_version,
                    "manifest_digest": spec.manifest_digest,
                    "idempotent": False,
                    "created": True,
                }
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------
    def rollback(self, request: RollbackRequest) -> dict[str, Any]:
        with self.lock:
            self._require_initialized_locked()
            recovered = self._recover_locked()
            result = self._rollback_locked(request)
            return {"ok": True, "operation": "rollback", "recovery": recovered, **result}

    def _rollback_locked(self, request: RollbackRequest) -> dict[str, Any]:
        connection = connect(self.db_path)
        try:
            existing = connection.execute(
                "SELECT * FROM rollback_requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != request.payload_json:
                    raise BlockStoreError(
                        ErrorCode.REQUEST_CONFLICT,
                        "rollback request_id was already used with a different payload",
                        {
                            "request_id": request.request_id,
                            "existing_payload": json.loads(existing["payload_json"]),
                            "incoming_payload": request.payload(),
                        },
                    )
                if existing["phase"] == "prepared":
                    return self._complete_rollback_request(request, existing)
                outcome = json.loads(existing["outcome_json"])
                return {**outcome, "idempotent": True, "applied": True}
            view = connection.execute("SELECT * FROM current_view").fetchone()
            target = connection.execute(
                "SELECT * FROM snapshots WHERE snapshot_id = ?",
                (request.target_snapshot_id,),
            ).fetchone()
            if target is None:
                raise BlockStoreError(
                    ErrorCode.ROLLBACK_TARGET_NOT_FOUND,
                    "rollback target snapshot does not exist",
                    {"target_snapshot_id": request.target_snapshot_id},
                )
            if target["status"] != "live":
                raise BlockStoreError(
                    ErrorCode.ROLLBACK_TARGET_ORPHANED,
                    "rollback target snapshot is orphaned",
                    {"target_snapshot_id": request.target_snapshot_id},
                )
            self._verify_snapshot_block_files(
                connection, request.target_snapshot_id
            )
            self._validate_rollback_clock(connection, request, view)
            with _transaction(connection):
                connection.execute(
                    "INSERT INTO rollback_requests("
                    "request_id, target_snapshot_id, rollback_clock, keep_after_target, "
                    "payload_json, phase, prepared_view_version"
                    ") VALUES(?, ?, ?, ?, ?, 'prepared', ?)",
                    (
                        request.request_id,
                        request.target_snapshot_id,
                        request.rollback_clock,
                        1 if request.keep_after_target else 0,
                        request.payload_json,
                        view["view_version"],
                    ),
                )
            _maybe_crash("rollback:prepared")
            current = connection.execute(
                "SELECT * FROM rollback_requests WHERE request_id = ?",
                (request.request_id,),
            ).fetchone()
            assert current is not None
            result = self._complete_rollback_request(request, current)
            _maybe_crash("rollback:committed")
            return result
        finally:
            connection.close()

    def _validate_rollback_clock(
        self,
        connection: sqlite3.Connection,
        request: RollbackRequest,
        view: sqlite3.Row,
    ) -> None:
        if request.rollback_clock < view["logical_clock"]:
            raise BlockStoreError(
                ErrorCode.ROLLBACK_STALE,
                "rollback clock is older than the current logical clock watermark",
                {
                    "request_id": request.request_id,
                    "rollback_clock": request.rollback_clock,
                    "logical_clock": view["logical_clock"],
                },
            )
        if request.rollback_clock == view["logical_clock"]:
            raise BlockStoreError(
                ErrorCode.ROLLBACK_CONFLICT,
                "rollback clock must advance beyond the committed chain view",
                {
                    "request_id": request.request_id,
                    "rollback_clock": request.rollback_clock,
                    "logical_clock": view["logical_clock"],
                    "last_rollback_request_id": view["last_rollback_request_id"],
                },
            )
        target = connection.execute(
            "SELECT created_clock FROM snapshots WHERE snapshot_id = ?",
            (request.target_snapshot_id,),
        ).fetchone()
        if target is None:
            raise BlockStoreError(
                ErrorCode.ROLLBACK_TARGET_NOT_FOUND,
                "rollback target snapshot does not exist",
                {"target_snapshot_id": request.target_snapshot_id},
            )
        if request.rollback_clock <= target["created_clock"]:
            raise BlockStoreError(
                ErrorCode.CLOCK_NOT_MONOTONIC,
                "rollback clock must be strictly later than the target snapshot",
                {
                    "rollback_clock": request.rollback_clock,
                    "target_clock": target["created_clock"],
                },
            )

    def _ancestor_path(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
    ) -> list[str]:
        path: list[str] = []
        current: str | None = snapshot_id
        seen: set[str] = set()
        while current is not None:
            if current in seen:
                raise BlockStoreError(
                    ErrorCode.VERIFY_FAILED,
                    "snapshot parent chain contains a cycle",
                    {"snapshot_id": snapshot_id},
                )
            seen.add(current)
            row = connection.execute(
                "SELECT parent_snapshot_id FROM snapshots WHERE snapshot_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                raise BlockStoreError(
                    ErrorCode.VERIFY_FAILED,
                    "snapshot parent chain is broken",
                    {"snapshot_id": snapshot_id, "missing_ancestor": current},
                )
            path.append(current)
            current = row["parent_snapshot_id"]
        return path

    def _orphan_reference_path(
        self,
        connection: sqlite3.Connection,
        target: str,
        orphan: str,
    ) -> dict[str, Any]:
        target_path = self._ancestor_path(connection, target)
        orphan_path = self._ancestor_path(connection, orphan)
        target_set = {node: index for index, node in enumerate(target_path)}
        lca = next(node for node in orphan_path if node in target_set)
        return {
            "target": target,
            "orphan": orphan,
            "lowest_common_ancestor": lca,
            "target_to_lca": target_path[: target_set[lca] + 1],
            "orphan_to_lca": orphan_path[: orphan_path.index(lca) + 1],
        }

    def _complete_rollback_request(
        self,
        request: RollbackRequest,
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        if row["phase"] == "applied":
            outcome = json.loads(row["outcome_json"])
            return {**outcome, "idempotent": True, "applied": True}
        connection = connect(self.db_path)
        try:
            with _transaction(connection):
                current_row = connection.execute(
                    "SELECT * FROM rollback_requests WHERE request_id = ?",
                    (request.request_id,),
                ).fetchone()
                if current_row is None:
                    raise BlockStoreError(
                        ErrorCode.RECOVERY_FAILED,
                        "prepared rollback request disappeared",
                        {"request_id": request.request_id},
                    )
                if current_row["phase"] == "applied":
                    outcome = json.loads(current_row["outcome_json"])
                    return {**outcome, "idempotent": True, "applied": True}
                target = connection.execute(
                    "SELECT * FROM snapshots WHERE snapshot_id = ?",
                    (request.target_snapshot_id,),
                ).fetchone()
                if target is None:
                    raise BlockStoreError(
                        ErrorCode.ROLLBACK_TARGET_NOT_FOUND,
                        "rollback target snapshot does not exist",
                        {"target_snapshot_id": request.target_snapshot_id},
                    )
                if target["status"] != "live":
                    raise BlockStoreError(
                        ErrorCode.ROLLBACK_TARGET_ORPHANED,
                        "rollback target snapshot is orphaned",
                        {"target_snapshot_id": request.target_snapshot_id},
                    )
                self._verify_snapshot_block_files(
                    connection, request.target_snapshot_id
                )
                view = connection.execute("SELECT * FROM current_view").fetchone()
                assert view is not None
                target_ancestors = set(
                    self._ancestor_path(connection, request.target_snapshot_id)
                )
                candidate_rows = connection.execute(
                    "SELECT * FROM snapshots "
                    "WHERE status = 'live' AND created_clock > ? "
                    "ORDER BY seq, snapshot_id",
                    (target["created_clock"],),
                ).fetchall()
                orphans: list[dict[str, Any]] = []
                next_version = int(view["view_version"]) + 1
                for candidate in candidate_rows:
                    if candidate["snapshot_id"] in target_ancestors:
                        continue
                    path = self._orphan_reference_path(
                        connection,
                        request.target_snapshot_id,
                        candidate["snapshot_id"],
                    )
                    orphans.append(
                        {
                            "snapshot_id": candidate["snapshot_id"],
                            "created_clock": candidate["created_clock"],
                            "reference_path": path,
                        }
                    )
                    connection.execute(
                        "UPDATE snapshots SET status = 'orphan', orphaned_by = ?, "
                        "orphan_reason = 'rollback_invalidated_branch', "
                        "orphan_path = ?, orphaned_view_version = ? "
                        "WHERE snapshot_id = ?",
                        (
                            request.request_id,
                            canonical_json(path),
                            next_version,
                            candidate["snapshot_id"],
                        ),
                    )
                target_rows = connection.execute(
                    "SELECT block_no, digest FROM snapshot_blocks "
                    "WHERE snapshot_id = ? ORDER BY block_no",
                    (request.target_snapshot_id,),
                ).fetchall()
                if not target_rows:
                    raise BlockStoreError(
                        ErrorCode.RECOVERY_FAILED,
                        "rollback target has no block ownership mapping",
                        {"target_snapshot_id": request.target_snapshot_id},
                    )
                connection.execute("DELETE FROM volume_blocks")
                volume_id = view["volume_id"]
                for target_row in target_rows:
                    connection.execute(
                        "INSERT INTO volume_blocks("
                        "block_no, volume_id, digest, owner_snapshot_id"
                        ") VALUES(?, ?, ?, ?)",
                        (
                            target_row["block_no"],
                            volume_id,
                            target_row["digest"],
                            request.target_snapshot_id,
                        ),
                    )
                connection.execute(
                    "UPDATE current_view SET view_version = ?, logical_clock = ?, "
                    "anchor_snapshot_id = ?, last_rollback_clock = ?, "
                    "last_rollback_request_id = ? WHERE singleton = 1",
                    (
                        next_version,
                        request.rollback_clock,
                        request.target_snapshot_id,
                        request.rollback_clock,
                        request.request_id,
                    ),
                )
                self._rebuild_refs(connection)
                outcome = {
                    "request_id": request.request_id,
                    "target_snapshot_id": request.target_snapshot_id,
                    "rollback_clock": request.rollback_clock,
                    "keep_after_target": request.keep_after_target,
                    "view_version": next_version,
                    "logical_clock": request.rollback_clock,
                    "anchor_snapshot_id": request.target_snapshot_id,
                    "orphaned_snapshots": orphans,
                    "idempotent": False,
                    "applied": True,
                }
                connection.execute(
                    "UPDATE rollback_requests SET phase = 'applied', "
                    "result_view_version = ?, outcome_json = ? WHERE request_id = ?",
                    (next_version, canonical_json(outcome), request.request_id),
                )
                return outcome
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Garbage collection
    # ------------------------------------------------------------------
    def gc(self) -> dict[str, Any]:
        with self.lock:
            self._require_initialized_locked()
            recovered = self._recover_locked()
            connection = connect(self.db_path)
            try:
                view = connection.execute("SELECT * FROM current_view").fetchone()
                assert view is not None
                existing = connection.execute(
                    "SELECT * FROM gc_runs WHERE volume_id = ? AND view_version = ?",
                    (view["volume_id"], view["view_version"]),
                ).fetchone()
                if existing is not None and existing["phase"] == "applied":
                    report = json.loads(existing["report_json"])
                    report["idempotent"] = True
                    return {
                        "ok": True,
                        "operation": "gc",
                        "recovery": recovered,
                        **report,
                    }
                if existing is None:
                    run_id = f"gc-v{view['view_version']}"
                    with _transaction(connection):
                        connection.execute(
                            "INSERT INTO gc_runs("
                            "run_id, volume_id, view_version, phase, report_json"
                            ") VALUES(?, ?, ?, 'prepared', NULL)",
                            (run_id, view["volume_id"], view["view_version"]),
                        )
                        self._populate_gc_actions(
                            connection,
                            run_id,
                            int(view["view_version"]),
                        )
                    _maybe_crash("gc:planned")
                    existing = connection.execute(
                        "SELECT * FROM gc_runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                assert existing is not None
                report = self._finish_gc_run(existing)
                report["idempotent"] = False
                return {
                    "ok": True,
                    "operation": "gc",
                    "recovery": recovered,
                    **report,
                }
            finally:
                connection.close()

    def _populate_gc_actions(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        view_version: int,
    ) -> None:
        ref_rows = connection.execute(
            "SELECT digest, source_kind, source_id, block_no FROM block_refs "
            "ORDER BY digest, source_kind, source_id, block_no"
        ).fetchall()
        refs_by_digest: dict[str, list[dict[str, Any]]] = {}
        for row in ref_rows:
            refs_by_digest.setdefault(str(row["digest"]), []).append(
                {
                    "source_kind": row["source_kind"],
                    "source_id": row["source_id"],
                    "block_no": int(row["block_no"]),
                }
            )
        known = set(self.blocks.iter_digests())
        known.update(
            str(row["digest"])
            for row in connection.execute("SELECT digest FROM blocks").fetchall()
        )
        known.update(refs_by_digest)
        for digest in sorted(known):
            references = refs_by_digest.get(digest, [])
            if references:
                action = "retain"
                source_kinds = sorted(
                    {str(reference["source_kind"]) for reference in references}
                )
                reason = "referenced_by_" + "+".join(source_kinds)
            else:
                action = "delete"
                reason = f"zero_references_at_view_version_{view_version}"
            connection.execute(
                "INSERT INTO gc_actions("
                "run_id, digest, action, reason, references_json, ref_count"
                ") VALUES(?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    digest,
                    action,
                    reason,
                    canonical_json(references),
                    len(references),
                ),
            )

    def _finish_gc_run(self, run: sqlite3.Row) -> dict[str, Any]:
        run_id = str(run["run_id"])
        connection = connect(self.db_path)
        try:
            if run["phase"] == "applied":
                return json.loads(run["report_json"])
            if run["phase"] == "prepared":
                with _transaction(connection):
                    connection.execute(
                        "UPDATE gc_runs SET phase = 'deleting' WHERE run_id = ?",
                        (run_id,),
                    )
                _maybe_crash("gc:deleting")
            delete_digests = [
                str(row["digest"])
                for row in connection.execute(
                    "SELECT digest FROM gc_actions "
                    "WHERE run_id = ? AND action = 'delete' ORDER BY digest",
                    (run_id,),
                ).fetchall()
            ]
            for index, digest in enumerate(delete_digests):
                referenced = connection.execute(
                    "SELECT 1 FROM block_refs WHERE digest = ? LIMIT 1",
                    (digest,),
                ).fetchone()
                if referenced is not None:
                    raise BlockStoreError(
                        ErrorCode.VERIFY_FAILED,
                        "GC candidate became referenced before deletion",
                        {"digest": digest, "run_id": run_id},
                    )
                self.blocks.delete(digest)
                if index == 0:
                    _maybe_crash("gc:after_one_delete")
            with _transaction(connection):
                for digest in delete_digests:
                    referenced = connection.execute(
                        "SELECT 1 FROM block_refs WHERE digest = ? LIMIT 1",
                        (digest,),
                    ).fetchone()
                    if referenced is not None:
                        raise BlockStoreError(
                            ErrorCode.VERIFY_FAILED,
                            "GC candidate became referenced before finalization",
                            {"digest": digest, "run_id": run_id},
                        )
                    connection.execute("DELETE FROM blocks WHERE digest = ?", (digest,))
                    connection.execute(
                        "DELETE FROM block_counts WHERE digest = ?", (digest,)
                    )
                action_rows = connection.execute(
                    "SELECT digest, action, reason, references_json, ref_count "
                    "FROM gc_actions WHERE run_id = ? ORDER BY digest",
                    (run_id,),
                ).fetchall()
                actions: list[dict[str, Any]] = []
                for row in action_rows:
                    actions.append(
                        {
                            "digest": row["digest"],
                            "action": row["action"],
                            "reason": row["reason"],
                            "references": json.loads(row["references_json"]),
                            "ref_count": int(row["ref_count"]),
                        }
                    )
                report = {
                    "run_id": run_id,
                    "view_version": int(run["view_version"]),
                    "phase": "applied",
                    "deleted_count": sum(
                        1 for action in actions if action["action"] == "delete"
                    ),
                    "retained_count": sum(
                        1 for action in actions if action["action"] == "retain"
                    ),
                    "actions": actions,
                }
                connection.execute(
                    "UPDATE gc_runs SET phase = 'applied', report_json = ? "
                    "WHERE run_id = ?",
                    (canonical_json(report), run_id),
                )
                _maybe_crash("gc:deleted")
                return report
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------
    def resume(self) -> dict[str, Any]:
        with self.lock:
            self._require_initialized_locked()
            actions = self._recover_locked()
            status = self._status_unlocked()
            return {
                "ok": True,
                "operation": "resume",
                "recovery_actions": actions,
                "status": status,
            }

    def _recover_locked(self) -> list[dict[str, Any]]:
        self._require_initialized_locked()
        removed_temp = self.blocks.clean_temp_files()
        actions: list[dict[str, Any]] = []
        if removed_temp:
            actions.append(
                {"kind": "clean_temp_files", "removed": sorted(removed_temp)}
            )
        connection = connect(self.db_path)
        try:
            snapshot_runs = connection.execute(
                "SELECT * FROM snapshot_runs WHERE phase = 'prepared' "
                "ORDER BY created_clock, seq, snapshot_id"
            ).fetchall()
        finally:
            connection.close()
        for run in snapshot_runs:
            spec = self._spec_from_run(run)
            result = self._complete_snapshot_run(spec, run)
            actions.append(
                {
                    "kind": "complete_snapshot",
                    "snapshot_id": spec.snapshot_id,
                    "result": result,
                }
            )
        connection = connect(self.db_path)
        try:
            rollback_rows = connection.execute(
                "SELECT * FROM rollback_requests WHERE phase = 'prepared' "
                "ORDER BY rollback_clock, request_id"
            ).fetchall()
        finally:
            connection.close()
        for row in rollback_rows:
            request = RollbackRequest(
                request_id=row["request_id"],
                target_snapshot_id=row["target_snapshot_id"],
                rollback_clock=int(row["rollback_clock"]),
                keep_after_target=bool(row["keep_after_target"]),
            )
            result = self._complete_rollback_request(request, row)
            actions.append(
                {
                    "kind": "complete_rollback",
                    "request_id": request.request_id,
                    "result": result,
                }
            )
        connection = connect(self.db_path)
        try:
            gc_rows = connection.execute(
                "SELECT * FROM gc_runs WHERE phase IN ('prepared', 'deleting') "
                "ORDER BY view_version, run_id"
            ).fetchall()
        finally:
            connection.close()
        for row in gc_rows:
            report = self._finish_gc_run(row)
            actions.append(
                {
                    "kind": "complete_gc",
                    "run_id": row["run_id"],
                    "deleted_count": report.get("deleted_count", 0),
                    "retained_count": report.get("retained_count", 0),
                }
            )
        return actions

    def _spec_from_run(self, run: sqlite3.Row) -> SnapshotSpec:
        volume = self._volume_identity_unlocked()
        pseudo_volume = VolumeSpec(
            volume_id=volume["volume_id"],
            block_size=int(volume["block_size"]),
            block_count=int(volume["block_count"]),
            created_clock=int(volume["created_clock"]),
            blocks={},
        )
        raw_spec = json.loads(run["spec_json"])
        specs = parse_snapshot_document(raw_spec, pseudo_volume, context="snapshot_run")
        if len(specs) != 1:
            raise BlockStoreError(
                ErrorCode.RECOVERY_FAILED,
                "prepared snapshot run does not contain exactly one snapshot",
                {"snapshot_id": run["snapshot_id"]},
            )
        if specs[0].snapshot_id != run["snapshot_id"]:
            raise BlockStoreError(
                ErrorCode.RECOVERY_FAILED,
                "prepared snapshot run id mismatch",
                {"snapshot_id": run["snapshot_id"], "spec": specs[0].snapshot_id},
            )
        return specs[0]

    # ------------------------------------------------------------------
    # Reads and verification
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        self._require_initialized_locked()
        return self._status_unlocked()

    def _status_unlocked(self) -> dict[str, Any]:
        connection = connect(self.db_path)
        try:
            connection.execute("BEGIN")
            volume = connection.execute("SELECT * FROM volumes LIMIT 1").fetchone()
            view = connection.execute("SELECT * FROM current_view").fetchone()
            assert volume is not None and view is not None
            snapshots: list[dict[str, Any]] = []
            for row in connection.execute(
                "SELECT * FROM snapshots ORDER BY seq, snapshot_id"
            ).fetchall():
                orphan_path = (
                    None
                    if row["orphan_path"] is None
                    else json.loads(row["orphan_path"])
                )
                snapshots.append(
                    {
                        "snapshot_id": row["snapshot_id"],
                        "seq": int(row["seq"]),
                        "parent_snapshot_id": row["parent_snapshot_id"],
                        "created_clock": int(row["created_clock"]),
                        "status": row["status"],
                        "is_base": bool(row["is_base"]),
                        "view_version": int(row["view_version"]),
                        "manifest_digest": row["manifest_digest"],
                        "orphaned_by": row["orphaned_by"],
                        "orphan_reason": row["orphan_reason"],
                        "orphan_path": orphan_path,
                    }
                )
            counts = [
                {"digest": row["digest"], "ref_count": int(row["ref_count"])}
                for row in connection.execute(
                    "SELECT digest, ref_count FROM block_counts ORDER BY digest"
                ).fetchall()
            ]
            pending = {
                "snapshots": int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM snapshot_runs WHERE phase = 'prepared'"
                    ).fetchone()["n"]
                ),
                "rollbacks": int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM rollback_requests "
                        "WHERE phase = 'prepared'"
                    ).fetchone()["n"]
                ),
                "gc": int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM gc_runs "
                        "WHERE phase IN ('prepared', 'deleting')"
                    ).fetchone()["n"]
                ),
            }
            connection.commit()
            return {
                "ok": True,
                "operation": "status",
                "volume_id": volume["volume_id"],
                "block_size": int(volume["block_size"]),
                "block_count": int(volume["block_count"]),
                "view_version": int(view["view_version"]),
                "logical_clock": int(view["logical_clock"]),
                "anchor_snapshot_id": view["anchor_snapshot_id"],
                "pending_operations": pending,
                "snapshots": snapshots,
                "block_reference_summary": {
                    "distinct_referenced_blocks": len(counts),
                    "total_references": sum(item["ref_count"] for item in counts),
                    "counts": counts,
                },
            }
        finally:
            connection.close()

    def verify(self) -> dict[str, Any]:
        with self.lock:
            self._require_initialized_locked()
            recovered = self._recover_locked()
            report = self._verify_unlocked()
            report["recovery"] = recovered
            if not report["ok"]:
                raise BlockStoreError(
                    ErrorCode.VERIFY_FAILED,
                    "persistent snapshot view is inconsistent",
                    report,
                )
            return report

    def _verify_unlocked(self) -> dict[str, Any]:
        connection = connect(self.db_path)
        try:
            connection.execute("BEGIN")
            checks: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []

            def check(name: str, condition: bool, details: Any = None) -> None:
                item: dict[str, Any] = {"name": name, "ok": bool(condition)}
                if details is not None:
                    item["details"] = details
                checks.append(item)
                if not condition:
                    errors.append(item)

            volume = connection.execute("SELECT * FROM volumes LIMIT 1").fetchone()
            view = connection.execute("SELECT * FROM current_view").fetchone()
            check("single_volume", volume is not None)
            check("single_current_view", view is not None)
            if volume is None or view is None:
                connection.commit()
                return {
                    "ok": False,
                    "operation": "verify",
                    "checks": checks,
                    "errors": errors,
                }
            block_count = int(volume["block_count"])
            base_id = base_snapshot_id(volume["volume_id"])
            snapshot_rows = connection.execute(
                "SELECT * FROM snapshots ORDER BY created_clock, seq, snapshot_id"
            ).fetchall()
            snapshots_by_id = {row["snapshot_id"]: row for row in snapshot_rows}
            check("base_snapshot_exists", base_id in snapshots_by_id)
            base_row = snapshots_by_id.get(base_id)
            if base_row is not None:
                check("base_snapshot_live", base_row["status"] == "live")
                check("base_snapshot_has_no_parent", base_row["parent_snapshot_id"] is None)
            bad_parents: list[dict[str, Any]] = []
            bad_clocks: list[dict[str, Any]] = []
            for row in snapshot_rows:
                parent_id = row["parent_snapshot_id"]
                if row["snapshot_id"] == base_id:
                    continue
                parent = snapshots_by_id.get(parent_id)
                if parent is None:
                    bad_parents.append(
                        {"snapshot_id": row["snapshot_id"], "parent": parent_id}
                    )
                    continue
                if row["created_clock"] <= parent["created_clock"]:
                    bad_clocks.append(
                        {
                            "snapshot_id": row["snapshot_id"],
                            "created_clock": row["created_clock"],
                            "parent_clock": parent["created_clock"],
                        }
                    )
            check("snapshot_parents_exist", not bad_parents, bad_parents or None)
            check("snapshot_clocks_monotonic", not bad_clocks, bad_clocks or None)
            bad_manifests: list[dict[str, Any]] = []
            manifests: dict[str, dict[str, Any]] = {}
            snapshot_blocks: dict[str, dict[int, str]] = {}
            snapshot_sources: dict[str, dict[int, str]] = {}
            bad_maps: list[dict[str, Any]] = []
            bad_statuses: list[dict[str, Any]] = []
            for row in snapshot_rows:
                snapshot_id = str(row["snapshot_id"])
                if row["status"] not in {"live", "orphan"}:
                    bad_statuses.append(
                        {"snapshot_id": snapshot_id, "status": row["status"]}
                    )
                if row["status"] == "orphan" and (
                    row["orphaned_by"] is None or row["orphan_path"] is None
                ):
                    bad_statuses.append(
                        {"snapshot_id": snapshot_id, "error": "orphan_audit_missing"}
                    )
                try:
                    manifest = json.loads(row["manifest_json"])
                except json.JSONDecodeError:
                    bad_manifests.append({"snapshot_id": snapshot_id, "error": "json"})
                    continue
                manifests[snapshot_id] = manifest
                if sha256_text(row["manifest_json"]) != row["manifest_digest"]:
                    bad_manifests.append(
                        {"snapshot_id": snapshot_id, "error": "manifest_digest"}
                    )
                if manifest.get("snapshot_id") != snapshot_id:
                    bad_manifests.append(
                        {"snapshot_id": snapshot_id, "error": "manifest_id"}
                    )
                expected_manifest_fields = {
                    "seq": int(row["seq"]),
                    "volume_id": row["volume_id"],
                    "parent_snapshot_id": row["parent_snapshot_id"],
                    "created_clock": int(row["created_clock"]),
                    "covered_start": row["covered_start"],
                    "covered_end": row["covered_end"],
                }
                for key, expected in expected_manifest_fields.items():
                    if manifest.get(key) != expected:
                        bad_manifests.append(
                            {
                                "snapshot_id": snapshot_id,
                                "error": "manifest_metadata",
                                "field": key,
                            }
                        )
                digest_by_block = {
                    int(item["block_no"]): item["digest"]
                    for item in manifest.get("blocks", [])
                }
                if snapshot_id == base_id:
                    expected_source = {number: "base" for number in range(block_count)}
                else:
                    expected_source = {}
                    for number in range(block_count):
                        if (
                            int(row["covered_start"])
                            <= number
                            <= int(row["covered_end"])
                        ):
                            expected_source[number] = "cow"
                        else:
                            expected_source[number] = "inherit"
                rows = connection.execute(
                    "SELECT block_no, digest, source FROM snapshot_blocks "
                    "WHERE snapshot_id = ? ORDER BY block_no",
                    (snapshot_id,),
                ).fetchall()
                mapping = {int(item["block_no"]): item["digest"] for item in rows}
                sources = {int(item["block_no"]): item["source"] for item in rows}
                if set(mapping) != set(range(block_count)):
                    bad_maps.append(
                        {
                            "snapshot_id": snapshot_id,
                            "error": "incomplete_block_map",
                            "missing": sorted(set(range(block_count)) - set(mapping)),
                        }
                    )
                if sources != expected_source:
                    bad_maps.append(
                        {"snapshot_id": snapshot_id, "error": "ownership_source"}
                    )
                covered_numbers = [
                    number
                    for number in range(block_count)
                    if expected_source[number] == "cow"
                ]
                if snapshot_id != base_id and set(covered_numbers) != set(
                    digest_by_block
                ):
                    bad_maps.append(
                        {"snapshot_id": snapshot_id, "error": "manifest_coverage"}
                    )
                for number, digest in digest_by_block.items():
                    if mapping.get(number) != digest:
                        bad_maps.append(
                            {
                                "snapshot_id": snapshot_id,
                                "block_no": number,
                                "error": "manifest_digest_mismatch",
                            }
                        )
                snapshot_blocks[snapshot_id] = mapping
                snapshot_sources[snapshot_id] = sources
            check("snapshot_manifests_valid", not bad_manifests, bad_manifests or None)
            check("snapshot_block_maps_valid", not bad_maps, bad_maps or None)
            check("snapshot_statuses_valid", not bad_statuses, bad_statuses or None)

            visible_maps: dict[str, dict[int, str]] = {}
            replay_errors: list[dict[str, Any]] = []
            for row in snapshot_rows:
                snapshot_id = str(row["snapshot_id"])
                if snapshot_id == base_id:
                    visible_maps[snapshot_id] = dict(
                        snapshot_blocks.get(snapshot_id, {})
                    )
                    continue
                parent_id = row["parent_snapshot_id"]
                if parent_id not in visible_maps:
                    replay_errors.append(
                        {"snapshot_id": snapshot_id, "error": "parent_not_replayed"}
                    )
                    continue
                visible = dict(visible_maps[parent_id])
                manifest = manifests.get(snapshot_id, {})
                for item in manifest.get("blocks", []):
                    visible[int(item["block_no"])] = item["digest"]
                if visible != snapshot_blocks.get(snapshot_id, {}):
                    replay_errors.append(
                        {
                            "snapshot_id": snapshot_id,
                            "error": "parent_replay_mismatch",
                        }
                    )
                visible_maps[snapshot_id] = visible
            check("snapshot_replay_matches_ownership", not replay_errors, replay_errors or None)

            volume_rows = connection.execute(
                "SELECT block_no, digest, owner_snapshot_id FROM volume_blocks "
                "ORDER BY block_no"
            ).fetchall()
            volume_map = {
                int(row["block_no"]): str(row["digest"]) for row in volume_rows
            }
            anchor = view["anchor_snapshot_id"]
            check("volume_block_map_complete", set(volume_map) == set(range(block_count)))
            bad_owners = [
                {
                    "block_no": int(row["block_no"]),
                    "owner_snapshot_id": row["owner_snapshot_id"],
                }
                for row in volume_rows
                if row["owner_snapshot_id"] != anchor
            ]
            check("volume_block_owner_matches_anchor", not bad_owners, bad_owners or None)
            check(
                "volume_matches_anchor",
                visible_maps.get(anchor) == volume_map,
                {
                    "anchor_snapshot_id": anchor,
                    "anchor_live": (
                        snapshots_by_id[anchor]["status"]
                        if anchor in snapshots_by_id
                        else None
                    ),
                },
            )
            check(
                "anchor_snapshot_live",
                anchor in snapshots_by_id
                and snapshots_by_id[anchor]["status"] == "live",
            )
            expected_refs: set[tuple[str, str, int, str]] = set()
            for block_no, digest in volume_map.items():
                expected_refs.add(("volume", volume["volume_id"], block_no, digest))
            for row in snapshot_rows:
                if row["status"] != "live":
                    continue
                snapshot_id = str(row["snapshot_id"])
                for block_no, digest in snapshot_blocks.get(snapshot_id, {}).items():
                    expected_refs.add(
                        ("snapshot", snapshot_id, block_no, digest)
                    )
            actual_refs = {
                (
                    row["source_kind"],
                    row["source_id"],
                    int(row["block_no"]),
                    row["digest"],
                )
                for row in connection.execute(
                    "SELECT source_kind, source_id, block_no, digest FROM block_refs"
                ).fetchall()
            }
            check(
                "block_reference_sources_match",
                actual_refs == expected_refs,
                {
                    "missing": sorted(expected_refs - actual_refs)[:20],
                    "unexpected": sorted(actual_refs - expected_refs)[:20],
                }
                if actual_refs != expected_refs
                else None,
            )
            expected_counts: dict[str, int] = {}
            for _, _, _, digest in expected_refs:
                expected_counts[digest] = expected_counts.get(digest, 0) + 1
            actual_counts = {
                row["digest"]: int(row["ref_count"])
                for row in connection.execute(
                    "SELECT digest, ref_count FROM block_counts"
                ).fetchall()
            }
            check(
                "reference_counts_match",
                actual_counts == expected_counts,
                {
                    "missing": {
                        key: value
                        for key, value in expected_counts.items()
                        if actual_counts.get(key) != value
                    },
                    "unexpected": {
                        key: value
                        for key, value in actual_counts.items()
                        if expected_counts.get(key) != value
                    },
                }
                if actual_counts != expected_counts
                else None,
            )
            block_errors: list[dict[str, Any]] = []
            for digest in sorted(expected_counts):
                row = connection.execute(
                    "SELECT size FROM blocks WHERE digest = ?", (digest,)
                ).fetchone()
                if row is None:
                    block_errors.append({"digest": digest, "error": "metadata_missing"})
                    continue
                try:
                    path = self.blocks.verify(digest)
                    if path.stat().st_size != int(row["size"]):
                        block_errors.append(
                            {"digest": digest, "error": "size_mismatch"}
                        )
                except BlockStoreError as exc:
                    block_errors.append(exc.as_dict())
            check("referenced_block_files_valid", not block_errors, block_errors or None)
            pending = {
                "snapshots": int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM snapshot_runs WHERE phase = 'prepared'"
                    ).fetchone()["n"]
                ),
                "rollbacks": int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM rollback_requests "
                        "WHERE phase = 'prepared'"
                    ).fetchone()["n"]
                ),
                "gc": int(
                    connection.execute(
                        "SELECT COUNT(*) AS n FROM gc_runs "
                        "WHERE phase IN ('prepared', 'deleting')"
                    ).fetchone()["n"]
                ),
            }
            check(
                "no_unfinished_operations",
                all(value == 0 for value in pending.values()),
                pending,
            )
            transaction_ref_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM transaction_refs"
                ).fetchone()["n"]
            )
            check(
                "no_uncommitted_transaction_refs",
                transaction_ref_count == 0,
                {"count": transaction_ref_count},
            )
            connection.commit()
            return {
                "ok": not errors,
                "operation": "verify",
                "volume_id": volume["volume_id"],
                "view_version": int(view["view_version"]),
                "logical_clock": int(view["logical_clock"]),
                "snapshot_count": len(snapshot_rows),
                "live_snapshot_count": sum(
                    1 for row in snapshot_rows if row["status"] == "live"
                ),
                "orphan_snapshot_count": sum(
                    1 for row in snapshot_rows if row["status"] == "orphan"
                ),
                "distinct_referenced_blocks": len(expected_counts),
                "total_references": sum(expected_counts.values()),
                "checks": checks,
                "errors": errors,
            }
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    def _require_initialized_locked(self) -> None:
        if not self.db_path.exists():
            raise BlockStoreError(
                ErrorCode.STORAGE_NOT_INITIALIZED,
                "storage is not initialized",
                {"root": str(self.root)},
            )
        connection = connect(self.db_path)
        try:
            if not table_exists(connection, "volumes"):
                raise BlockStoreError(
                    ErrorCode.STORAGE_NOT_INITIALIZED,
                    "storage database has no schema",
                    {"db_path": str(self.db_path)},
                )
            row = connection.execute("SELECT 1 FROM volumes LIMIT 1").fetchone()
            if row is None:
                raise BlockStoreError(
                    ErrorCode.STORAGE_NOT_INITIALIZED,
                    "storage database has no volume",
                    {"db_path": str(self.db_path)},
                )
        finally:
            connection.close()

    def _volume_identity_unlocked(self) -> sqlite3.Row:
        connection = connect(self.db_path)
        try:
            row = connection.execute("SELECT * FROM volumes LIMIT 1").fetchone()
            if row is None:
                raise BlockStoreError(
                    ErrorCode.STORAGE_NOT_INITIALIZED,
                    "storage has no volume",
                )
            return row
        finally:
            connection.close()

    def _rebuild_refs(self, connection: sqlite3.Connection) -> None:
        connection.execute("DELETE FROM block_refs")
        connection.execute("DELETE FROM block_counts")
        connection.execute(
            "INSERT INTO block_refs(source_kind, source_id, block_no, digest) "
            "SELECT 'volume', volume_id, block_no, digest FROM volume_blocks"
        )
        connection.execute(
            "INSERT INTO block_refs(source_kind, source_id, block_no, digest) "
            "SELECT 'snapshot', sb.snapshot_id, sb.block_no, sb.digest "
            "FROM snapshot_blocks sb JOIN snapshots s ON s.snapshot_id = sb.snapshot_id "
            "WHERE s.status = 'live'"
        )
        connection.execute(
            "INSERT INTO block_refs(source_kind, source_id, block_no, digest) "
            "SELECT 'transaction', source_id, block_no, digest "
            "FROM transaction_refs"
        )
        connection.execute(
            "INSERT INTO block_counts(digest, ref_count) "
            "SELECT digest, COUNT(*) FROM block_refs GROUP BY digest"
        )

    def _verify_snapshot_block_files(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
    ) -> None:
        rows = connection.execute(
            "SELECT DISTINCT digest FROM snapshot_blocks WHERE snapshot_id = ? "
            "ORDER BY digest",
            (snapshot_id,),
        ).fetchall()
        if not rows:
            raise BlockStoreError(
                ErrorCode.RECOVERY_FAILED,
                "snapshot has no block ownership mapping",
                {"snapshot_id": snapshot_id},
            )
        for row in rows:
            self.blocks.verify(str(row["digest"]))

    def volume_identity(self) -> dict[str, Any]:
        self._require_initialized_locked()
        row = self._volume_identity_unlocked()
        return {
            "volume_id": row["volume_id"],
            "block_size": int(row["block_size"]),
            "block_count": int(row["block_count"]),
            "created_clock": int(row["created_clock"]),
        }

    def parse_snapshot_file(
        self,
        raw: Any,
        *,
        snapshot_id: str | None = None,
        context: str = "snapshot",
    ) -> SnapshotSpec:
        volume = self.volume_identity()
        pseudo = VolumeSpec(
            volume_id=volume["volume_id"],
            block_size=volume["block_size"],
            block_count=volume["block_count"],
            created_clock=volume["created_clock"],
            blocks={},
        )
        specs = parse_snapshot_document(raw, pseudo, context=context)
        if snapshot_id is None:
            if len(specs) != 1:
                raise BlockStoreError(
                    ErrorCode.INVALID_INPUT,
                    "snapshot file contains multiple snapshots; select one with --snapshot-id",
                    {"count": len(specs)},
                )
            return specs[0]
        selected = [spec for spec in specs if spec.snapshot_id == snapshot_id]
        if not selected:
            raise BlockStoreError(
                ErrorCode.INVALID_INPUT,
                "requested snapshot_id is not present in the file",
                {"snapshot_id": snapshot_id},
            )
        if len(selected) > 1:
            raise BlockStoreError(
                ErrorCode.DUPLICATE_SNAPSHOT_ID,
                "requested snapshot_id appears more than once",
                {"snapshot_id": snapshot_id},
            )
        return selected[0]

    def parse_rollback_file(
        self,
        raw: Any,
        *,
        request_id: str | None = None,
        context: str = "rollback",
    ) -> RollbackRequest:
        volume = self.volume_identity()
        pseudo = VolumeSpec(
            volume_id=volume["volume_id"],
            block_size=volume["block_size"],
            block_count=volume["block_count"],
            created_clock=volume["created_clock"],
            blocks={},
        )
        requests = parse_rollback_document(raw, pseudo, context=context)
        if request_id is None:
            if len(requests) != 1:
                raise BlockStoreError(
                    ErrorCode.INVALID_INPUT,
                    "rollback file contains multiple requests; select one with --request-id",
                    {"count": len(requests)},
                )
            return requests[0]
        selected = [item for item in requests if item.request_id == request_id]
        if not selected:
            raise BlockStoreError(
                ErrorCode.INVALID_INPUT,
                "requested request_id is not present in the file",
                {"request_id": request_id},
            )
        if len(selected) > 1:
            raise BlockStoreError(
                ErrorCode.DUPLICATE_REQUEST_ID,
                "requested request_id appears more than once",
                {"request_id": request_id},
            )
        return selected[0]
