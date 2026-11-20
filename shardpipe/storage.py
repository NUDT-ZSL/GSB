from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .blobs import BlobStore
from .errors import BusinessError, ErrorCode
from .locking import FileLock
from .models import CacheKey, JobSpec, Workload
from .util import canonical_json
from .workload import parse_workload


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workloads (
    workload_digest TEXT PRIMARY KEY,
    canonical_json TEXT NOT NULL,
    global_seed INTEGER NOT NULL,
    loaded_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    workload_digest TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    generation_fingerprint TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    feature_dim INTEGER NOT NULL,
    sample_bytes INTEGER NOT NULL,
    shard_size INTEGER NOT NULL,
    cacheable INTEGER NOT NULL,
    PRIMARY KEY (workload_digest, name)
);

CREATE TABLE IF NOT EXISTS jobs (
    workload_digest TEXT NOT NULL,
    name TEXT NOT NULL,
    dataset_name TEXT NOT NULL,
    batch_size INTEGER NOT NULL,
    epochs INTEGER NOT NULL,
    data_parallelism INTEGER NOT NULL,
    prefetch_depth INTEGER NOT NULL,
    allow_shard_reuse INTEGER NOT NULL,
    PRIMARY KEY (workload_digest, name)
);

CREATE TABLE IF NOT EXISTS cache_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL,
    version TEXT NOT NULL,
    updated_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cache_entries (
    dataset TEXT NOT NULL,
    shard_index INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    generation_fingerprint TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    digest TEXT NOT NULL,
    last_used_clock INTEGER NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (dataset, shard_index)
);

CREATE TABLE IF NOT EXISTS shard_contents (
    dataset TEXT NOT NULL,
    shard_index INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    generation_fingerprint TEXT NOT NULL,
    digest TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    key_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    PRIMARY KEY (
        dataset, shard_index, generation, generation_fingerprint
    )
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    job_name TEXT NOT NULL,
    workload_digest TEXT NOT NULL,
    dataset_name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    updated_clock INTEGER NOT NULL,
    result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rounds (
    run_id TEXT NOT NULL,
    round_index INTEGER NOT NULL,
    logical_start_ms INTEGER NOT NULL,
    logical_end_ms INTEGER NOT NULL,
    peak_bytes INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    committed INTEGER NOT NULL,
    PRIMARY KEY (run_id, round_index)
);

CREATE TABLE IF NOT EXISTS run_shards (
    run_id TEXT NOT NULL,
    dataset TEXT NOT NULL,
    shard_index INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    generation_fingerprint TEXT NOT NULL,
    digest TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    round_index INTEGER NOT NULL,
    origin TEXT NOT NULL,
    PRIMARY KEY (
        run_id, dataset, shard_index, generation, generation_fingerprint
    )
);

CREATE TABLE IF NOT EXISTS run_batches (
    run_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    batch_key TEXT NOT NULL,
    batch_digest TEXT NOT NULL,
    round_index INTEGER NOT NULL,
    PRIMARY KEY (run_id, batch_id)
);

CREATE TABLE IF NOT EXISTS batch_results (
    batch_key TEXT PRIMARY KEY,
    batch_digest TEXT NOT NULL,
    job_name TEXT NOT NULL,
    dataset_name TEXT NOT NULL,
    epoch INTEGER NOT NULL,
    worker_id INTEGER NOT NULL,
    local_index INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    run_id TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_batch_results_digest
ON batch_results(batch_digest);

CREATE TABLE IF NOT EXISTS memory_ledger (
    run_id TEXT NOT NULL,
    round_index INTEGER NOT NULL,
    shard_key TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    PRIMARY KEY (run_id, round_index, shard_key)
);

CREATE TABLE IF NOT EXISTS snapshots (
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    round_index INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    memory_bytes INTEGER NOT NULL,
    memory_peak_bytes INTEGER NOT NULL,
    state_json TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    PRIMARY KEY (run_id, version)
);

CREATE TABLE IF NOT EXISTS current_snapshots (
    run_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS recovered_items (
    recovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_clock INTEGER NOT NULL
);
"""


def _failpoint(name: str) -> None:
    if os.environ.get("SHARDPIPE_FAILPOINT") == name:
        os._exit(91)


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class StateStore:
    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir).resolve()
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise BusinessError(
                ErrorCode.INVALID_STATE_DIR,
                f"cannot create state directory: {exc}",
                details={"state_dir": str(self.state_dir)},
            ) from exc
        self.db_path = self.state_dir / "control.db"
        self.lock_dir = self.state_dir / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.blobs = BlobStore(self.state_dir)
        with FileLock(self.state_dir / "init.lock"):
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA_SQL)
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            row = connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None or int(row["value"]) != SCHEMA_VERSION:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "unsupported state schema version",
                    details={"found": None if row is None else row["value"]},
                )

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load(self, workload: Workload, *, clock: int = 0) -> dict[str, Any]:
        canonical = canonical_json(dict(workload.canonical))
        with FileLock(self.state_dir / "load.lock"):
            with self.transaction() as connection:
                meta = connection.execute(
                    "SELECT generation, version FROM cache_meta WHERE singleton=1"
                ).fetchone()
                previous_generation = -1 if meta is None else int(meta["generation"])
                if workload.cache.generation < previous_generation:
                    raise BusinessError(
                        ErrorCode.CACHE_GENERATION_REGRESSION,
                        "cache generation must be monotonic across load operations",
                        details={
                            "previous_generation": previous_generation,
                            "new_generation": workload.cache.generation,
                        },
                    )
                version_changed = (
                    meta is not None and meta["version"] != workload.cache.version
                )
                generation_changed = (
                    meta is not None
                    and int(meta["generation"]) != workload.cache.generation
                )
                exists = connection.execute(
                    "SELECT 1 FROM workloads WHERE workload_digest=?",
                    (workload.digest,),
                ).fetchone()
                if exists is None:
                    connection.execute(
                        """
                        INSERT INTO workloads(
                            workload_digest, canonical_json, global_seed, loaded_clock
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (workload.digest, canonical, workload.global_seed, clock),
                    )
                fingerprints = dict(
                    workload.canonical.get("_dataset_fingerprints", {})
                )
                for dataset in workload.datasets:
                    connection.execute(
                        """
                        INSERT INTO datasets(
                            workload_digest, name, version,
                            generation_fingerprint, sample_count, feature_dim,
                            sample_bytes, shard_size, cacheable
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(workload_digest, name) DO UPDATE SET
                            version=excluded.version,
                            generation_fingerprint=excluded.generation_fingerprint,
                            sample_count=excluded.sample_count,
                            feature_dim=excluded.feature_dim,
                            sample_bytes=excluded.sample_bytes,
                            shard_size=excluded.shard_size,
                            cacheable=excluded.cacheable
                        """,
                        (
                            workload.digest,
                            dataset.name,
                            dataset.version,
                            fingerprints.get(dataset.name, ""),
                            dataset.sample_count,
                            dataset.feature_dim,
                            dataset.sample_bytes,
                            dataset.shard_size,
                            1 if dataset.cacheable else 0,
                        ),
                    )
                for job in workload.jobs:
                    connection.execute(
                        """
                        INSERT INTO jobs(
                            workload_digest, name, dataset_name, batch_size,
                            epochs, data_parallelism, prefetch_depth,
                            allow_shard_reuse
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(workload_digest, name) DO UPDATE SET
                            dataset_name=excluded.dataset_name,
                            batch_size=excluded.batch_size,
                            epochs=excluded.epochs,
                            data_parallelism=excluded.data_parallelism,
                            prefetch_depth=excluded.prefetch_depth,
                            allow_shard_reuse=excluded.allow_shard_reuse
                        """,
                        (
                            workload.digest,
                            job.name,
                            job.dataset,
                            job.batch_size,
                            job.epochs,
                            job.data_parallelism,
                            job.prefetch_depth,
                            1 if job.allow_shard_reuse else 0,
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO cache_meta(
                        singleton, generation, version, updated_clock
                    ) VALUES (1, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        generation=excluded.generation,
                        version=excluded.version,
                        updated_clock=excluded.updated_clock
                    """,
                    (workload.cache.generation, workload.cache.version, clock),
                )
                imported = 0
                skipped = 0
                for entry in workload.cache.entries:
                    existing = connection.execute(
                        """
                        SELECT generation FROM cache_entries
                        WHERE dataset=? AND shard_index=?
                        """,
                        (entry.dataset, entry.shard_index),
                    ).fetchone()
                    if existing is not None and existing["generation"] > entry.generation:
                        skipped += 1
                        continue
                    connection.execute(
                        """
                        INSERT INTO cache_entries(
                            dataset, shard_index, generation,
                            generation_fingerprint, bytes, digest,
                            last_used_clock, origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'cache_view')
                        ON CONFLICT(dataset, shard_index) DO UPDATE SET
                            generation=excluded.generation,
                            generation_fingerprint=excluded.generation_fingerprint,
                            bytes=excluded.bytes,
                            digest=excluded.digest,
                            last_used_clock=excluded.last_used_clock,
                            origin=excluded.origin
                        """,
                        (
                            entry.dataset,
                            entry.shard_index,
                            entry.generation,
                            entry.generation_fingerprint,
                            entry.bytes,
                            entry.digest,
                            entry.last_used_clock,
                        ),
                    )
                    imported += 1
                total_entries = int(
                    connection.execute(
                        "SELECT COUNT(*) AS count FROM cache_entries"
                    ).fetchone()["count"]
                )
                evicted: list[dict[str, Any]] = []
                if total_entries > workload.config.cache_capacity_entries:
                    rows = connection.execute(
                        """
                        SELECT dataset, shard_index, last_used_clock
                        FROM cache_entries
                        ORDER BY last_used_clock, dataset, shard_index
                        LIMIT ?
                        """,
                        (
                            total_entries
                            - workload.config.cache_capacity_entries,
                        ),
                    ).fetchall()
                    for row in rows:
                        evicted.append(
                            {
                                "dataset": row["dataset"],
                                "shard_index": row["shard_index"],
                            }
                        )
                        connection.execute(
                            """
                            DELETE FROM cache_entries
                            WHERE dataset=? AND shard_index=?
                            """,
                            (row["dataset"], row["shard_index"]),
                        )
        return {
            "ok": True,
            "status": "loaded",
            "workload_digest": workload.digest,
            "cache_generation": workload.cache.generation,
            "previous_cache_generation": previous_generation,
            "cache_generation_changed": generation_changed,
            "cache_version_changed": version_changed,
            "cache_entries_imported": imported,
            "cache_entries_skipped": skipped,
            "cache_entries_evicted": evicted,
            "datasets": sorted(dataset.name for dataset in workload.datasets),
            "jobs": sorted(job.name for job in workload.jobs),
        }

    def get_workload(self, digest: str | None = None) -> Workload:
        connection = self._connect()
        try:
            if digest is None:
                row = connection.execute(
                    "SELECT canonical_json FROM workloads ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT canonical_json FROM workloads WHERE workload_digest=?",
                    (digest,),
                ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise BusinessError(
                ErrorCode.NO_WORKLOAD,
                "no matching workload has been loaded",
                details={"workload_digest": digest},
            )
        return parse_workload(json.loads(row["canonical_json"]))

    def list_workloads(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT workload_digest, global_seed, loaded_clock FROM workloads"
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def next_run_id(self, job_name: str, workload_digest: str) -> str:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM runs
                WHERE job_name=? AND workload_digest=?
                """,
                (job_name, workload_digest),
            ).fetchone()
            sequence = int(row["count"]) + 1
        return f"{job_name}-{workload_digest[:10]}-{sequence:04d}"

    def create_run(
        self,
        workload: Workload,
        job: JobSpec,
        *,
        run_id: str,
        clock: int,
        initial_state: dict[str, Any],
    ) -> dict[str, Any]:
        state_json = canonical_json(initial_state)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, job_name, workload_digest, dataset_name,
                    status, created_clock, updated_clock, result_json
                ) VALUES (?, ?, ?, ?, 'running', ?, ?, ?)
                """,
                (
                    run_id,
                    job.name,
                    workload.digest,
                    job.dataset,
                    clock,
                    clock,
                    canonical_json({"status": "running"}),
                ),
            )
            connection.execute(
                """
                INSERT INTO snapshots(
                    run_id, version, round_index, logical_clock_ms,
                    memory_bytes, memory_peak_bytes, state_json, created_clock
                ) VALUES (?, 1, -1, ?, 0, 0, ?, ?)
                """,
                (run_id, clock, state_json, clock),
            )
            connection.execute(
                "INSERT INTO current_snapshots(run_id, version) VALUES (?, 1)",
                (run_id,),
            )
        return {
            "run_id": run_id,
            "job_name": job.name,
            "workload_digest": workload.digest,
            "dataset_name": job.dataset,
            "status": "running",
        }

    def get_run(self, run_id: str | None = None) -> dict[str, Any]:
        connection = self._connect()
        try:
            if run_id is None:
                row = connection.execute(
                    "SELECT * FROM runs ORDER BY created_clock DESC, rowid DESC LIMIT 1"
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise BusinessError(
                ErrorCode.RUN_NOT_FOUND,
                "run not found",
                details={"run_id": run_id},
            )
        result = dict(row)
        result["result"] = json.loads(result.pop("result_json"))
        return result

    def list_runs(self) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT run_id, job_name, workload_digest, dataset_name,
                       status, created_clock, updated_clock
                FROM runs ORDER BY run_id
                """
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def get_snapshot(self, run_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT s.*
                FROM current_snapshots c
                JOIN snapshots s
                  ON s.run_id=c.run_id AND s.version=c.version
                WHERE c.run_id=?
                """,
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise BusinessError(
                ErrorCode.STATE_CORRUPT,
                "run has no current snapshot",
                details={"run_id": run_id},
            )
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json"))
        return result

    def get_rounds(self, run_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM rounds WHERE run_id=? ORDER BY round_index",
                (run_id,),
            ).fetchall()
        finally:
            connection.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["state"] = json.loads(item.pop("state_json"))
            result.append(item)
        return result

    def list_cache_entries(self, dataset: str | None = None) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if dataset is None:
                rows = connection.execute(
                    """
                    SELECT * FROM cache_entries
                    ORDER BY dataset, shard_index
                    """
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM cache_entries WHERE dataset=?
                    ORDER BY shard_index
                    """,
                    (dataset,),
                ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def get_cache_entry(self, key: CacheKey) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM cache_entries
                WHERE dataset=? AND shard_index=?
                """,
                (key.dataset, key.shard_index),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_dict(row)

    def get_content(self, key: CacheKey) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM shard_contents
                WHERE dataset=? AND shard_index=? AND generation=?
                  AND generation_fingerprint=?
                """,
                (
                    key.dataset,
                    key.shard_index,
                    key.cache_generation,
                    key.generation_fingerprint,
                ),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_dict(row)

    def get_content_for_shard(
        self, dataset: str, shard_index: int
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM shard_contents
                WHERE dataset=? AND shard_index=?
                ORDER BY created_clock DESC, generation DESC
                LIMIT 1
                """,
                (dataset, shard_index),
            ).fetchone()
        finally:
            connection.close()
        return _row_to_dict(row)

    def commit_content(
        self,
        *,
        key: CacheKey,
        digest: str,
        byte_count: int,
        manifest: dict[str, Any],
        clock: int,
    ) -> dict[str, Any]:
        key_hash = self.blobs.key_hash(key)
        with self.transaction() as connection:
            existing = connection.execute(
                """
                SELECT * FROM shard_contents
                WHERE dataset=? AND shard_index=? AND generation=?
                  AND generation_fingerprint=?
                """,
                (
                    key.dataset,
                    key.shard_index,
                    key.cache_generation,
                    key.generation_fingerprint,
                ),
            ).fetchone()
            if existing is not None:
                if existing["digest"] != digest or existing["bytes"] != byte_count:
                    raise BusinessError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "existing shard content has a different digest or size",
                        details={
                            "key": key.as_dict(),
                            "existing_digest": existing["digest"],
                            "new_digest": digest,
                        },
                    )
                if existing["state"] != "ready":
                    connection.execute(
                        """
                        UPDATE shard_contents SET state='ready'
                        WHERE dataset=? AND shard_index=? AND generation=?
                          AND generation_fingerprint=?
                        """,
                        (
                            key.dataset,
                            key.shard_index,
                            key.cache_generation,
                            key.generation_fingerprint,
                        ),
                    )
                result = dict(existing)
                result["state"] = "ready"
                result["created"] = False
                return result
            connection.execute(
                """
                INSERT INTO shard_contents(
                    dataset, shard_index, generation,
                    generation_fingerprint, digest, bytes, key_hash,
                    state, created_clock
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ready', ?)
                """,
                (
                    key.dataset,
                    key.shard_index,
                    key.cache_generation,
                    key.generation_fingerprint,
                    digest,
                    byte_count,
                    key_hash,
                    clock,
                ),
            )
        return {
            "dataset": key.dataset,
            "shard_index": key.shard_index,
            "generation": key.cache_generation,
            "generation_fingerprint": key.generation_fingerprint,
            "digest": digest,
            "bytes": byte_count,
            "key_hash": key_hash,
            "state": "ready",
            "manifest": manifest,
            "created": True,
        }

    def recover_cache(
        self, *, workload: Workload | None, clock: int, run_id: str | None
    ) -> list[dict[str, Any]]:
        actions = self.blobs.quarantine_unpaired(clock)
        datasets = workload.dataset_map() if workload is not None else {}
        for key, manifest in self.blobs.iter_manifests():
            try:
                dataset = datasets.get(key.dataset)
                if dataset is not None and dataset.shard_bytes != int(
                    manifest["bytes"]
                ):
                    raise BusinessError(
                        ErrorCode.CACHE_FILE_CORRUPT,
                        "recovered shard size does not match dataset declaration",
                        details={"key": key.as_dict()},
                    )
                loaded = self.blobs.read(key)
                if loaded is None:
                    continue
                _, validated = loaded
                content = self.commit_content(
                    key=key,
                    digest=validated["digest"],
                    byte_count=validated["bytes"],
                    manifest=validated,
                    clock=clock,
                )
                if content["created"]:
                    actions.append(
                        {
                            "kind": "half_committed_cache",
                            "action": "registered",
                            "key": key.as_dict(),
                            "digest": validated["digest"],
                        }
                    )
            except BusinessError as exc:
                actions.append(
                    {
                        "kind": "half_committed_cache",
                        "action": "rejected",
                        "key": key.as_dict(),
                        "error": exc.as_dict()["error"],
                    }
                )
        connection = self._connect()
        try:
            content_rows = connection.execute(
                "SELECT * FROM shard_contents WHERE state='ready'"
            ).fetchall()
        finally:
            connection.close()
        for row in content_rows:
            key = CacheKey(
                dataset=row["dataset"],
                shard_index=int(row["shard_index"]),
                cache_generation=int(row["generation"]),
                generation_fingerprint=row["generation_fingerprint"],
            )
            if not self.blobs.blob_path(key).exists():
                connection = self._connect()
                try:
                    with connection:
                        connection.execute(
                            """
                            UPDATE shard_contents SET state='missing'
                            WHERE dataset=? AND shard_index=? AND generation=?
                              AND generation_fingerprint=?
                            """,
                            (
                                key.dataset,
                                key.shard_index,
                                key.cache_generation,
                                key.generation_fingerprint,
                            ),
                        )
                finally:
                    connection.close()
                actions.append(
                    {
                        "kind": "content_row",
                        "action": "missing_blob",
                        "key": key.as_dict(),
                    }
                )
        if run_id is not None:
            with self.transaction() as connection:
                for action in actions:
                    connection.execute(
                        """
                        INSERT INTO recovered_items(
                            run_id, kind, detail_json, created_clock
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            action["kind"],
                            canonical_json(action),
                            clock,
                        ),
                    )
        return actions

    def commit_round(
        self,
        *,
        run_id: str,
        expected_snapshot_version: int,
        round_index: int,
        round_state: dict[str, Any],
        new_state: dict[str, Any],
        successful_shards: list[dict[str, Any]],
        batches: list[dict[str, Any]],
        cache_updates: list[dict[str, Any]],
        cache_capacity: int,
        run_status: str,
        clock: int,
    ) -> dict[str, Any]:
        round_state = dict(round_state)
        snapshot_version = expected_snapshot_version + 1
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT version FROM current_snapshots WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if current is None or int(current["version"]) != expected_snapshot_version:
                raise BusinessError(
                    ErrorCode.RUN_CONFLICT,
                    "run snapshot advanced concurrently",
                    details={
                        "run_id": run_id,
                        "expected_version": expected_snapshot_version,
                        "actual_version": None if current is None else current["version"],
                    },
                )
            existing_round = connection.execute(
                "SELECT 1 FROM rounds WHERE run_id=? AND round_index=?",
                (run_id, round_index),
            ).fetchone()
            if existing_round is not None:
                raise BusinessError(
                    ErrorCode.RUN_CONFLICT,
                    "round has already been committed",
                    details={"run_id": run_id, "round_index": round_index},
                )

            connection.execute(
                """
                INSERT INTO rounds(
                    run_id, round_index, logical_start_ms,
                    logical_end_ms, peak_bytes, snapshot_version,
                    state_json, committed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    run_id,
                    round_index,
                    int(round_state["logical_start_ms"]),
                    int(round_state["logical_end_ms"]),
                    int(round_state["memory_peak_bytes"]),
                    snapshot_version,
                    canonical_json(round_state),
                ),
            )

            new_memory_keys: list[str] = []
            for shard in successful_shards:
                key = CacheKey(
                    dataset=shard["dataset"],
                    shard_index=int(shard["shard_index"]),
                    cache_generation=int(shard["cache_generation"]),
                    generation_fingerprint=shard["generation_fingerprint"],
                )
                existing = connection.execute(
                    """
                    SELECT * FROM run_shards
                    WHERE run_id=? AND dataset=? AND shard_index=?
                      AND generation=? AND generation_fingerprint=?
                    """,
                    (
                        run_id,
                        key.dataset,
                        key.shard_index,
                        key.cache_generation,
                        key.generation_fingerprint,
                    ),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO run_shards(
                            run_id, dataset, shard_index, generation,
                            generation_fingerprint, digest, bytes,
                            round_index, origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            key.dataset,
                            key.shard_index,
                            key.cache_generation,
                            key.generation_fingerprint,
                            shard["digest"],
                            int(shard["bytes"]),
                            round_index,
                            shard.get("origin", "generated"),
                        ),
                    )
                    new_memory_keys.append(key.identity())
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO memory_ledger(
                            run_id, round_index, shard_key, bytes
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            round_index,
                            key.identity(),
                            int(shard["bytes"]),
                        ),
                    )
                elif existing["digest"] != shard["digest"]:
                    raise BusinessError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "run shard digest changed between commits",
                        details={"key": key.as_dict()},
                    )

            for batch in batches:
                result = connection.execute(
                    "SELECT * FROM batch_results WHERE batch_key=?",
                    (batch["batch_key"],),
                ).fetchone()
                if result is None:
                    result = connection.execute(
                        "SELECT * FROM batch_results WHERE batch_digest=?",
                        (batch["batch_digest"],),
                    ).fetchone()
                if result is None:
                    connection.execute(
                        """
                        INSERT INTO batch_results(
                            batch_key, batch_digest, job_name,
                            dataset_name, epoch, worker_id, local_index,
                            state_json, created_clock, run_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            batch["batch_key"],
                            batch["batch_digest"],
                            batch["job_name"],
                            batch["dataset"],
                            int(batch["epoch"]),
                            int(batch["worker_id"]),
                            int(batch["local_index"]),
                            canonical_json(batch["state"]),
                            clock,
                            run_id,
                        ),
                    )
                elif result["batch_digest"] != batch["batch_digest"]:
                    raise BusinessError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "batch key exists with a different digest",
                        details={
                            "batch_key": batch["batch_key"],
                            "existing_digest": result["batch_digest"],
                            "new_digest": batch["batch_digest"],
                        },
                    )
                existing_run_batch = connection.execute(
                    """
                    SELECT * FROM run_batches
                    WHERE run_id=? AND batch_id=?
                    """,
                    (run_id, batch["batch_id"]),
                ).fetchone()
                if existing_run_batch is None:
                    connection.execute(
                        """
                        INSERT INTO run_batches(
                            run_id, batch_id, batch_key,
                            batch_digest, round_index
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            run_id,
                            batch["batch_id"],
                            batch["batch_key"],
                            batch["batch_digest"],
                            round_index,
                        ),
                    )
                elif existing_run_batch["batch_digest"] != batch["batch_digest"]:
                    raise BusinessError(
                        ErrorCode.IDEMPOTENCY_CONFLICT,
                        "run batch digest changed between commits",
                        details={"batch_id": batch["batch_id"]},
                    )

            for update in cache_updates:
                existing = connection.execute(
                    """
                    SELECT generation FROM cache_entries
                    WHERE dataset=? AND shard_index=?
                    """,
                    (update["dataset"], update["shard_index"]),
                ).fetchone()
                if existing is not None and int(existing["generation"]) > int(
                    update["generation"]
                ):
                    continue
                connection.execute(
                    """
                    INSERT INTO cache_entries(
                        dataset, shard_index, generation,
                        generation_fingerprint, bytes, digest,
                        last_used_clock, origin
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(dataset, shard_index) DO UPDATE SET
                        generation=excluded.generation,
                        generation_fingerprint=excluded.generation_fingerprint,
                        bytes=excluded.bytes,
                        digest=excluded.digest,
                        last_used_clock=excluded.last_used_clock,
                        origin=excluded.origin
                    """,
                    (
                        update["dataset"],
                        update["shard_index"],
                        update["generation"],
                        update["generation_fingerprint"],
                        update["bytes"],
                        update["digest"],
                        update["last_used_clock"],
                        update["origin"],
                    ),
                )
            total_entries = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM cache_entries"
                ).fetchone()["count"]
            )
            evicted: list[dict[str, Any]] = []
            if total_entries > cache_capacity:
                candidates = connection.execute(
                    """
                    SELECT dataset, shard_index FROM cache_entries
                    ORDER BY last_used_clock, dataset, shard_index
                    LIMIT ?
                    """,
                    (total_entries - cache_capacity,),
                ).fetchall()
                for candidate in candidates:
                    evicted.append(
                        {
                            "dataset": candidate["dataset"],
                            "shard_index": candidate["shard_index"],
                        }
                    )
                    connection.execute(
                        """
                        DELETE FROM cache_entries
                        WHERE dataset=? AND shard_index=?
                        """,
                        (candidate["dataset"], candidate["shard_index"]),
                    )
            round_state["cache_evicted"] = evicted
            connection.execute(
                """
                UPDATE rounds SET state_json=?
                WHERE run_id=? AND round_index=?
                """,
                (canonical_json(round_state), run_id, round_index),
            )

            memory_row = connection.execute(
                """
                SELECT COALESCE(SUM(bytes), 0) AS total
                FROM run_shards WHERE run_id=?
                """,
                (run_id,),
            ).fetchone()
            computed_memory = int(memory_row["total"])
            if computed_memory != int(new_state["memory_bytes"]):
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "memory ledger does not match new snapshot memory",
                    details={
                        "computed": computed_memory,
                        "snapshot": new_state["memory_bytes"],
                    },
                )
            if int(new_state["memory_peak_bytes"]) < computed_memory:
                raise BusinessError(
                    ErrorCode.STATE_CORRUPT,
                    "memory peak is smaller than resident memory",
                    details={"peak": new_state["memory_peak_bytes"]},
                )
            connection.execute(
                """
                INSERT INTO snapshots(
                    run_id, version, round_index, logical_clock_ms,
                    memory_bytes, memory_peak_bytes, state_json, created_clock
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    snapshot_version,
                    round_index,
                    int(new_state["clock_ms"]),
                    computed_memory,
                    int(new_state["memory_peak_bytes"]),
                    canonical_json(new_state),
                    clock,
                ),
            )
            connection.execute(
                """
                INSERT INTO current_snapshots(run_id, version)
                VALUES (?, ?)
                ON CONFLICT(run_id) DO UPDATE SET version=excluded.version
                """,
                (run_id, snapshot_version),
            )
            result_json = canonical_json(
                {
                    "status": run_status,
                    "snapshot_version": snapshot_version,
                    "round_index": round_index,
                    "clock_ms": int(new_state["clock_ms"]),
                    "memory_bytes": computed_memory,
                    "memory_peak_bytes": int(new_state["memory_peak_bytes"]),
                }
            )
            connection.execute(
                """
                UPDATE runs
                SET status=?, updated_clock=?, result_json=?
                WHERE run_id=?
                """,
                (run_status, clock, result_json, run_id),
            )
            _failpoint("before_round_commit")
        _failpoint("after_round_commit")
        return {
            "snapshot_version": snapshot_version,
            "round_index": round_index,
            "new_memory_keys": sorted(new_memory_keys),
            "evicted": evicted,
        }

    def finalize_run(
        self,
        *,
        run_id: str,
        status: str,
        result: dict[str, Any],
        clock: int,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE runs SET status=?, updated_clock=?, result_json=?
                WHERE run_id=?
                """,
                (status, clock, canonical_json(result), run_id),
            )

    def list_run_shards(self, run_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM run_shards WHERE run_id=?
                ORDER BY dataset, shard_index
                """,
                (run_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def list_run_batches(self, run_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM run_batches WHERE run_id=?
                ORDER BY batch_id
                """,
                (run_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def get_batch_result(self, batch_key: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM batch_results WHERE batch_key=?",
                (batch_key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json"))
        return result

    def get_batch_result_by_digest(
        self, batch_digest: str
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM batch_results WHERE batch_digest=?",
                (batch_digest,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        result = dict(row)
        result["state"] = json.loads(result.pop("state_json"))
        return result

    def find_stale_batches(
        self,
        *,
        job_name: str,
        dataset_name: str,
        dataset_fingerprint_value: str,
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT batch_key, batch_digest, epoch, worker_id,
                       local_index, dataset_name
                FROM batch_results
                WHERE job_name=? AND dataset_name=?
                ORDER BY epoch, worker_id, local_index
                """,
                (job_name, dataset_name),
            ).fetchall()
        finally:
            connection.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            # The current fingerprint is available in the canonical state.
            # A caller-provided batch key is the authoritative hit test; this
            # query reports historical candidates for invalidation diagnostics.
            result.append(item)
        return result

    def memory_ledger(self, run_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM memory_ledger WHERE run_id=?
                ORDER BY round_index, shard_key
                """,
                (run_id,),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def recovered(self, run_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM recovered_items WHERE run_id=?
                ORDER BY recovery_id
                """,
                (run_id,),
            ).fetchall()
        finally:
            connection.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item.pop("detail_json"))
            result.append(item)
        return result
