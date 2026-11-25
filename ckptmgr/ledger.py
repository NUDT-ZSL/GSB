"""SQLite ledger and cross-process file locking."""

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .errors import CheckpointNotFoundError

SCHEMA_VERSION = 1


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Ledger:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state_dir = root / ".ckpt"
        self.db_path = self.state_dir / "ledger.sqlite3"
        self.lock_path = self.state_dir / "ledger.lock"
        self.objects_dir = root / "objects"
        self.manifests_dir = root / "manifests"
        self.tmp_dir = self.state_dir / "tmp"

    def initialize(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS clock_watermarks (
                    scope TEXT PRIMARY KEY,
                    clock_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rounds (
                    round_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    plan_digest TEXT NOT NULL,
                    plan_json TEXT,
                    mode TEXT NOT NULL,
                    base_version INTEGER,
                    start_clock_ms INTEGER NOT NULL,
                    end_clock_ms INTEGER,
                    deadline_ms INTEGER NOT NULL,
                    budget_ms INTEGER NOT NULL,
                    concurrency INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    manifest_json TEXT,
                    manifest_digest TEXT,
                    diagnostics TEXT NOT NULL DEFAULT '{}',
                    UNIQUE(checkpoint, version)
                );

                CREATE TABLE IF NOT EXISTS round_shards (
                    round_id INTEGER NOT NULL,
                    shard_id TEXT NOT NULL,
                    shard_index INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    layer_names TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    logical_cost_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    started INTEGER NOT NULL DEFAULT 0,
                    finished INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    reuse_hit INTEGER NOT NULL DEFAULT 0,
                    reuse_from_version INTEGER,
                    reuse_basis TEXT,
                    PRIMARY KEY (round_id, shard_id)
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    plan_digest TEXT NOT NULL,
                    parameter_digest TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    base_version INTEGER,
                    round_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    clock_ms INTEGER NOT NULL,
                    committed_clock_ms INTEGER NOT NULL,
                    PRIMARY KEY (checkpoint, version)
                );

                CREATE TABLE IF NOT EXISTS checkpoint_shards (
                    checkpoint TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    shard_id TEXT NOT NULL,
                    shard_index INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    object_relpath TEXT NOT NULL,
                    layer_names TEXT NOT NULL,
                    reuse_hit INTEGER NOT NULL,
                    reuse_from_version INTEGER,
                    reuse_basis TEXT,
                    PRIMARY KEY (checkpoint, version, shard_id)
                );

                CREATE TABLE IF NOT EXISTS parameter_digests (
                    checkpoint TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    layer_name TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    shape_json TEXT NOT NULL,
                    dtype TEXT NOT NULL,
                    nbytes INTEGER NOT NULL,
                    shard_id TEXT NOT NULL,
                    shard_index INTEGER NOT NULL,
                    offset_in_shard INTEGER NOT NULL,
                    PRIMARY KEY (checkpoint, version, layer_name)
                );

                CREATE TABLE IF NOT EXISTS reuse_records (
                    checkpoint TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    shard_id TEXT NOT NULL,
                    source_version INTEGER,
                    hit INTEGER NOT NULL,
                    basis TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    PRIMARY KEY (checkpoint, version, shard_id)
                );

                CREATE TABLE IF NOT EXISTS objects (
                    digest TEXT PRIMARY KEY,
                    relpath TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    created_clock_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pointers (
                    checkpoint TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    clock_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    checkpoint TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    round_id INTEGER,
                    stage TEXT NOT NULL,
                    clock_ms INTEGER NOT NULL,
                    details_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_checkpoint_shards_digest
                    ON checkpoint_shards(digest);
                CREATE INDEX IF NOT EXISTS idx_round_shards_status
                    ON round_shards(round_id, status);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            round_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(rounds)").fetchall()
            }
            if "plan_json" not in round_columns:
                connection.execute("ALTER TABLE rounds ADD COLUMN plan_json TEXT")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            str(self.db_path), timeout=30.0, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    @contextmanager
    def file_lock(self, exclusive: bool) -> Iterator[None]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+")
        try:
            fcntl.flock(
                handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            )
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def object_relpath(self, digest: str) -> str:
        return "objects/{0}/{1}.shard".format(digest[:2], digest)

    def object_path(self, digest: str) -> Path:
        return self.root / self.object_relpath(digest)

    def manifest_relpath(self, checkpoint: str, version: int) -> str:
        safe = checkpoint.replace("/", "_").replace("\\", "_")
        return "manifests/{0}/{1}.json".format(safe, version)

    def manifest_path(self, checkpoint: str, version: int) -> Path:
        return self.root / self.manifest_relpath(checkpoint, version)

    def get_clock(self, scope: str = "global") -> Optional[int]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT clock_ms FROM clock_watermarks WHERE scope = ?", (scope,)
            ).fetchone()
            return int(row["clock_ms"]) if row else None

    def set_clock(self, clock_ms: int, scope: str = "global") -> None:
        with self.transaction() as connection:
            self.set_clock_in_transaction(connection, clock_ms, scope)

    @staticmethod
    def set_clock_in_transaction(
        connection: sqlite3.Connection, clock_ms: int, scope: str = "global"
    ) -> None:
        current = connection.execute(
            "SELECT clock_ms FROM clock_watermarks WHERE scope = ?", (scope,)
        ).fetchone()
        if current is not None:
            clock_ms = max(clock_ms, int(current["clock_ms"]))
        connection.execute(
            "INSERT INTO clock_watermarks(scope, clock_ms) VALUES(?, ?) "
            "ON CONFLICT(scope) DO UPDATE SET clock_ms = excluded.clock_ms",
            (scope, clock_ms),
        )

    def get_pointer(self, checkpoint: str) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM pointers WHERE checkpoint = ?", (checkpoint,)
            ).fetchone()
            return dict(row) if row else None

    def list_checkpoints(self, checkpoint: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM checkpoints"
        params: Tuple[Any, ...] = ()
        if checkpoint:
            sql += " WHERE checkpoint = ?"
            params = (checkpoint,)
        sql += " ORDER BY checkpoint, version"
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def get_checkpoint(
        self, checkpoint: str, version: Optional[int] = None
    ) -> Dict[str, Any]:
        with self.connection() as connection:
            if version is None:
                pointer = connection.execute(
                    "SELECT version FROM pointers WHERE checkpoint = ?", (checkpoint,)
                ).fetchone()
                if pointer is None:
                    raise CheckpointNotFoundError(
                        "checkpoint pointer does not exist", checkpoint=checkpoint
                    )
                version = int(pointer["version"])
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint = ? AND version = ?",
                (checkpoint, version),
            ).fetchone()
            if row is None:
                raise CheckpointNotFoundError(
                    "checkpoint version does not exist",
                    checkpoint=checkpoint,
                    version=version,
                )
            result = dict(row)
            result["manifest"] = json.loads(result["manifest_json"])
            return result

    def get_round(self, checkpoint: str, version: int) -> Optional[Dict[str, Any]]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM rounds WHERE checkpoint = ? AND version = ?",
                (checkpoint, version),
            ).fetchone()
            return dict(row) if row else None

    def list_rounds(self, checkpoint: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM rounds"
        params: Tuple[Any, ...] = ()
        if checkpoint:
            sql += " WHERE checkpoint = ?"
            params = (checkpoint,)
        sql += " ORDER BY checkpoint, version"
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(sql, params).fetchall()]

    def get_round_shards(self, round_id: int) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM round_shards WHERE round_id = ? ORDER BY shard_index",
                (round_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_checkpoint_shards(
        self, checkpoint: str, version: int
    ) -> List[Dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM checkpoint_shards "
                "WHERE checkpoint = ? AND version = ? ORDER BY shard_index",
                (checkpoint, version),
            ).fetchall()
            return [dict(row) for row in rows]

    def max_version(self, checkpoint: str) -> Optional[int]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT MAX(version) AS version FROM checkpoints WHERE checkpoint = ?",
                (checkpoint,),
            ).fetchone()
            if row is None or row["version"] is None:
                return None
            return int(row["version"])

    def event(
        self,
        checkpoint: str,
        version: int,
        stage: str,
        clock_ms: int,
        details: Dict[str, Any],
        round_id: Optional[int] = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO events(checkpoint, version, round_id, stage, clock_ms, details_json) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (checkpoint, version, round_id, stage, clock_ms, _json(details)),
            )

    def status(self) -> Dict[str, Any]:
        with self.connection() as connection:
            checkpoints = [
                dict(row)
                for row in connection.execute(
                    "SELECT checkpoint, version, plan_digest, parameter_digest, "
                    "manifest_digest, mode, base_version, clock_ms, committed_clock_ms "
                    "FROM checkpoints ORDER BY checkpoint, version"
                ).fetchall()
            ]
            pointers = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM pointers ORDER BY checkpoint"
                ).fetchall()
            ]
            rounds = [
                dict(row)
                for row in connection.execute(
                    "SELECT round_id, checkpoint, version, mode, status, start_clock_ms, "
                    "end_clock_ms, deadline_ms, budget_ms, concurrency, diagnostics "
                    "FROM rounds ORDER BY checkpoint, version"
                ).fetchall()
            ]
            for round_row in rounds:
                try:
                    round_row["diagnostics"] = json.loads(
                        round_row.get("diagnostics") or "{}"
                    )
                except (TypeError, json.JSONDecodeError):
                    round_row["diagnostics"] = {
                        "raw": round_row.get("diagnostics")
                    }
            round_shards = [
                dict(row)
                for row in connection.execute(
                    "SELECT round_id, shard_id, status, started, finished, reason "
                    "FROM round_shards ORDER BY round_id, shard_index"
                ).fetchall()
            ]
            object_count = int(
                connection.execute("SELECT COUNT(*) AS n FROM objects").fetchone()["n"]
            )
            object_bytes = int(
                connection.execute(
                    "SELECT COALESCE(SUM(size_bytes), 0) AS n FROM objects"
                ).fetchone()["n"]
            )
            clock = connection.execute(
                "SELECT clock_ms FROM clock_watermarks WHERE scope = 'global'"
            ).fetchone()
        return {
            "ok": True,
            "clock_watermark_ms": int(clock["clock_ms"]) if clock else None,
            "pointers": pointers,
            "checkpoints": checkpoints,
            "rounds": rounds,
            "round_shards": round_shards,
            "objects": {"count": object_count, "bytes": object_bytes},
        }
