from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError, fail
from .util import canonical_json_bytes, is_int


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS builds (
    version INTEGER PRIMARY KEY,
    content_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    initial_clock_ms INTEGER NOT NULL,
    update_clock_ms INTEGER NOT NULL,
    plan_json TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    manifest_digest TEXT,
    manifest_relpath TEXT,
    manifest_json TEXT,
    last_round INTEGER NOT NULL DEFAULT 0,
    abandon_json TEXT NOT NULL DEFAULT '[]',
    failure_json TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS shard_commits (
    version INTEGER NOT NULL,
    shard_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    relpath TEXT NOT NULL,
    logical_cost_ms INTEGER NOT NULL,
    PRIMARY KEY (version, shard_id),
    FOREIGN KEY (version) REFERENCES builds(version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS shard_outcomes (
    version INTEGER NOT NULL,
    shard_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT,
    started INTEGER NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    logical_clock_ms INTEGER NOT NULL,
    logical_cost_ms INTEGER NOT NULL,
    round_no INTEGER NOT NULL,
    PRIMARY KEY (version, shard_id),
    FOREIGN KEY (version) REFERENCES builds(version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS hits (
    version INTEGER NOT NULL,
    shard_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    source_kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (version, shard_id),
    FOREIGN KEY (version) REFERENCES builds(version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS layers (
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    shard_id TEXT NOT NULL,
    shape_json TEXT NOT NULL,
    dtype TEXT NOT NULL,
    has_optimizer_state INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    param_digest TEXT NOT NULL,
    optimizer_digest TEXT,
    layer_digest TEXT NOT NULL,
    PRIMARY KEY (version, name),
    FOREIGN KEY (version) REFERENCES builds(version) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pointer (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER,
    manifest_digest TEXT,
    manifest_relpath TEXT,
    logical_clock_ms INTEGER
);

CREATE TABLE IF NOT EXISTS watermarks (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event TEXT NOT NULL,
    version INTEGER,
    logical_clock_ms INTEGER,
    detail_json TEXT NOT NULL
);

INSERT OR IGNORE INTO pointer(id, version, manifest_digest, manifest_relpath, logical_clock_ms)
VALUES (1, NULL, NULL, NULL, NULL);
INSERT OR IGNORE INTO watermarks(key, value) VALUES ('logical_clock_ms', 0);
INSERT OR IGNORE INTO watermarks(key, value) VALUES ('committed_clock_ms', 0);
INSERT OR IGNORE INTO watermarks(key, value) VALUES ('max_version', 0);
"""


def _json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _connect_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10.0, isolation_level=None)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def read_state(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    path = Path(db_path)
    conn = _connect_ro(path)
    empty = {
        "exists": path.exists(),
        "initialized": False,
        "builds": {},
        "max_version": 0,
        "watermarks": {
            "logical_clock_ms": 0,
            "committed_clock_ms": 0,
            "max_version": 0,
        },
        "pointer": None,
    }
    if conn is None:
        return empty
    try:
        if not _table_exists(conn, "builds") or not _table_exists(conn, "watermarks"):
            return empty
        builds = {
            int(row["version"]): dict(row)
            for row in conn.execute("SELECT * FROM builds ORDER BY version")
        }
        watermark_rows = {
            row["key"]: int(row["value"])
            for row in conn.execute("SELECT key, value FROM watermarks")
        }
        watermarks = {
            "logical_clock_ms": watermark_rows.get("logical_clock_ms", 0),
            "committed_clock_ms": watermark_rows.get(
                "committed_clock_ms",
                max(
                    (
                        int(row["update_clock_ms"])
                        for row in builds.values()
                        if row["status"] == "committed"
                    ),
                    default=0,
                ),
            ),
            "max_version": watermark_rows.get("max_version", 0),
        }
        max_version = max(builds, default=0)
        pointer_row = conn.execute("SELECT * FROM pointer WHERE id=1").fetchone()
        return {
            "exists": True,
            "initialized": True,
            "builds": builds,
            "max_version": max_version,
            "watermarks": watermarks,
            "pointer": _row_to_dict(pointer_row),
        }
    except sqlite3.Error as exc:
        raise BusinessError(
            "DATABASE_ERROR",
            "读取 SQLite 账本失败",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    finally:
        conn.close()


def evaluate_preflight(
    state: dict[str, Any],
    *,
    version: int,
    content_digest: str,
    logical_clock_ms: int,
    tolerance_ms: int,
) -> dict[str, Any]:
    builds: dict[int, dict[str, Any]] = state.get("builds", {})
    max_version = int(state.get("max_version", 0))
    existing = builds.get(version)
    watermark_state = state.get("watermarks", {})
    logical_watermark = int(
        watermark_state.get(
            "committed_clock_ms", watermark_state.get("logical_clock_ms", 0)
        )
    )

    if version < max_version:
        fail(
            "VERSION_ROLLBACK",
            "检查点版本号必须单调递增",
            requested_version=version,
            max_build_version=max_version,
        )

    if existing is not None:
        if existing["content_digest"] != content_digest:
            fail(
                "VERSION_CONFLICT",
                "同一版本已绑定不同内容",
                version=version,
                expected_content_digest=existing["content_digest"],
                actual_content_digest=content_digest,
            )
        if existing["status"] == "committed":
            return {"action": "idempotent", "build": existing}
        if logical_clock_ms + tolerance_ms < logical_watermark:
            fail(
                "CLOCK_ROLLBACK",
                "逻辑时钟回拨超过容差",
                logical_clock_ms=logical_clock_ms,
                tolerance_ms=tolerance_ms,
                logical_watermark_ms=logical_watermark,
            )
        return {
            "action": "resume",
            "build": existing,
            "effective_start_clock_ms": max(
                logical_clock_ms, int(existing["update_clock_ms"])
            ),
        }

    if logical_clock_ms + tolerance_ms < logical_watermark:
        fail(
            "CLOCK_ROLLBACK",
            "逻辑时钟回拨超过容差",
            logical_clock_ms=logical_clock_ms,
            tolerance_ms=tolerance_ms,
            logical_watermark_ms=logical_watermark,
        )

    incomplete = sorted(
        row["version"]
        for row in builds.values()
        if row["status"] != "committed" and row["version"] < version
    )
    if incomplete:
        fail(
            "BUILD_INCOMPLETE",
            "存在更早的未完成落盘轮次，需先 resume",
            incomplete_versions=incomplete,
            requested_version=version,
        )
    return {"action": "create", "effective_start_clock_ms": logical_clock_ms}


class Ledger:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.conn: sqlite3.Connection | None = None

    def open(self) -> "Ledger":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(
                str(self.path), timeout=30.0, isolation_level=None
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            conn.executescript(SCHEMA)
            committed_row = conn.execute(
                "SELECT value FROM watermarks WHERE key='committed_clock_ms'"
            ).fetchone()
            if committed_row is not None and int(committed_row["value"]) == 0:
                fallback = conn.execute(
                    "SELECT MAX(update_clock_ms) AS value FROM builds WHERE status='committed'"
                ).fetchone()
                if fallback is not None and fallback["value"] is not None:
                    conn.execute(
                        "UPDATE watermarks SET value=? WHERE key='committed_clock_ms'",
                        (int(fallback["value"]),),
                    )
        except (OSError, sqlite3.Error) as exc:
            raise BusinessError(
                "DATABASE_ERROR",
                "初始化 SQLite 账本失败",
                {"path": str(self.path), "reason": str(exc)},
            ) from exc
        self.conn = conn
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Ledger":
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @property
    def db(self) -> sqlite3.Connection:
        if self.conn is None:
            raise RuntimeError("ledger is not open")
        return self.conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield self.db
            self.db.execute("COMMIT")
        except BusinessError:
            self.db.execute("ROLLBACK")
            raise
        except sqlite3.Error as exc:
            try:
                self.db.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise BusinessError(
                "DATABASE_ERROR",
                "SQLite 事务失败",
                {"path": str(self.path), "reason": str(exc)},
            ) from exc
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def state(self) -> dict[str, Any]:
        builds = {
            int(row["version"]): dict(row)
            for row in self.db.execute("SELECT * FROM builds ORDER BY version")
        }
        watermark_rows = {
            row["key"]: int(row["value"])
            for row in self.db.execute("SELECT key, value FROM watermarks")
        }
        pointer_row = self.db.execute("SELECT * FROM pointer WHERE id=1").fetchone()
        return {
            "exists": True,
            "initialized": True,
            "builds": builds,
            "max_version": max(builds, default=0),
            "watermarks": {
                "logical_clock_ms": watermark_rows.get("logical_clock_ms", 0),
                "committed_clock_ms": watermark_rows.get(
                    "committed_clock_ms",
                    max(
                        (
                            int(row["update_clock_ms"])
                            for row in builds.values()
                            if row["status"] == "committed"
                        ),
                        default=0,
                    ),
                ),
                "max_version": watermark_rows.get("max_version", 0),
            },
            "pointer": _row_to_dict(pointer_row),
        }

    def preflight(
        self,
        *,
        version: int,
        content_digest: str,
        logical_clock_ms: int,
        tolerance_ms: int,
    ) -> dict[str, Any]:
        return evaluate_preflight(
            self.state(),
            version=version,
            content_digest=content_digest,
            logical_clock_ms=logical_clock_ms,
            tolerance_ms=tolerance_ms,
        )

    def _audit(
        self,
        conn: sqlite3.Connection,
        event: str,
        version: int | None,
        logical_clock_ms: int | None,
        detail: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO audit_log(event, version, logical_clock_ms, detail_json) "
            "VALUES (?, ?, ?, ?)",
            (event, version, logical_clock_ms, _json(detail)),
        )

    def begin_build(
        self,
        *,
        version: int,
        content_digest: str,
        initial_clock_ms: int,
        plan_json: str,
        policy_json: str,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO builds("
                "version, content_digest, status, initial_clock_ms, update_clock_ms, "
                "plan_json, policy_json) VALUES (?, ?, 'writing', ?, ?, ?, ?)",
                (
                    version,
                    content_digest,
                    initial_clock_ms,
                    initial_clock_ms,
                    plan_json,
                    policy_json,
                ),
            )
            conn.execute(
                "UPDATE watermarks SET value=MAX(value, ?) WHERE key='logical_clock_ms'",
                (initial_clock_ms,),
            )
            conn.execute(
                "UPDATE watermarks SET value=MAX(value, ?) WHERE key='max_version'",
                (version,),
            )
            self._audit(
                conn,
                "build_started",
                version,
                initial_clock_ms,
                {"content_digest": content_digest},
            )

    def start_round(
        self,
        *,
        version: int,
        round_no: int,
        start_clock_ms: int,
    ) -> None:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM builds WHERE version=?", (version,)
            ).fetchone()
            if row is None:
                fail("CHECKPOINT_NOT_FOUND", "落盘轮次不存在", version=version)
            if row["status"] == "committed":
                return
            conn.execute(
                "UPDATE builds SET status='writing', update_clock_ms=MAX(update_clock_ms, ?), "
                "last_round=? WHERE version=?",
                (start_clock_ms, round_no, version),
            )
            conn.execute(
                "UPDATE watermarks SET value=MAX(value, ?) WHERE key='logical_clock_ms'",
                (start_clock_ms,),
            )
            self._audit(
                conn,
                "round_started",
                version,
                start_clock_ms,
                {"round_no": round_no},
            )

    def get_build(self, version: int) -> dict[str, Any] | None:
        return _row_to_dict(
            self.db.execute(
                "SELECT * FROM builds WHERE version=?", (version,)
            ).fetchone()
        )

    def find_incomplete_build(self) -> dict[str, Any] | None:
        return _row_to_dict(
            self.db.execute(
                "SELECT * FROM builds WHERE status != 'committed' ORDER BY version DESC LIMIT 1"
            ).fetchone()
        )

    def get_shard_commits(self, version: int) -> dict[str, dict[str, Any]]:
        return {
            row["shard_id"]: dict(row)
            for row in self.db.execute(
                "SELECT * FROM shard_commits WHERE version=? ORDER BY shard_id",
                (version,),
            )
        }

    def get_hits(self, version: int) -> dict[str, dict[str, Any]]:
        return {
            row["shard_id"]: dict(row)
            for row in self.db.execute(
                "SELECT * FROM hits WHERE version=? ORDER BY shard_id", (version,)
            )
        }

    def _upsert_outcome(
        self,
        conn: sqlite3.Connection,
        *,
        version: int,
        shard_id: str,
        outcome: str,
        reason: str | None,
        started: bool,
        detail: dict[str, Any],
        logical_clock_ms: int,
        logical_cost_ms: int,
        round_no: int,
    ) -> None:
        conn.execute(
            "INSERT INTO shard_outcomes("
            "version, shard_id, outcome, reason, started, detail_json, "
            "logical_clock_ms, logical_cost_ms, round_no) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(version, shard_id) DO UPDATE SET "
            "outcome=excluded.outcome, reason=excluded.reason, started=excluded.started, "
            "detail_json=excluded.detail_json, "
            "logical_clock_ms=excluded.logical_clock_ms, "
            "logical_cost_ms=excluded.logical_cost_ms, round_no=excluded.round_no",
            (
                version,
                shard_id,
                outcome,
                reason,
                1 if started else 0,
                _json(detail),
                logical_clock_ms,
                logical_cost_ms,
                round_no,
            ),
        )

    def record_hit(
        self,
        *,
        version: int,
        shard_id: str,
        digest: str,
        size: int,
        relpath: str,
        source_version: int,
        source_kind: str,
        reason: str,
        logical_clock_ms: int,
        round_no: int,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO hits("
                "version, shard_id, digest, source_version, source_kind, reason) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(version, shard_id) DO UPDATE SET "
                "digest=excluded.digest, source_version=excluded.source_version, "
                "source_kind=excluded.source_kind, reason=excluded.reason",
                (
                    version,
                    shard_id,
                    digest,
                    source_version,
                    source_kind,
                    reason,
                ),
            )
            existing_commit = conn.execute(
                "SELECT digest FROM shard_commits WHERE version=? AND shard_id=?",
                (version, shard_id),
            ).fetchone()
            if existing_commit is not None and existing_commit["digest"] != digest:
                fail(
                    "VERSION_CONFLICT",
                    "命中分片与已提交分片摘要不一致",
                    version=version,
                    shard_id=shard_id,
                    existing_digest=existing_commit["digest"],
                    actual_digest=digest,
                )
            conn.execute(
                "INSERT OR IGNORE INTO shard_commits("
                "version, shard_id, digest, size, relpath, logical_cost_ms) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (version, shard_id, digest, size, relpath),
            )
            self._upsert_outcome(
                conn,
                version=version,
                shard_id=shard_id,
                outcome="hit",
                reason=reason,
                started=False,
                detail={
                    "source_version": source_version,
                    "source_kind": source_kind,
                    "digest": digest,
                },
                logical_clock_ms=logical_clock_ms,
                logical_cost_ms=0,
                round_no=round_no,
            )
            conn.execute(
                "UPDATE builds SET update_clock_ms=MAX(update_clock_ms, ?) WHERE version=?",
                (logical_clock_ms, version),
            )
            conn.execute(
                "UPDATE watermarks SET value=MAX(value, ?) WHERE key='logical_clock_ms'",
                (logical_clock_ms,),
            )
            self._audit(
                conn,
                "shard_hit",
                version,
                logical_clock_ms,
                {
                    "shard_id": shard_id,
                    "digest": digest,
                    "source_version": source_version,
                    "reason": reason,
                },
            )

    def record_shard_commit(
        self,
        *,
        version: int,
        shard_id: str,
        digest: str,
        size: int,
        relpath: str,
        logical_cost_ms: int,
        logical_clock_ms: int,
        round_no: int,
    ) -> None:
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT digest FROM shard_commits WHERE version=? AND shard_id=?",
                (version, shard_id),
            ).fetchone()
            if existing is not None:
                if existing["digest"] != digest:
                    fail(
                        "VERSION_CONFLICT",
                        "同一版本同一分片出现不同摘要",
                        version=version,
                        shard_id=shard_id,
                        existing_digest=existing["digest"],
                        actual_digest=digest,
                    )
            else:
                conn.execute(
                    "INSERT INTO shard_commits("
                    "version, shard_id, digest, size, relpath, logical_cost_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (version, shard_id, digest, size, relpath, logical_cost_ms),
                )
            self._upsert_outcome(
                conn,
                version=version,
                shard_id=shard_id,
                outcome="written",
                reason=None,
                started=True,
                detail={"digest": digest, "size": size},
                logical_clock_ms=logical_clock_ms,
                logical_cost_ms=logical_cost_ms,
                round_no=round_no,
            )
            conn.execute(
                "UPDATE builds SET update_clock_ms=MAX(update_clock_ms, ?) WHERE version=?",
                (logical_clock_ms, version),
            )
            conn.execute(
                "UPDATE watermarks SET value=MAX(value, ?) WHERE key='logical_clock_ms'",
                (logical_clock_ms,),
            )
            self._audit(
                conn,
                "shard_committed",
                version,
                logical_clock_ms,
                {"shard_id": shard_id, "digest": digest, "size": size},
            )

    def record_outcome(
        self,
        *,
        version: int,
        shard_id: str,
        outcome: str,
        reason: str,
        started: bool,
        detail: dict[str, Any],
        logical_clock_ms: int,
        logical_cost_ms: int,
        round_no: int,
    ) -> None:
        with self.transaction() as conn:
            self._upsert_outcome(
                conn,
                version=version,
                shard_id=shard_id,
                outcome=outcome,
                reason=reason,
                started=started,
                detail=detail,
                logical_clock_ms=logical_clock_ms,
                logical_cost_ms=logical_cost_ms,
                round_no=round_no,
            )
            self._audit(
                conn,
                f"shard_{outcome}",
                version,
                logical_clock_ms,
                {"shard_id": shard_id, "reason": reason, **detail},
            )

    def audit(
        self,
        *,
        event: str,
        version: int | None,
        logical_clock_ms: int | None,
        detail: dict[str, Any],
    ) -> None:
        with self.transaction() as conn:
            self._audit(conn, event, version, logical_clock_ms, detail)

    def finish_round(
        self,
        *,
        version: int,
        status: str,
        logical_clock_ms: int,
        abandon: list[dict[str, Any]],
        failures: list[dict[str, Any]],
        round_no: int,
    ) -> None:
        if status not in {"writing", "paused", "failed"}:
            raise ValueError(f"invalid intermediate status: {status}")
        with self.transaction() as conn:
            conn.execute(
                "UPDATE builds SET status=?, update_clock_ms=MAX(update_clock_ms, ?), "
                "last_round=?, abandon_json=?, failure_json=? WHERE version=?",
                (
                    status,
                    logical_clock_ms,
                    round_no,
                    _json(abandon),
                    _json(failures),
                    version,
                ),
            )
            conn.execute(
                "UPDATE watermarks SET value=MAX(value, ?) WHERE key='logical_clock_ms'",
                (logical_clock_ms,),
            )
            self._audit(
                conn,
                "round_finished",
                version,
                logical_clock_ms,
                {
                    "round_no": round_no,
                    "status": status,
                    "abandoned": abandon,
                    "failures": failures,
                },
            )

    def commit_manifest(
        self,
        *,
        version: int,
        manifest_json: str,
        manifest_digest: str,
        manifest_relpath: str,
        logical_clock_ms: int,
        expected_shards: dict[str, dict[str, Any]],
        layers: list[dict[str, Any]],
        hits: list[dict[str, Any]],
        round_no: int,
    ) -> None:
        with self.transaction() as conn:
            build = conn.execute(
                "SELECT status FROM builds WHERE version=?", (version,)
            ).fetchone()
            if build is None:
                fail("CHECKPOINT_NOT_FOUND", "落盘轮次不存在", version=version)
            if build["status"] == "committed":
                return

            committed = {
                row["shard_id"]: row["digest"]
                for row in conn.execute(
                    "SELECT shard_id, digest FROM shard_commits WHERE version=?",
                    (version,),
                )
            }
            if set(committed) != set(expected_shards):
                missing = sorted(set(expected_shards) - set(committed))
                extra = sorted(set(committed) - set(expected_shards))
                fail(
                    "SAVE_INCOMPLETE",
                    "已提交分片集合不完整",
                    missing=missing,
                    extra=extra,
                )
            for shard_id, expected in expected_shards.items():
                if committed[shard_id] != expected["digest"]:
                    fail(
                        "VERSION_CONFLICT",
                        "已提交分片摘要与清单不一致",
                        shard_id=shard_id,
                    )

            conn.execute(
                "UPDATE builds SET status='committed', update_clock_ms=MAX(update_clock_ms, ?), "
                "last_round=?, manifest_digest=?, manifest_relpath=?, manifest_json=?, "
                "abandon_json='[]', failure_json='[]' WHERE version=?",
                (
                    logical_clock_ms,
                    round_no,
                    manifest_digest,
                    manifest_relpath,
                    manifest_json,
                    version,
                ),
            )
            conn.execute("DELETE FROM layers WHERE version=?", (version,))
            for layer in layers:
                conn.execute(
                    "INSERT INTO layers("
                    "version, name, shard_id, shape_json, dtype, has_optimizer_state, "
                    "revision, param_digest, optimizer_digest, layer_digest) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        version,
                        layer["name"],
                        layer["shard_id"],
                        _json(layer["shape"]),
                        layer["dtype"],
                        1 if layer["has_optimizer_state"] else 0,
                        layer["revision"],
                        layer["param_digest"],
                        layer["optimizer_digest"],
                        layer["layer_digest"],
                    ),
                )
            conn.execute("DELETE FROM hits WHERE version=?", (version,))
            for hit in sorted(hits, key=lambda item: item["shard_id"]):
                conn.execute(
                    "INSERT INTO hits("
                    "version, shard_id, digest, source_version, source_kind, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        version,
                        hit["shard_id"],
                        hit["digest"],
                        hit["source_version"],
                        hit["source_kind"],
                        hit["reason"],
                    ),
                )

            pointer = conn.execute(
                "SELECT version FROM pointer WHERE id=1"
            ).fetchone()
            previous_pointer = pointer["version"] if pointer else None
            updated = conn.execute(
                "UPDATE pointer SET version=?, manifest_digest=?, manifest_relpath=?, "
                "logical_clock_ms=? WHERE id=1",
                (version, manifest_digest, manifest_relpath, logical_clock_ms),
            )
            if updated.rowcount != 1:
                fail("DATABASE_ERROR", "检查点指针整体替换失败")
            conn.execute(
                "UPDATE watermarks SET value=MAX(value, ?) WHERE key='logical_clock_ms'",
                (logical_clock_ms,),
            )
            conn.execute(
                "UPDATE watermarks SET value=MAX(value, ?) WHERE key='committed_clock_ms'",
                (logical_clock_ms,),
            )
            conn.execute(
                "UPDATE watermarks SET value=MAX(value, ?) WHERE key='max_version'",
                (version,),
            )
            self._audit(
                conn,
                "manifest_committed",
                version,
                logical_clock_ms,
                {
                    "manifest_digest": manifest_digest,
                    "previous_pointer_version": previous_pointer,
                    "new_pointer_version": version,
                    "hit_count": len(hits),
                },
            )

    def read_pointer_checkpoint(
        self, *, version: int | None = None
    ) -> dict[str, Any]:
        if version is None:
            row = self.db.execute("SELECT * FROM pointer WHERE id=1").fetchone()
            if row is None or row["version"] is None:
                fail("CHECKPOINT_NOT_FOUND", "检查点指针为空")
            selected_version = int(row["version"])
        else:
            selected_version = version
        build = self.get_build(selected_version)
        if build is None:
            fail("CHECKPOINT_NOT_FOUND", "指定版本不存在", version=selected_version)
        if build["status"] != "committed":
            fail(
                "CHECKPOINT_NOT_FOUND",
                "指定版本尚未完整提交，读取方不可见",
                version=selected_version,
                status=build["status"],
            )
        return build

    def status_snapshot(self, version: int | None = None) -> dict[str, Any]:
        with self.transaction() as conn:
            builds = {
                int(row["version"]): dict(row)
                for row in conn.execute("SELECT * FROM builds ORDER BY version")
            }
            pointer_row = conn.execute("SELECT * FROM pointer WHERE id=1").fetchone()
            pointer = _row_to_dict(pointer_row)
            watermarks = {
                row["key"]: int(row["value"])
                for row in conn.execute("SELECT key, value FROM watermarks")
            }
            selected_version = version
            if selected_version is None and pointer is not None:
                selected_version = pointer["version"]
            if selected_version is None and builds:
                selected_version = max(builds)
            selected = builds.get(selected_version) if selected_version is not None else None
            outcomes = []
            hits = []
            commits = []
            if selected_version is not None:
                outcomes = [
                    {
                        **dict(row),
                        "started": bool(row["started"]),
                        "detail": json.loads(row["detail_json"]),
                    }
                    for row in conn.execute(
                        "SELECT * FROM shard_outcomes WHERE version=? ORDER BY shard_id",
                        (selected_version,),
                    )
                ]
                hits = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM hits WHERE version=? ORDER BY shard_id",
                        (selected_version,),
                    )
                ]
                commits = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM shard_commits WHERE version=? ORDER BY shard_id",
                        (selected_version,),
                    )
                ]
            audit = [
                {
                    **dict(row),
                    "detail": json.loads(row["detail_json"]),
                }
                for row in conn.execute(
                    "SELECT * FROM audit_log ORDER BY id"
                )
            ]
            return {
                "initialized": True,
                "pointer": pointer,
                "watermarks": watermarks,
                "builds": builds,
                "selected_version": selected_version,
                "selected_build": selected,
                "outcomes": outcomes,
                "hits": hits,
                "shard_commits": commits,
                "audit_log": audit,
            }


def read_snapshot_status(
    db_path: str | os.PathLike[str], version: int | None = None
) -> dict[str, Any]:
    path = Path(db_path)
    if not path.exists():
        return {
            "initialized": False,
            "pointer": None,
            "watermarks": {
                "logical_clock_ms": 0,
                "committed_clock_ms": 0,
                "max_version": 0,
            },
            "builds": {},
            "selected_version": version,
            "selected_build": None,
            "outcomes": [],
            "hits": [],
            "shard_commits": [],
            "audit_log": [],
        }
    conn = _connect_ro(path)
    if conn is None:
        raise BusinessError(
            "DATABASE_ERROR",
            "无法只读打开 SQLite 账本",
            {"path": str(path)},
        )
    try:
        if not _table_exists(conn, "builds") or not _table_exists(conn, "watermarks"):
            return {
                "initialized": False,
                "pointer": None,
                "watermarks": {
                    "logical_clock_ms": 0,
                    "committed_clock_ms": 0,
                    "max_version": 0,
                },
                "builds": {},
                "selected_version": version,
                "selected_build": None,
                "outcomes": [],
                "hits": [],
                "shard_commits": [],
                "audit_log": [],
            }
        with conn:
            conn.execute("BEGIN")
            builds = {
                int(row["version"]): dict(row)
                for row in conn.execute("SELECT * FROM builds ORDER BY version")
            }
            pointer = _row_to_dict(
                conn.execute("SELECT * FROM pointer WHERE id=1").fetchone()
            )
            watermarks = {
                row["key"]: int(row["value"])
                for row in conn.execute("SELECT key, value FROM watermarks")
            }
            if "committed_clock_ms" not in watermarks:
                watermarks["committed_clock_ms"] = max(
                    (
                        int(row["update_clock_ms"])
                        for row in builds.values()
                        if row["status"] == "committed"
                    ),
                    default=0,
                )
            selected_version = version
            if selected_version is None and pointer is not None:
                selected_version = pointer["version"]
            if selected_version is None and builds:
                selected_version = max(builds)
            selected = (
                builds.get(selected_version) if selected_version is not None else None
            )
            outcomes = []
            hits = []
            commits = []
            if selected_version is not None:
                outcomes = [
                    {
                        **dict(row),
                        "started": bool(row["started"]),
                        "detail": json.loads(row["detail_json"]),
                    }
                    for row in conn.execute(
                        "SELECT * FROM shard_outcomes WHERE version=? ORDER BY shard_id",
                        (selected_version,),
                    )
                ]
                hits = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM hits WHERE version=? ORDER BY shard_id",
                        (selected_version,),
                    )
                ]
                commits = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT * FROM shard_commits WHERE version=? ORDER BY shard_id",
                        (selected_version,),
                    )
                ]
            audit = [
                {**dict(row), "detail": json.loads(row["detail_json"])}
                for row in conn.execute("SELECT * FROM audit_log ORDER BY id")
            ]
            return {
                "initialized": True,
                "pointer": pointer,
                "watermarks": watermarks,
                "builds": builds,
                "selected_version": selected_version,
                "selected_build": selected,
                "outcomes": outcomes,
                "hits": hits,
                "shard_commits": commits,
                "audit_log": audit,
            }
    except sqlite3.Error as exc:
        raise BusinessError(
            "DATABASE_ERROR",
            "读取诊断快照失败",
            {"path": str(path), "reason": str(exc)},
        ) from exc
    finally:
        conn.close()
