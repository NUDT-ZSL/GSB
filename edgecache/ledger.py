from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .config import ContentState, Policy
from .errors import StateError, VersionError
from .util import canonical_json, monotonic_ms, stable_hash, utc_now_text


@dataclass(frozen=True)
class ActiveState:
    ledger_version: int
    policy_version: str
    policy_hash: str
    content_set_hash: str
    policy: Policy
    content: ContentState


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS system_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ledger_version INTEGER NOT NULL,
    policy_version TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    content_set_hash TEXT NOT NULL,
    policy_json TEXT NOT NULL,
    content_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_versions (
    version INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    content_set_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS path_state (
    path TEXT PRIMARY KEY,
    etag TEXT NOT NULL,
    entry_version INTEGER NOT NULL,
    ledger_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cache_entries (
    node_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    path TEXT NOT NULL,
    key_fields_json TEXT NOT NULL,
    etag TEXT NOT NULL,
    entry_version INTEGER NOT NULL,
    written_mono_ms INTEGER NOT NULL,
    ttl_ms INTEGER NOT NULL,
    remaining_ttl_ms INTEGER NOT NULL,
    ttl_anchor_mono_ms INTEGER NOT NULL,
    last_access_mono_ms INTEGER NOT NULL,
    ledger_version INTEGER NOT NULL,
    policy_hash TEXT NOT NULL,
    content_set_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (node_id, cache_key)
);
CREATE INDEX IF NOT EXISTS idx_cache_entries_path ON cache_entries(node_id, path);

CREATE TABLE IF NOT EXISTS cache_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    path TEXT NOT NULL,
    event_type TEXT NOT NULL,
    reason TEXT,
    ledger_version INTEGER NOT NULL,
    content_set_hash TEXT NOT NULL,
    entry_version INTEGER,
    before_json TEXT,
    after_json TEXT,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eviction_events (
    eviction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    path TEXT NOT NULL,
    etag TEXT,
    reason TEXT NOT NULL,
    rule TEXT,
    ledger_version INTEGER NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS write_intents (
    intent_id TEXT PRIMARY KEY,
    node_id TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    path TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_write_intents_status ON write_intents(node_id, status);

CREATE TABLE IF NOT EXISTS propagation_batches (
    batch_id TEXT PRIMARY KEY,
    ledger_version INTEGER NOT NULL,
    trigger_path TEXT NOT NULL,
    content_set_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS propagation_traces (
    trace_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    trigger_path TEXT NOT NULL,
    affected_path TEXT NOT NULL,
    depth INTEGER NOT NULL,
    relation TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    UNIQUE (batch_id, seq)
);

CREATE TABLE IF NOT EXISTS propagation_node_results (
    result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    trace_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    affected_path TEXT NOT NULL,
    status TEXT NOT NULL,
    before_etag TEXT,
    after_etag TEXT,
    entry_version INTEGER,
    replaced_entries INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (trace_id, node_id)
);
"""


class Ledger:
    """SQLite-backed, versioned system ledger for all local processes."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(Path(db_path))
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def ensure_schema(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(SCHEMA_SQL)
        finally:
            connection.close()

    @contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def _read(self, query: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        connection = self._connect()
        try:
            return list(connection.execute(query, params))
        finally:
            connection.close()

    def _one(self, query: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self._read(query, params)
        return rows[0] if rows else None

    def has_state(self) -> bool:
        return self._one("SELECT 1 FROM system_state WHERE id = 1") is not None

    def current_version(self) -> int:
        row = self._one("SELECT ledger_version FROM system_state WHERE id = 1")
        if row is None:
            raise StateError("ledger has no active state", location="system_state")
        return int(row["ledger_version"])

    def bootstrap(
        self,
        policy_doc: Mapping[str, Any] | None,
        content_doc: Mapping[str, Any] | None,
    ) -> tuple[ActiveState, bool]:
        """Initialize version 1 if empty; never overwrite an existing ledger."""

        with self.transaction() as connection:
            row = connection.execute("SELECT 1 FROM system_state WHERE id = 1").fetchone()
            if row is not None:
                active = self.active_state_conn(connection)
                return active, False
            if policy_doc is None or content_doc is None:
                raise StateError(
                    "empty ledger requires policy and content bootstrap documents",
                    location="system_state",
                )
            content = ContentState.from_doc(content_doc)
            policy = Policy.from_doc(policy_doc, content.paths)
            policy_hash = policy.policy_hash
            content_hash = content.content_set_hash
            version = 1
            now = utc_now_text()
            detail = {
                "reason": "bootstrap",
                "content_paths": list(content.paths),
            }
            connection.execute(
                "INSERT INTO ledger_versions(version, kind, policy_version, policy_hash, "
                "content_set_hash, created_at, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    version,
                    "bootstrap",
                    policy.policy_version,
                    policy_hash,
                    content_hash,
                    now,
                    canonical_json(detail),
                ),
            )
            connection.execute(
                "INSERT INTO system_state(id, ledger_version, policy_version, policy_hash, "
                "content_set_hash, policy_json, content_json, updated_at) "
                "VALUES (1, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version,
                    policy.policy_version,
                    policy_hash,
                    content_hash,
                    canonical_json(policy.to_doc()),
                    canonical_json(content.to_doc()),
                    now,
                ),
            )
            for path in content.paths:
                connection.execute(
                    "INSERT INTO path_state(path, etag, entry_version, ledger_version) "
                    "VALUES (?, ?, 1, ?)",
                    (path, content.effective[path].etag, version),
                )
            active = self.active_state_conn(connection)
            return active, True

    def active_state(self) -> ActiveState:
        connection = self._connect()
        try:
            return self.active_state_conn(connection)
        finally:
            connection.close()

    def active_state_conn(self, connection: sqlite3.Connection) -> ActiveState:
        row = connection.execute("SELECT * FROM system_state WHERE id = 1").fetchone()
        if row is None:
            raise StateError("ledger has no active state", location="system_state")
        policy_doc = json.loads(row["policy_json"])
        content_doc = json.loads(row["content_json"])
        content = ContentState.from_doc(content_doc)
        policy = Policy.from_doc(policy_doc, content.paths)
        if policy.policy_hash != row["policy_hash"]:
            raise StateError(
                "active policy hash does not match policy JSON",
                location="system_state.policy_hash",
                expected=policy.policy_hash,
                actual=row["policy_hash"],
            )
        if content.content_set_hash != row["content_set_hash"]:
            raise StateError(
                "active content set hash does not match content JSON",
                location="system_state.content_set_hash",
                expected=content.content_set_hash,
                actual=row["content_set_hash"],
            )
        return ActiveState(
            ledger_version=int(row["ledger_version"]),
            policy_version=str(row["policy_version"]),
            policy_hash=str(row["policy_hash"]),
            content_set_hash=str(row["content_set_hash"]),
            policy=policy,
            content=content,
        )

    def assert_version(self, expected: int) -> ActiveState:
        active = self.active_state()
        self._assert_version_value(expected, active.ledger_version)
        return active

    def assert_version_conn(
        self,
        connection: sqlite3.Connection,
        expected: int,
    ) -> ActiveState:
        active = self.active_state_conn(connection)
        self._assert_version_value(expected, active.ledger_version)
        return active

    @staticmethod
    def _assert_version_value(expected: int, current: int) -> None:
        if not isinstance(expected, int):
            raise VersionError(
                "ledger_version must be an integer",
                location="request.ledger_version",
            )
        if expected != current:
            raise VersionError(
                "cache operation mixed ledger versions",
                location="request.ledger_version",
                expected=expected,
                current=current,
            )

    def update_policy(self, policy_doc: Mapping[str, Any], detail: Mapping[str, Any] | None = None) -> ActiveState:
        with self.transaction() as connection:
            active = self.active_state_conn(connection)
            policy = Policy.from_doc(policy_doc, active.content.paths)
            new_version = int(
                connection.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM ledger_versions").fetchone()[0]
            )
            if new_version <= active.ledger_version:
                new_version = active.ledger_version + 1
            now = utc_now_text()
            entry_detail = dict(detail or {})
            entry_detail.update(
                {
                    "from_policy_version": active.policy_version,
                    "to_policy_version": policy.policy_version,
                }
            )
            connection.execute(
                "INSERT INTO ledger_versions(version, kind, policy_version, policy_hash, "
                "content_set_hash, created_at, detail_json) VALUES (?, 'policy', ?, ?, ?, ?, ?)",
                (
                    new_version,
                    policy.policy_version,
                    policy.policy_hash,
                    active.content_set_hash,
                    now,
                    canonical_json(entry_detail),
                ),
            )
            connection.execute(
                "UPDATE system_state SET ledger_version = ?, policy_version = ?, policy_hash = ?, "
                "policy_json = ?, updated_at = ? WHERE id = 1",
                (
                    new_version,
                    policy.policy_version,
                    policy.policy_hash,
                    canonical_json(policy.to_doc()),
                    now,
                ),
            )
            return self.active_state_conn(connection)

    def commit_content_transition(
        self,
        trigger_path: str,
        new_content: ContentState,
        batch_id: str,
        propagation: Sequence[tuple[str, int, str]],
        detail: Mapping[str, Any] | None = None,
    ) -> tuple[int, dict[str, int]]:
        """Persist a content evolution, entry-version transitions and trace batch atomically.

        ``propagation`` entries are ``(affected_path, depth, relation)`` in stable
        topological order and must include the trigger path first.
        """

        with self.transaction() as connection:
            active = self.active_state_conn(connection)
            content_set_hash = new_content.content_set_hash
            now = utc_now_text()
            new_version = int(
                connection.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM ledger_versions").fetchone()[0]
            )
            if new_version <= active.ledger_version:
                new_version = active.ledger_version + 1

            old_paths = {
                row["path"]: row
                for row in connection.execute("SELECT * FROM path_state").fetchall()
            }
            version_transitions: dict[str, int] = {}
            for path in new_content.paths:
                etag = new_content.effective[path].etag
                old = old_paths.get(path)
                if old is None:
                    entry_version = 1
                elif old["etag"] != etag:
                    entry_version = int(old["entry_version"]) + 1
                else:
                    entry_version = int(old["entry_version"])
                version_transitions[path] = entry_version
                connection.execute(
                    "INSERT INTO path_state(path, etag, entry_version, ledger_version) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                    "etag = excluded.etag, entry_version = excluded.entry_version, "
                    "ledger_version = excluded.ledger_version",
                    (path, etag, entry_version, new_version),
                )
            stale_policy_paths = sorted(set(old_paths) - set(new_content.paths))
            if stale_policy_paths:
                raise StateError(
                    "content evolution removed paths; this component requires stable path declarations",
                    location="contents",
                    removed_paths=stale_policy_paths,
                )

            entry_detail = dict(detail or {})
            entry_detail.update(
                {
                    "trigger_path": trigger_path,
                    "batch_id": batch_id,
                    "propagation_count": len(propagation),
                    "entry_version_transitions": version_transitions,
                }
            )
            connection.execute(
                "INSERT INTO ledger_versions(version, kind, policy_version, policy_hash, "
                "content_set_hash, created_at, detail_json) VALUES (?, 'content', ?, ?, ?, ?, ?)",
                (
                    new_version,
                    active.policy_version,
                    active.policy_hash,
                    content_set_hash,
                    now,
                    canonical_json(entry_detail),
                ),
            )
            connection.execute(
                "UPDATE system_state SET ledger_version = ?, content_set_hash = ?, "
                "content_json = ?, updated_at = ? WHERE id = 1",
                (new_version, content_set_hash, canonical_json(new_content.to_doc()), now),
            )
            connection.execute(
                "INSERT INTO propagation_batches(batch_id, ledger_version, trigger_path, "
                "content_set_hash, created_at, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    new_version,
                    trigger_path,
                    content_set_hash,
                    now,
                    canonical_json(entry_detail),
                ),
            )
            for seq, (affected_path, depth, relation) in enumerate(propagation):
                connection.execute(
                    "INSERT INTO propagation_traces(batch_id, seq, trigger_path, affected_path, "
                    "depth, relation, ledger_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        seq,
                        trigger_path,
                        affected_path,
                        depth,
                        relation,
                        new_version,
                    ),
                )
            return new_version, version_transitions

    def get_path_versions(self) -> dict[str, dict[str, Any]]:
        return {
            row["path"]: {
                "etag": row["etag"],
                "entry_version": int(row["entry_version"]),
                "ledger_version": int(row["ledger_version"]),
            }
            for row in self._read("SELECT * FROM path_state ORDER BY path")
        }

    def get_path_entry_version_conn(self, connection: sqlite3.Connection, path: str) -> int:
        row = connection.execute(
            "SELECT entry_version FROM path_state WHERE path = ?", (path,)
        ).fetchone()
        if row is None:
            raise StateError("path has no entry version", location=f"path_state[{path!r}]")
        return int(row["entry_version"])

    def newest_ledger_version(self) -> int:
        row = self._one("SELECT COALESCE(MAX(version), 0) AS version FROM ledger_versions")
        return int(row["version"]) if row else 0

    def ledger_history(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._read("SELECT * FROM ledger_versions ORDER BY version")]

    def list_entries(self, node_id: str | None = None) -> list[dict[str, Any]]:
        if node_id is None:
            rows = self._read("SELECT * FROM cache_entries ORDER BY node_id, cache_key")
        else:
            rows = self._read(
                "SELECT * FROM cache_entries WHERE node_id = ? ORDER BY cache_key", (node_id,)
            )
        return [dict(row) for row in rows]

    def get_entry(self, node_id: str, cache_key: str) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM cache_entries WHERE node_id = ? AND cache_key = ?",
            (node_id, cache_key),
        )
        return dict(row) if row else None

    def list_evictions(self, node_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if node_id is None:
            rows = self._read(
                "SELECT * FROM eviction_events ORDER BY eviction_id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._read(
                "SELECT * FROM eviction_events WHERE node_id = ? "
                "ORDER BY eviction_id DESC LIMIT ?",
                (node_id, limit),
            )
        return [dict(row) for row in rows]

    def list_cache_events(self, node_id: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        if node_id is None:
            rows = self._read(
                "SELECT * FROM cache_events ORDER BY event_id DESC LIMIT ?", (limit,)
            )
        else:
            rows = self._read(
                "SELECT * FROM cache_events WHERE node_id = ? ORDER BY event_id DESC LIMIT ?",
                (node_id, limit),
            )
        return [dict(row) for row in rows]

    def record_cache_event_conn(
        self,
        connection: sqlite3.Connection,
        *,
        node_id: str,
        cache_key: str,
        path: str,
        event_type: str,
        ledger_version: int,
        content_set_hash: str,
        entry_version: int | None = None,
        reason: str | None = None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO cache_events(node_id, cache_key, path, event_type, reason, "
            "ledger_version, content_set_hash, entry_version, before_json, after_json, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id,
                cache_key,
                path,
                event_type,
                reason,
                ledger_version,
                content_set_hash,
                entry_version,
                canonical_json(before) if before is not None else None,
                canonical_json(after) if after is not None else None,
                utc_now_text(),
            ),
        )

    def record_eviction_conn(
        self,
        connection: sqlite3.Connection,
        *,
        node_id: str,
        cache_key: str,
        path: str,
        etag: str | None,
        reason: str,
        rule: str | None,
        ledger_version: int,
    ) -> None:
        connection.execute(
            "INSERT INTO eviction_events(node_id, cache_key, path, etag, reason, rule, "
            "ledger_version, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (node_id, cache_key, path, etag, reason, rule, ledger_version, utc_now_text()),
        )

    def begin_write_intent(
        self,
        *,
        node_id: str,
        cache_key: str,
        path: str,
        ledger_version: int,
        detail: Mapping[str, Any] | None = None,
    ) -> str:
        intent_id = uuid.uuid4().hex
        now = utc_now_text()
        with self.transaction() as connection:
            self.assert_version_conn(connection, ledger_version)
            connection.execute(
                "INSERT INTO write_intents(intent_id, node_id, cache_key, path, ledger_version, "
                "status, detail_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    intent_id,
                    node_id,
                    cache_key,
                    path,
                    ledger_version,
                    canonical_json(detail or {}),
                    now,
                    now,
                ),
            )
        return intent_id

    @staticmethod
    def mark_write_intent_conn(
        connection: sqlite3.Connection,
        intent_id: str,
        status: str,
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            "UPDATE write_intents SET status = ?, detail_json = ?, updated_at = ? WHERE intent_id = ?",
            (status, canonical_json(detail or {}), utc_now_text(), intent_id),
        )

    def fail_write_intent(self, intent_id: str, detail: Mapping[str, Any] | None = None) -> None:
        with self.transaction() as connection:
            self.mark_write_intent_conn(connection, intent_id, "failed", detail)

    def list_write_intents(self, node_id: str | None = None) -> list[dict[str, Any]]:
        if node_id is None:
            rows = self._read("SELECT * FROM write_intents ORDER BY created_at, intent_id")
        else:
            rows = self._read(
                "SELECT * FROM write_intents WHERE node_id = ? ORDER BY created_at, intent_id",
                (node_id,),
            )
        return [dict(row) for row in rows]

    def recover_node(self, node_id: str, now_ms: int | None = None) -> dict[str, Any]:
        now_ms = monotonic_ms() if now_ms is None else now_ms
        unknown: list[dict[str, Any]] = []
        stale: list[dict[str, Any]] = []
        with self.transaction() as connection:
            active = self.active_state_conn(connection)
            pending = connection.execute(
                "SELECT * FROM write_intents WHERE node_id = ? AND status = 'pending' "
                "ORDER BY created_at, intent_id",
                (node_id,),
            ).fetchall()
            for row in pending:
                unknown_row = dict(row)
                unknown_row["status"] = "unknown"
                connection.execute(
                    "UPDATE write_intents SET status = 'unknown', updated_at = ? WHERE intent_id = ?",
                    (utc_now_text(), row["intent_id"]),
                )
                self.record_cache_event_conn(
                    connection,
                    node_id=node_id,
                    cache_key=row["cache_key"],
                    path=row["path"],
                    event_type="unknown",
                    ledger_version=active.ledger_version,
                    content_set_hash=active.content_set_hash,
                    reason="interrupted_write_requires_rerun",
                    before=None,
                    after={"intent_id": row["intent_id"], "status": "unknown"},
                )
                unknown.append(unknown_row)

            stale_rows = connection.execute(
                "SELECT * FROM cache_entries WHERE node_id = ? AND "
                "(ledger_version != ? OR content_set_hash != ? OR policy_hash != ?) "
                "ORDER BY cache_key",
                (
                    node_id,
                    active.ledger_version,
                    active.content_set_hash,
                    active.policy_hash,
                ),
            ).fetchall()
            for row in stale_rows:
                item = dict(row)
                stale.append(item)
                connection.execute(
                    "DELETE FROM cache_entries WHERE node_id = ? AND cache_key = ?",
                    (node_id, row["cache_key"]),
                )
                self.record_eviction_conn(
                    connection,
                    node_id=node_id,
                    cache_key=row["cache_key"],
                    path=row["path"],
                    etag=row["etag"],
                    reason="recovery_version_mismatch",
                    rule=None,
                    ledger_version=active.ledger_version,
                )
                self.record_cache_event_conn(
                    connection,
                    node_id=node_id,
                    cache_key=row["cache_key"],
                    path=row["path"],
                    event_type="evict",
                    ledger_version=active.ledger_version,
                    content_set_hash=active.content_set_hash,
                    entry_version=int(row["entry_version"]),
                    reason="recovery_version_mismatch",
                    before=item,
                    after=None,
                )
        return {
            "node_id": node_id,
            "ledger_version": active.ledger_version,
            "unknown_intents": unknown,
            "rerun_required": bool(unknown),
            "stale_entries_removed": stale,
            "restored_entries": len(self.list_entries(node_id)),
            "recovered_at_mono_ms": now_ms,
        }

    def propagation_batches(self) -> list[dict[str, Any]]:
        batches = [dict(row) for row in self._read(
            "SELECT * FROM propagation_batches ORDER BY ledger_version, batch_id"
        )]
        for batch in batches:
            batch["traces"] = [dict(row) for row in self._read(
                "SELECT * FROM propagation_traces WHERE batch_id = ? ORDER BY seq",
                (batch["batch_id"],),
            )]
            batch["node_results"] = [dict(row) for row in self._read(
                "SELECT * FROM propagation_node_results WHERE batch_id = ? "
                "ORDER BY trace_id, node_id",
                (batch["batch_id"],),
            )]
        return batches

    def record_propagation_node_results(
        self,
        batch_id: str,
        node_id: str,
        results: Sequence[Mapping[str, Any]],
        ledger_version: int,
    ) -> None:
        with self.transaction() as connection:
            active = self.active_state_conn(connection)
            self._assert_version_value(ledger_version, active.ledger_version)
            traces = connection.execute(
                "SELECT trace_id, affected_path, ledger_version FROM propagation_traces "
                "WHERE batch_id = ? ORDER BY seq",
                (batch_id,),
            ).fetchall()
            by_path = {row["affected_path"]: row for row in traces}
            for result in results:
                path = str(result["affected_path"])
                trace = by_path.get(path)
                if trace is None:
                    raise StateError(
                        "propagation result references an unknown path",
                        location=f"propagation_traces[{batch_id!r}]",
                        affected_path=path,
                    )
                connection.execute(
                    "INSERT INTO propagation_node_results(batch_id, trace_id, node_id, affected_path, "
                    "status, before_etag, after_etag, entry_version, replaced_entries, detail_json, "
                    "occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(trace_id, node_id) DO UPDATE SET status = excluded.status, "
                    "before_etag = excluded.before_etag, after_etag = excluded.after_etag, "
                    "entry_version = excluded.entry_version, replaced_entries = excluded.replaced_entries, "
                    "detail_json = excluded.detail_json, occurred_at = excluded.occurred_at",
                    (
                        batch_id,
                        trace["trace_id"],
                        node_id,
                        path,
                        result.get("status", "ok"),
                        result.get("before_etag"),
                        result.get("after_etag"),
                        result.get("entry_version"),
                        int(result.get("replaced_entries", 0)),
                        canonical_json(result.get("detail", {})),
                        utc_now_text(),
                    ),
                )

    def export_snapshot_rows(self) -> dict[str, Any]:
        active = self.active_state()
        connection = self._connect()
        try:
            def rows(table: str, order: str) -> list[dict[str, Any]]:
                return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY {order}")]

            return {
                "active": active,
                "ledger_history": rows("ledger_versions", "version"),
                "cache_entries": rows("cache_entries", "node_id, cache_key"),
                "path_versions": rows("path_state", "path"),
                "evictions": rows("eviction_events", "eviction_id"),
                "cache_events": rows("cache_events", "event_id"),
                "write_intents": rows("write_intents", "created_at, intent_id"),
                "propagation_batches": rows("propagation_batches", "ledger_version, batch_id"),
                "propagation_traces": rows("propagation_traces", "batch_id, seq"),
                "propagation_node_results": rows(
                    "propagation_node_results", "batch_id, trace_id, node_id"
                ),
            }
        finally:
            connection.close()

    def restore_snapshot_state(
        self,
        *,
        policy_doc: Mapping[str, Any],
        content_doc: Mapping[str, Any],
        entries: Sequence[Mapping[str, Any]],
        path_versions: Sequence[Mapping[str, Any]],
        propagation_batches: Sequence[Mapping[str, Any]],
        propagation_traces: Sequence[Mapping[str, Any]],
        propagation_node_results: Sequence[Mapping[str, Any]],
        source_snapshot_ledger_version: int,
        now_ms: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replace active entries/derivations wholesale; never merge rows by key."""

        start_mono = monotonic_ms() if now_ms is None else now_ms
        content = ContentState.from_doc(content_doc)
        policy = Policy.from_doc(policy_doc, content.paths)
        policy_hash = policy.policy_hash
        content_hash = content.content_set_hash
        now = utc_now_text()

        with self.transaction() as connection:
            active = self.active_state_conn(connection)
            previous_entries = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM cache_entries ORDER BY node_id, cache_key"
                ).fetchall()
            ]
            connection.execute("DELETE FROM cache_entries")
            new_version = int(
                connection.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM ledger_versions").fetchone()[0]
            )
            if new_version <= active.ledger_version:
                new_version = active.ledger_version + 1

            entry_detail = dict(detail or {})
            entry_detail.update(
                {
                    "source_snapshot_ledger_version": source_snapshot_ledger_version,
                    "replaced_active_entries": len(previous_entries),
                    "restored_entries": len(entries),
                }
            )
            connection.execute(
                "INSERT INTO ledger_versions(version, kind, policy_version, policy_hash, "
                "content_set_hash, created_at, detail_json) VALUES (?, 'snapshot_restore', ?, ?, ?, ?, ?)",
                (
                    new_version,
                    policy.policy_version,
                    policy_hash,
                    content_hash,
                    now,
                    canonical_json(entry_detail),
                ),
            )
            connection.execute(
                "UPDATE system_state SET ledger_version = ?, policy_version = ?, policy_hash = ?, "
                "content_set_hash = ?, policy_json = ?, content_json = ?, updated_at = ? WHERE id = 1",
                (
                    new_version,
                    policy.policy_version,
                    policy_hash,
                    content_hash,
                    canonical_json(policy.to_doc()),
                    canonical_json(content.to_doc()),
                    now,
                ),
            )
            connection.execute("DELETE FROM path_state")
            for state in sorted(path_versions, key=lambda item: str(item.get("path", ""))):
                path = str(state["path"])
                if path not in content.effective:
                    raise StateError(
                        "snapshot path version references missing content path",
                        location="snapshot.path_versions",
                        path=path,
                    )
                etag = str(state["etag"])
                if content.effective[path].etag != etag:
                    raise StateError(
                        "snapshot path ETag mismatch",
                        location="snapshot.path_versions",
                        path=path,
                        expected=content.effective[path].etag,
                        actual=etag,
                    )
                connection.execute(
                    "INSERT INTO path_state(path, etag, entry_version, ledger_version) "
                    "VALUES (?, ?, ?, ?)",
                    (path, etag, int(state["entry_version"]), new_version),
                )
            missing = sorted(set(content.paths) - {str(item["path"]) for item in path_versions})
            if missing:
                raise StateError(
                    "snapshot has no entry version for content paths",
                    location="snapshot.path_versions",
                    missing_paths=missing,
                )

            inserted = 0
            for entry in sorted(entries, key=lambda item: (str(item["node_id"]), str(item["cache_key"]))):
                path = str(entry["path"])
                if path not in content.effective:
                    raise StateError(
                        "snapshot cache entry references missing content path",
                        location="snapshot.cache_entries.path",
                        path=path,
                    )
                if content.effective[path].etag != entry["etag"]:
                    raise StateError(
                        "snapshot cache ETag does not match snapshot content set",
                        location="snapshot.cache_entries.etag",
                        node_id=entry["node_id"],
                        path=path,
                    )
                connection.execute(
                    "INSERT INTO cache_entries(node_id, cache_key, path, key_fields_json, etag, "
                    "entry_version, written_mono_ms, ttl_ms, remaining_ttl_ms, ttl_anchor_mono_ms, "
                    "last_access_mono_ms, ledger_version, policy_hash, content_set_hash, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["node_id"],
                        entry["cache_key"],
                        path,
                        entry["key_fields_json"],
                        entry["etag"],
                        int(entry["entry_version"]),
                        start_mono,
                        int(entry["ttl_ms"]),
                        int(entry["remaining_ttl_ms"]),
                        start_mono,
                        start_mono,
                        new_version,
                        policy_hash,
                        content_hash,
                        now,
                    ),
                )
                inserted += 1
                self.record_cache_event_conn(
                    connection,
                    node_id=str(entry["node_id"]),
                    cache_key=str(entry["cache_key"]),
                    path=path,
                    event_type="restore",
                    ledger_version=new_version,
                    content_set_hash=content_hash,
                    entry_version=int(entry["entry_version"]),
                    reason="snapshot_full_replace",
                    before=None,
                    after=dict(entry),
                )

            # Propagation traces are immutable replay evidence.  Copy missing
            # rows from the snapshot without replacing later audit history.
            for batch in propagation_batches:
                connection.execute(
                    "INSERT OR IGNORE INTO propagation_batches(batch_id, ledger_version, trigger_path, "
                    "content_set_hash, created_at, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        batch["batch_id"],
                        int(batch["ledger_version"]),
                        batch["trigger_path"],
                        batch["content_set_hash"],
                        batch["created_at"],
                        batch["detail_json"],
                    ),
                )
            for trace in propagation_traces:
                connection.execute(
                    "INSERT OR IGNORE INTO propagation_traces(trace_id, batch_id, seq, trigger_path, "
                    "affected_path, depth, relation, ledger_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(trace["trace_id"]),
                        trace["batch_id"],
                        int(trace["seq"]),
                        trace["trigger_path"],
                        trace["affected_path"],
                        int(trace["depth"]),
                        trace["relation"],
                        int(trace["ledger_version"]),
                    ),
                )
            for result in propagation_node_results:
                connection.execute(
                    "INSERT OR IGNORE INTO propagation_node_results(result_id, batch_id, trace_id, "
                    "node_id, affected_path, status, before_etag, after_etag, entry_version, "
                    "replaced_entries, detail_json, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(result["result_id"]),
                        result["batch_id"],
                        int(result["trace_id"]),
                        result["node_id"],
                        result["affected_path"],
                        result["status"],
                        result.get("before_etag"),
                        result.get("after_etag"),
                        result.get("entry_version"),
                        int(result.get("replaced_entries", 0)),
                        result["detail_json"],
                        result["occurred_at"],
                    ),
                )

            return {
                "new_ledger_version": new_version,
                "previous_ledger_version": active.ledger_version,
                "previous_policy_version": active.policy_version,
                "previous_content_set_hash": active.content_set_hash,
                "restored_policy_version": policy.policy_version,
                "restored_content_set_hash": content_hash,
                "removed_entries": len(previous_entries),
                "inserted_entries": inserted,
                "replaced_entries": len(previous_entries) + inserted,
            }

    def health(self) -> dict[str, Any]:
        active = self.active_state()
        return {
            "ledger_version": active.ledger_version,
            "policy_version": active.policy_version,
            "policy_hash": active.policy_hash,
            "content_set_hash": active.content_set_hash,
            "content_versions": active.content.versions,
            "ledger_max_version": self.newest_ledger_version(),
            "cache_entry_count": len(self.list_entries()),
            "eviction_count": len(self.list_evictions(limit=1000000)),
        }


def content_batch_id(ledger_version: int, trigger_path: str, content_hash: str) -> str:
    slug = trigger_path.strip("/").replace("/", "-") or "root"
    return f"prop-{ledger_version:06d}-{slug}-{content_hash[:10]}"


def policy_fingerprint(policy_doc: Mapping[str, Any]) -> str:
    return stable_hash(policy_doc)
