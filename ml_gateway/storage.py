from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_json, sha256_json
from .errors import SnapshotError, StateError


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS current_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    state_json TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    view_version INTEGER NOT NULL,
    stats_version INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    backend_fingerprints_json TEXT NOT NULL,
    backend_load_sequences_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backend_current (
    backend_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    max_concurrency INTEGER NOT NULL,
    parameter_length INTEGER NOT NULL,
    load_sequence INTEGER NOT NULL,
    content_signature TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    canonical_vector TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_current (
    entry_id TEXT PRIMARY KEY,
    entry_seq INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    request_ordinal INTEGER NOT NULL,
    attempt_ordinal INTEGER NOT NULL,
    view_version INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    backend_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    bound_fingerprint TEXT NOT NULL,
    bound_load_sequence INTEGER NOT NULL,
    output_summary TEXT,
    effective INTEGER NOT NULL,
    committed INTEGER NOT NULL,
    void INTEGER NOT NULL,
    invalid_reason TEXT,
    invalidated_by_fingerprint TEXT,
    redispatch_of_entry_id TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS ledger_one_effective_per_request
ON ledger_current(request_id) WHERE effective = 1;

CREATE TABLE IF NOT EXISTS batch_current (
    batch_id TEXT PRIMARY KEY,
    batch_seq INTEGER NOT NULL,
    view_version INTEGER NOT NULL,
    rule_id TEXT NOT NULL,
    backend_id TEXT NOT NULL,
    bound_fingerprint TEXT NOT NULL,
    bound_load_sequence INTEGER NOT NULL,
    status TEXT NOT NULL,
    output_summary TEXT,
    duration_ms INTEGER,
    error_code TEXT,
    error_message TEXT,
    request_ids_json TEXT NOT NULL,
    requests_json TEXT NOT NULL,
    redispatch_of TEXT,
    superseded_by TEXT,
    created_epoch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stats_versions (
    stats_version INTEGER PRIMARY KEY,
    readonly INTEGER NOT NULL,
    reason TEXT NOT NULL,
    previous_version INTEGER,
    view_version INTEGER NOT NULL,
    created_logical_epoch INTEGER NOT NULL,
    stats_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_id TEXT PRIMARY KEY,
    ordinal INTEGER NOT NULL UNIQUE,
    label TEXT NOT NULL,
    created_logical_epoch INTEGER NOT NULL,
    view_version INTEGER NOT NULL,
    rule_version TEXT NOT NULL,
    stats_version INTEGER NOT NULL,
    backend_fingerprints_json TEXT NOT NULL,
    backend_load_sequences_json TEXT NOT NULL,
    replay_requests_json TEXT NOT NULL,
    replay_expected_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    state_digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS log_events (
    event_seq INTEGER PRIMARY KEY,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
"""


class SessionStore:
    """SQLite-backed current view plus append-only stats and snapshot history.

    Current-view tables are always deleted and rebuilt in one transaction. This
    makes snapshot restore and fingerprint evolution a whole-view replacement,
    never an incremental merge.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            str(self.path), timeout=30.0, check_same_thread=False
        )
        self._connection.row_factory = sqlite3.Row
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=DELETE")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.executescript(SCHEMA_SQL)
            self._connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', '1')"
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _fetchone(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._connection.execute(sql, tuple(params)).fetchone()

    def _fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._connection.execute(sql, tuple(params)).fetchall())

    def replace_current_view(self, state: dict[str, Any]) -> str:
        serialized = canonical_json(state)
        digest = sha256_json(state)
        backend_states = sorted(
            state.get("backend_states", []), key=lambda item: item["backend_id"]
        )
        ledger = sorted(
            state.get("ledger", []),
            key=lambda item: (item["entry_seq"], item["entry_id"]),
        )
        batches = sorted(
            state.get("batches", []),
            key=lambda item: (item["batch_seq"], item["batch_id"]),
        )
        fingerprints = {
            item["backend_id"]: item["fingerprint"] for item in backend_states
        }
        load_sequences = {
            item["backend_id"]: item["load_sequence"] for item in backend_states
        }
        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM backend_current")
                connection.execute("DELETE FROM ledger_current")
                connection.execute("DELETE FROM batch_current")
                for backend in backend_states:
                    connection.execute(
                        """
                        INSERT INTO backend_current(
                            backend_id, model_id, host, port, max_concurrency,
                            parameter_length, load_sequence, content_signature,
                            config_digest, fingerprint, canonical_vector
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            backend["backend_id"],
                            backend["model_id"],
                            backend["host"],
                            int(backend["port"]),
                            int(backend["max_concurrency"]),
                            int(backend["parameter_length"]),
                            int(backend["load_sequence"]),
                            backend["content_signature"],
                            backend["config_digest"],
                            backend["fingerprint"],
                            backend["canonical_vector"],
                        ),
                    )
                for entry in ledger:
                    connection.execute(
                        """
                        INSERT INTO ledger_current(
                            entry_id, entry_seq, request_id, request_ordinal,
                            attempt_ordinal, view_version, rule_id, backend_id,
                            batch_id, bound_fingerprint, bound_load_sequence,
                            output_summary, effective, committed, void,
                            invalid_reason, invalidated_by_fingerprint,
                            redispatch_of_entry_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            entry["entry_id"],
                            int(entry["entry_seq"]),
                            entry["request_id"],
                            int(entry["request_ordinal"]),
                            int(entry["attempt_ordinal"]),
                            int(entry.get("view_version", state["view_version"])),
                            entry["rule_id"],
                            entry["backend_id"],
                            entry["batch_id"],
                            entry["bound_fingerprint"],
                            int(entry["bound_load_sequence"]),
                            entry.get("output_summary"),
                            1 if entry.get("effective") else 0,
                            1 if entry.get("committed") else 0,
                            1 if entry.get("void") else 0,
                            entry.get("invalid_reason"),
                            entry.get("invalidated_by_fingerprint"),
                            entry.get("redispatch_of_entry_id"),
                        ),
                    )
                for batch in batches:
                    connection.execute(
                        """
                        INSERT INTO batch_current(
                            batch_id, batch_seq, view_version, rule_id, backend_id,
                            bound_fingerprint, bound_load_sequence, status,
                            output_summary, duration_ms, error_code, error_message,
                            request_ids_json, requests_json, redispatch_of,
                            superseded_by, created_epoch
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            batch["batch_id"],
                            int(batch["batch_seq"]),
                            int(batch.get("view_version", state["view_version"])),
                            batch["rule_id"],
                            batch["backend_id"],
                            batch["bound_fingerprint"],
                            int(batch["bound_load_sequence"]),
                            batch["status"],
                            batch.get("output_summary"),
                            batch.get("duration_ms"),
                            batch.get("error_code"),
                            batch.get("error_message"),
                            canonical_json(batch.get("request_ids", [])),
                            canonical_json(batch.get("requests", [])),
                            batch.get("redispatch_of"),
                            batch.get("superseded_by"),
                            int(batch.get("created_epoch", state["event_epoch"])),
                        ),
                    )
                connection.execute("DELETE FROM current_state")
                connection.execute(
                    """
                    INSERT INTO current_state(
                        singleton, state_json, state_digest, view_version,
                        stats_version, rule_version, backend_fingerprints_json,
                        backend_load_sequences_json
                    ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        serialized,
                        digest,
                        int(state["view_version"]),
                        int(state["stats_version"]),
                        state["rule_version"],
                        canonical_json(fingerprints),
                        canonical_json(load_sequences),
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return digest

    def get_current_state(self) -> dict[str, Any] | None:
        row = self._fetchone("SELECT state_json FROM current_state WHERE singleton = 1")
        if row is None:
            return None
        return json.loads(row["state_json"])

    def current_digest(self) -> str | None:
        row = self._fetchone("SELECT state_digest FROM current_state WHERE singleton = 1")
        return None if row is None else str(row["state_digest"])

    def current_fingerprints(self) -> dict[str, str]:
        row = self._fetchone(
            "SELECT backend_fingerprints_json FROM current_state WHERE singleton = 1"
        )
        if row is None:
            return {}
        return dict(sorted(json.loads(row["backend_fingerprints_json"]).items()))

    def current_load_sequences(self) -> dict[str, int]:
        row = self._fetchone(
            "SELECT backend_load_sequences_json FROM current_state WHERE singleton = 1"
        )
        if row is None:
            return {}
        return dict(sorted(json.loads(row["backend_load_sequences_json"]).items()))

    def list_backends(self) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM backend_current ORDER BY backend_id")
        return [dict(row) for row in rows]

    def list_ledger(self) -> list[dict[str, Any]]:
        rows = self._fetchall(
            "SELECT * FROM ledger_current ORDER BY request_ordinal, attempt_ordinal, entry_id"
        )
        return [dict(row) for row in rows]

    def list_batches(self) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM batch_current ORDER BY batch_seq, batch_id")
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["request_ids"] = json.loads(value.pop("request_ids_json"))
            value["requests"] = json.loads(value.pop("requests_json"))
            result.append(value)
        return result

    def next_stats_version(self) -> int:
        row = self._fetchone("SELECT COALESCE(MAX(stats_version), 0) + 1 AS value FROM stats_versions")
        return int(row["value"]) if row is not None else 1

    def save_stats_version(
        self,
        *,
        stats_version: int,
        stats: dict[str, Any],
        metadata: dict[str, Any],
        view_version: int,
        logical_epoch: int,
        reason: str,
        previous_version: int | None,
        readonly: bool = False,
    ) -> None:
        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE stats_versions SET readonly = 1")
                connection.execute(
                    """
                    INSERT INTO stats_versions(
                        stats_version, readonly, reason, previous_version,
                        view_version, created_logical_epoch, stats_json, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(stats_version),
                        1 if readonly else 0,
                        reason,
                        previous_version,
                        int(view_version),
                        int(logical_epoch),
                        canonical_json(stats),
                        canonical_json(metadata),
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise StateError(
                    "statistics version already exists", stats_version=stats_version
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def get_stats_version(self, stats_version: int) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT * FROM stats_versions WHERE stats_version = ?", (stats_version,)
        )
        if row is None:
            return None
        value = dict(row)
        value["stats"] = json.loads(value.pop("stats_json"))
        value["metadata"] = json.loads(value.pop("metadata_json"))
        value["readonly"] = bool(value["readonly"])
        return value

    def mark_stats_current(self, stats_version: int) -> None:
        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE stats_versions SET readonly = 1")
                cursor = connection.execute(
                    "UPDATE stats_versions SET readonly = 0 WHERE stats_version = ?",
                    (int(stats_version),),
                )
                if cursor.rowcount != 1:
                    raise StateError(
                        "snapshot references a missing statistics version",
                        stats_version=stats_version,
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def list_stats_versions(self) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM stats_versions ORDER BY stats_version")
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["stats"] = json.loads(value.pop("stats_json"))
            value["metadata"] = json.loads(value.pop("metadata_json"))
            value["readonly"] = bool(value["readonly"])
            result.append(value)
        return result

    def save_snapshot(
        self,
        *,
        state: dict[str, Any],
        label: str,
        replay_requests: list[dict[str, Any]],
        replay_expected: list[dict[str, Any]],
    ) -> dict[str, Any]:
        self.replace_current_view(state)
        ordinal_row = self._fetchone("SELECT COALESCE(MAX(ordinal), 0) + 1 AS value FROM snapshots")
        ordinal = int(ordinal_row["value"]) if ordinal_row is not None else 1
        state_json = canonical_json(state)
        state_digest = sha256_json(state)
        replay_payload = {
            "replay_expected": replay_expected,
            "replay_requests": replay_requests,
            "state_digest": state_digest,
        }
        snapshot_id = f"snap-{ordinal:04d}-{sha256_json(replay_payload)[:12]}"
        fingerprints = {
            backend["backend_id"]: backend["fingerprint"]
            for backend in sorted(state["backend_states"], key=lambda item: item["backend_id"])
        }
        load_sequences = {
            backend["backend_id"]: backend["load_sequence"]
            for backend in sorted(state["backend_states"], key=lambda item: item["backend_id"])
        }
        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO snapshots(
                        snapshot_id, ordinal, label, created_logical_epoch,
                        view_version, rule_version, stats_version,
                        backend_fingerprints_json, backend_load_sequences_json,
                        replay_requests_json, replay_expected_json, state_json,
                        state_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        ordinal,
                        label,
                        int(state["event_epoch"]),
                        int(state["view_version"]),
                        state["rule_version"],
                        int(state["stats_version"]),
                        canonical_json(fingerprints),
                        canonical_json(load_sequences),
                        canonical_json(replay_requests),
                        canonical_json(replay_expected),
                        state_json,
                        state_digest,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return self.get_snapshot(snapshot_id)

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        row = self._fetchone("SELECT * FROM snapshots WHERE snapshot_id = ?", (snapshot_id,))
        if row is None:
            raise SnapshotError("snapshot does not exist", snapshot_id=snapshot_id)
        value = dict(row)
        value["backend_fingerprints"] = json.loads(value.pop("backend_fingerprints_json"))
        value["backend_load_sequences"] = json.loads(value.pop("backend_load_sequences_json"))
        value["replay_requests"] = json.loads(value.pop("replay_requests_json"))
        value["replay_expected"] = json.loads(value.pop("replay_expected_json"))
        value["state"] = json.loads(value.pop("state_json"))
        return value

    def list_snapshots(self) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT snapshot_id, ordinal, label, created_logical_epoch, view_version,
                   rule_version, stats_version, backend_fingerprints_json,
                   backend_load_sequences_json, state_digest
            FROM snapshots ORDER BY ordinal
            """
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["backend_fingerprints"] = json.loads(
                value.pop("backend_fingerprints_json")
            )
            value["backend_load_sequences"] = json.loads(
                value.pop("backend_load_sequences_json")
            )
            result.append(value)
        return result

    def latest_snapshot(self) -> dict[str, Any] | None:
        row = self._fetchone("SELECT snapshot_id FROM snapshots ORDER BY ordinal DESC LIMIT 1")
        if row is None:
            return None
        return self.get_snapshot(str(row["snapshot_id"]))

    def next_log_sequence(self) -> int:
        row = self._fetchone("SELECT COALESCE(MAX(event_seq), 0) + 1 AS value FROM log_events")
        return int(row["value"]) if row is not None else 1

    def append_log_event(
        self,
        *,
        event_seq: int,
        level: str,
        code: str,
        message: str,
        payload: dict[str, Any],
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO log_events(event_seq, level, code, message, payload_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_seq, level, code, message, canonical_json(payload)),
            )
            self._connection.commit()

    def append_log_event_auto(
        self,
        *,
        level: str,
        code: str,
        message: str,
        payload: dict[str, Any],
    ) -> int:
        """Atomically allocate and append one logical log sequence number."""

        with self._lock:
            connection = self._connection
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT COALESCE(MAX(event_seq), 0) + 1 AS value FROM log_events"
                ).fetchone()
                event_seq = int(row["value"])
                connection.execute(
                    """
                    INSERT INTO log_events(event_seq, level, code, message, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (event_seq, level, code, message, canonical_json(payload)),
                )
                connection.commit()
                return event_seq
            except Exception:
                connection.rollback()
                raise

    def list_log_events(self) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM log_events ORDER BY event_seq")
        result: list[dict[str, Any]] = []
        for row in rows:
            value = dict(row)
            value["payload"] = json.loads(value.pop("payload_json"))
            result.append(value)
        return result
