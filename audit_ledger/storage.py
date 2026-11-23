from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path
from typing import Any, Iterator

from .errors import AuditError
from .locks import exclusive_file_lock
from .models import (
    AuditEvent,
    InputBundle,
    LedgerItem,
    Policy,
    QuotaConfig,
    canonical_json,
    logical_window,
    payload_bytes,
    sha256_digest,
)
from .validation import parse_policy, parse_quotas


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    config_json TEXT NOT NULL,
    PRIMARY KEY (kind, version)
);

CREATE TABLE IF NOT EXISTS submissions (
    event_seq INTEGER NOT NULL,
    event_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    category TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    logical_ts_ms INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    stream_version INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    event_digest TEXT NOT NULL,
    PRIMARY KEY (event_seq, event_version)
);

CREATE TABLE IF NOT EXISTS work_queue (
    event_seq INTEGER NOT NULL,
    event_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_ts_ms INTEGER,
    last_error TEXT,
    updated_ledger_version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (event_seq, event_version)
);

CREATE INDEX IF NOT EXISTS idx_work_queue_state
    ON work_queue (state, event_seq, event_version);

CREATE TABLE IF NOT EXISTS processed_events (
    event_seq INTEGER NOT NULL,
    event_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    category TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    logical_ts_ms INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_bytes INTEGER NOT NULL,
    window_id TEXT NOT NULL,
    status TEXT NOT NULL,
    verdict TEXT NOT NULL,
    error_code TEXT,
    decision_json TEXT NOT NULL,
    decision_digest TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    PRIMARY KEY (event_seq, event_version)
);

CREATE INDEX IF NOT EXISTS idx_processed_tenant_window
    ON processed_events (tenant, window_id, status);

CREATE TABLE IF NOT EXISTS audit_events (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq INTEGER NOT NULL,
    event_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    category TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    logical_ts_ms INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_bytes INTEGER NOT NULL,
    window_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    UNIQUE (event_seq, event_version)
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_window
    ON audit_events (tenant, window_id);

CREATE TABLE IF NOT EXISTS audit_archive (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq INTEGER NOT NULL,
    event_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    category TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    logical_ts_ms INTEGER NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    event_bytes INTEGER NOT NULL,
    window_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    UNIQUE (event_seq, event_version)
);

CREATE INDEX IF NOT EXISTS idx_archive_tenant_window
    ON audit_archive (tenant, window_id);

CREATE TABLE IF NOT EXISTS ledger_versions (
    version INTEGER PRIMARY KEY,
    parent_version INTEGER,
    ledger_digest TEXT NOT NULL,
    items_json TEXT NOT NULL,
    event_watermark INTEGER NOT NULL,
    logical_clock_watermark INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_ledger (
    ledger_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    window_id TEXT NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    used_bytes INTEGER NOT NULL,
    archived_bytes INTEGER NOT NULL,
    written_count INTEGER NOT NULL,
    archived_count INTEGER NOT NULL,
    overflow_count INTEGER NOT NULL,
    PRIMARY KEY (ledger_version, tenant, window_id)
);

CREATE INDEX IF NOT EXISTS idx_tenant_ledger_lookup
    ON tenant_ledger (ledger_version, tenant, window_id);

CREATE TABLE IF NOT EXISTS tenant_clocks (
    tenant TEXT PRIMARY KEY,
    watermark INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_queue (
    queue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_seq INTEGER NOT NULL,
    event_version INTEGER NOT NULL,
    tenant TEXT NOT NULL,
    window_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    reason TEXT NOT NULL,
    attempts INTEGER NOT NULL,
    next_attempt_ts_ms INTEGER,
    resolved INTEGER NOT NULL DEFAULT 0,
    UNIQUE (event_seq, event_version)
);

CREATE TABLE IF NOT EXISTS write_intents (
    intent_id TEXT PRIMARY KEY,
    round_no INTEGER NOT NULL,
    start_clock_ms INTEGER NOT NULL,
    event_keys_json TEXT NOT NULL,
    next_index INTEGER NOT NULL,
    last_ledger_version INTEGER NOT NULL,
    state TEXT NOT NULL,
    report_json TEXT NOT NULL
);
"""


def ledger_digest(items: dict[tuple[str, str], LedgerItem]) -> str:
    payload = [
        items[key].to_dict()
        for key in sorted(items, key=lambda value: (value[0], value[1]))
    ]
    return sha256_digest(payload)


def _now_key(event: AuditEvent) -> tuple[int, int]:
    return event.event_seq, event.event_version


class AuditStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.lock_path = Path(str(self.db_path) + ".lock")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with exclusive_file_lock(self.lock_path):
            yield

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute("PRAGMA foreign_keys = ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, connection: sqlite3.Connection) -> Iterator[None]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def initialize(self) -> None:
        with self.exclusive():
            with self.connection() as connection:
                self.ensure_schema(connection)

    def ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA)
        with self.transaction(connection):
            empty_digest = ledger_digest({})
            defaults = {
                "schema_version": "1",
                "current_ledger_version": "0",
                "ledger_digest": empty_digest,
                "event_watermark": "0",
                "logical_clock_watermark": "0",
                "current_quota_version": "0",
                "current_policy_version": "0",
            }
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)",
                    (key, value),
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO ledger_versions
                    (version, parent_version, ledger_digest, items_json,
                     event_watermark, logical_clock_watermark)
                VALUES (0, NULL, ?, '[]', 0, 0)
                """,
                (empty_digest,),
            )

    def get_meta(self, connection: sqlite3.Connection, key: str) -> str:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            raise AuditError("AUD-STATE-CORRUPT", f"missing meta key: {key}")
        return str(row["value"])

    def set_meta(
        self, connection: sqlite3.Connection, key: str, value: str | int
    ) -> None:
        connection.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    def current_versions(self, connection: sqlite3.Connection) -> tuple[int, int, int]:
        return (
            int(self.get_meta(connection, "current_ledger_version")),
            int(self.get_meta(connection, "current_quota_version")),
            int(self.get_meta(connection, "current_policy_version")),
        )

    def load_configs(
        self, connection: sqlite3.Connection
    ) -> tuple[QuotaConfig, Policy]:
        _, quota_version, policy_version = self.current_versions(connection)
        quota_row = connection.execute(
            "SELECT config_json FROM config_versions WHERE kind = 'quota' AND version = ?",
            (quota_version,),
        ).fetchone()
        policy_row = connection.execute(
            "SELECT config_json FROM config_versions WHERE kind = 'policy' AND version = ?",
            (policy_version,),
        ).fetchone()
        if quota_row is None or policy_row is None:
            raise AuditError(
                "AUD-CONFIG-MISSING",
                "load a quota and policy configuration before writing",
            )
        return (
            parse_quotas(json.loads(quota_row["config_json"])),
            parse_policy(json.loads(policy_row["config_json"])),
        )

    def _upsert_config(
        self,
        connection: sqlite3.Connection,
        kind: str,
        version: int,
        digest: str,
        config_json: str,
        regression_code: str,
    ) -> None:
        meta_key = f"current_{kind}_version"
        current = int(self.get_meta(connection, meta_key))
        if version < current:
            raise AuditError(
                regression_code,
                f"{kind} configuration version regressed from {current} to {version}",
                details={"current_version": current, "incoming_version": version},
            )
        if version == current:
            row = connection.execute(
                "SELECT digest FROM config_versions WHERE kind = ? AND version = ?",
                (kind, version),
            ).fetchone()
            if row is not None and str(row["digest"]) != digest:
                raise AuditError(
                    f"AUD-{kind.upper()}-CONFIG-CONFLICT",
                    f"{kind} configuration changed without a version increase",
                    details={"version": version},
                )
            return
        connection.execute(
            """
            INSERT INTO config_versions (kind, version, digest, config_json)
            VALUES (?, ?, ?, ?)
            """,
            (kind, version, digest, config_json),
        )
        self.set_meta(connection, meta_key, version)

    def load_bundle(self, bundle: InputBundle, source: str = "load") -> dict[str, Any]:
        with self.exclusive():
            with self.connection() as connection:
                with self.transaction(connection):
                    self._upsert_config(
                        connection,
                        "quota",
                        bundle.quotas.quota_version,
                        bundle.quotas.digest,
                        canonical_json(bundle.quotas.to_dict()),
                        "AUD-QUOTA-VERSION-REGRESSION",
                    )
                    self._upsert_config(
                        connection,
                        "policy",
                        bundle.policy.policy_version,
                        bundle.policy.digest,
                        canonical_json(bundle.policy.to_dict()),
                        "AUD-POLICY-VERSION-REGRESSION",
                    )
                    counters = {
                        "submitted": len(bundle.events),
                        "inserted": 0,
                        "already_present": 0,
                        "already_processed": 0,
                        "queued": 0,
                    }
                    for event in bundle.events:
                        self._validate_database_submission(
                            connection, event, bundle.policy
                        )
                        existing = connection.execute(
                            """
                            SELECT event_digest FROM submissions
                            WHERE event_seq = ? AND event_version = ?
                            """,
                            (event.event_seq, event.event_version),
                        ).fetchone()
                        if existing is None:
                            connection.execute(
                                """
                                INSERT INTO submissions
                                    (event_seq, event_version, tenant, category,
                                     payload_digest, logical_ts_ms, idempotency_key,
                                     stream_version, event_json, event_digest)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    event.event_seq,
                                    event.event_version,
                                    event.tenant,
                                    event.category,
                                    event.payload_digest,
                                    event.logical_ts_ms,
                                    event.idempotency_key,
                                    event.stream_version,
                                    canonical_json(event.to_dict()),
                                    event.event_digest,
                                ),
                            )
                            counters["inserted"] += 1
                        else:
                            if str(existing["event_digest"]) != event.event_digest:
                                raise AuditError(
                                    "AUD-EVENT-CONFLICT",
                                    "same event_seq/event_version has different content",
                                    tenant=event.tenant,
                                    window_id=logical_window(
                                        event.logical_ts_ms, bundle.policy.window_ms
                                    )[0],
                                    details={"event_seq": event.event_seq},
                                )
                            counters["already_present"] += 1
                        processed = connection.execute(
                            """
                            SELECT 1 FROM processed_events
                            WHERE event_seq = ? AND event_version = ?
                            """,
                            (event.event_seq, event.event_version),
                        ).fetchone()
                        if processed is not None:
                            counters["already_processed"] += 1
                            if source == "replay":
                                connection.execute(
                                    """
                                    INSERT INTO work_queue
                                        (event_seq, event_version, state, attempts)
                                    VALUES (?, ?, 'pending', 0)
                                    ON CONFLICT(event_seq, event_version)
                                    DO UPDATE SET state = 'pending'
                                    """,
                                    (event.event_seq, event.event_version),
                                )
                            continue
                        queued = connection.execute(
                            """
                            SELECT 1 FROM work_queue
                            WHERE event_seq = ? AND event_version = ?
                            """,
                            (event.event_seq, event.event_version),
                        ).fetchone()
                        if queued is None:
                            connection.execute(
                                """
                                INSERT INTO work_queue
                                    (event_seq, event_version, state, attempts)
                                VALUES (?, ?, 'pending', 0)
                                """,
                                (event.event_seq, event.event_version),
                            )
                            counters["queued"] += 1
                return {
                    "ok": True,
                    "source": source,
                    "stream_version": bundle.stream_version,
                    "quota_version": bundle.quotas.quota_version,
                    "policy_version": bundle.policy.policy_version,
                    **counters,
                }

    def _validate_database_submission(
        self,
        connection: sqlite3.Connection,
        event: AuditEvent,
        policy: Policy,
    ) -> None:
        window_id = logical_window(event.logical_ts_ms, policy.window_ms)[0]
        row = connection.execute(
            """
            SELECT event_digest FROM submissions
            WHERE event_seq = ? AND event_version = ?
            """,
            (event.event_seq, event.event_version),
        ).fetchone()
        if row is not None and str(row["event_digest"]) != event.event_digest:
            raise AuditError(
                "AUD-EVENT-CONFLICT",
                "submitted event differs from the persisted event version",
                tenant=event.tenant,
                window_id=window_id,
                details={"event_seq": event.event_seq},
            )
        idem = connection.execute(
            """
            SELECT event_seq, event_version, event_digest
            FROM submissions WHERE idempotency_key = ?
            """,
            (event.idempotency_key,),
        ).fetchone()
        if idem is not None and str(idem["event_digest"]) != event.event_digest:
            raise AuditError(
                "AUD-IDEMPOTENCY-CONFLICT",
                "idempotency_key is already bound to different event content",
                tenant=event.tenant,
                window_id=window_id,
                details={
                    "idempotency_key": event.idempotency_key,
                    "existing_event_seq": int(idem["event_seq"]),
                },
            )

    def load_ledger_items(
        self,
        connection: sqlite3.Connection,
        version: int | None = None,
    ) -> dict[tuple[str, str], LedgerItem]:
        if version is None:
            version = int(self.get_meta(connection, "current_ledger_version"))
        rows = connection.execute(
            "SELECT * FROM tenant_ledger WHERE ledger_version = ?",
            (version,),
        ).fetchall()
        result: dict[tuple[str, str], LedgerItem] = {}
        for row in rows:
            item = LedgerItem(
                tenant=str(row["tenant"]),
                window_id=str(row["window_id"]),
                window_start_ms=int(row["window_start_ms"]),
                window_end_ms=int(row["window_end_ms"]),
                used_bytes=int(row["used_bytes"]),
                archived_bytes=int(row["archived_bytes"]),
                written_count=int(row["written_count"]),
                archived_count=int(row["archived_count"]),
                overflow_count=int(row["overflow_count"]),
            )
            result[(item.tenant, item.window_id)] = item
        return result

    def assert_ledger_digest(self, connection: sqlite3.Connection) -> None:
        version = int(self.get_meta(connection, "current_ledger_version"))
        expected = str(self.get_meta(connection, "ledger_digest"))
        snapshot = connection.execute(
            """
            SELECT ledger_digest, items_json, event_watermark,
                   logical_clock_watermark
            FROM ledger_versions WHERE version = ?
            """,
            (version,),
        ).fetchone()
        items = self.load_ledger_items(connection, version)
        actual = ledger_digest(items)
        expected_items = [
            items[key].to_dict()
            for key in sorted(items, key=lambda value: (value[0], value[1]))
        ]
        event_watermark = int(self.get_meta(connection, "event_watermark"))
        logical_clock = int(
            self.get_meta(connection, "logical_clock_watermark")
        )
        consistent = (
            snapshot is not None
            and actual == expected
            and str(snapshot["ledger_digest"]) == actual
            and canonical_json(json.loads(str(snapshot["items_json"])))
            == canonical_json(expected_items)
            and int(snapshot["event_watermark"]) == event_watermark
            and int(snapshot["logical_clock_watermark"]) == logical_clock
        )
        if not consistent:
            first = sorted(items)[0] if items else None
            raise AuditError(
                "AUD-LEDGER-DIGEST-MISMATCH",
                "current ledger state does not match its committed snapshot",
                tenant=first[0] if first else None,
                window_id=first[1] if first else None,
                details={
                    "ledger_version": version,
                    "event_watermark": event_watermark,
                    "logical_clock_watermark": logical_clock,
                },
            )

    def commit_ledger(
        self,
        connection: sqlite3.Connection,
        items: dict[tuple[str, str], LedgerItem],
        event_watermark: int,
        logical_clock_watermark: int,
    ) -> int:
        current = int(self.get_meta(connection, "current_ledger_version"))
        new_version = current + 1
        digest = ledger_digest(items)
        ordered = [
            items[key].to_dict()
            for key in sorted(items, key=lambda value: (value[0], value[1]))
        ]
        for item in ordered:
            connection.execute(
                """
                INSERT INTO tenant_ledger
                    (ledger_version, tenant, window_id, window_start_ms,
                     window_end_ms, used_bytes, archived_bytes, written_count,
                     archived_count, overflow_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_version,
                    item["tenant"],
                    item["window_id"],
                    item["window_start_ms"],
                    item["window_end_ms"],
                    item["used_bytes"],
                    item["archived_bytes"],
                    item["written_count"],
                    item["archived_count"],
                    item["overflow_count"],
                ),
            )
        connection.execute(
            """
            INSERT INTO ledger_versions
                (version, parent_version, ledger_digest, items_json,
                 event_watermark, logical_clock_watermark)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                new_version,
                current,
                digest,
                canonical_json(ordered),
                event_watermark,
                logical_clock_watermark,
            ),
        )
        self.set_meta(connection, "current_ledger_version", new_version)
        self.set_meta(connection, "ledger_digest", digest)
        self.set_meta(connection, "event_watermark", event_watermark)
        self.set_meta(connection, "logical_clock_watermark", logical_clock_watermark)
        return new_version

    def advance_clock(self, amount_ms: int) -> dict[str, int]:
        if not isinstance(amount_ms, int) or amount_ms < 0:
            raise AuditError(
                "AUD-CLOCK-INVALID", "clock advance must be a non-negative integer"
            )
        with self.exclusive():
            with self.connection() as connection:
                with self.transaction(connection):
                    self.assert_ledger_digest(connection)
                    watermark = int(
                        self.get_meta(connection, "logical_clock_watermark")
                    )
                    updated = watermark + amount_ms
                    items = self.load_ledger_items(connection)
                    event_watermark = int(self.get_meta(connection, "event_watermark"))
                    version = self.commit_ledger(
                        connection, items, event_watermark, updated
                    )
                return {
                    "previous_watermark": watermark,
                    "logical_clock_watermark": updated,
                    "ledger_version": version,
                }

    def status(self) -> dict[str, Any]:
        with self.exclusive():
            with self.connection() as connection:
                quota, policy = self.load_configs(connection)
                version, quota_version, policy_version = self.current_versions(connection)
                items = self.load_ledger_items(connection, version)
                quota_map = quota.by_tenant()
                manual_rows = connection.execute(
                    """
                    SELECT tenant, window_id, COUNT(*) AS count
                    FROM manual_queue WHERE resolved = 0
                    GROUP BY tenant, window_id
                    """
                ).fetchall()
                manual_by_window = {
                    (str(row["tenant"]), str(row["window_id"])): int(row["count"])
                    for row in manual_rows
                }
                processed_windows = {
                    (str(row["tenant"]), str(row["window_id"])): int(
                        row["logical_ts_ms"]
                    )
                    for row in connection.execute(
                        """
                        SELECT tenant, window_id, MIN(logical_ts_ms) AS logical_ts_ms
                        FROM processed_events
                        GROUP BY tenant, window_id
                        """
                    ).fetchall()
                }
                clock_rows = connection.execute(
                    "SELECT tenant, watermark FROM tenant_clocks"
                ).fetchall()
                tenant_clocks = {
                    str(row["tenant"]): int(row["watermark"]) for row in clock_rows
                }
                queue_rows = connection.execute(
                    "SELECT state, COUNT(*) AS count FROM work_queue GROUP BY state"
                ).fetchall()
                queue_by_state = {
                    str(row["state"]): int(row["count"]) for row in queue_rows
                }
                manual_queue = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT event_seq, event_version, tenant, window_id,
                               idempotency_key, reason_code, reason, attempts,
                               next_attempt_ts_ms
                        FROM manual_queue WHERE resolved = 0
                        ORDER BY tenant, window_id, event_seq, event_version
                        """
                    ).fetchall()
                ]
                tenants = sorted(
                    set(quota_map)
                    | {key[0] for key in items}
                    | set(tenant_clocks)
                    | {key[0] for key in processed_windows}
                    | {key[0] for key in manual_by_window}
                )
                tenant_payload: list[dict[str, Any]] = []
                total_manual = 0
                for tenant in tenants:
                    windows: list[dict[str, Any]] = []
                    tenant_manual = 0
                    window_keys = set(items)
                    window_keys.update(manual_by_window)
                    window_keys.update(processed_windows)
                    for item_tenant, window_id in sorted(window_keys):
                        if item_tenant != tenant:
                            continue
                        item = items.get((item_tenant, window_id))
                        if item is None:
                            logical_ts = processed_windows.get(
                                (item_tenant, window_id), 0
                            )
                            _, start, end = logical_window(
                                logical_ts, policy.window_ms
                            )
                            item = LedgerItem(
                                tenant=item_tenant,
                                window_id=window_id,
                                window_start_ms=start,
                                window_end_ms=end,
                                used_bytes=0,
                                archived_bytes=0,
                                written_count=0,
                                archived_count=0,
                                overflow_count=0,
                            )
                        declaration = quota_map.get(tenant)
                        quota_bytes = (
                            declaration.daily_byte_quota
                            if declaration is not None
                            else 0
                        )
                        pending = manual_by_window.get((tenant, window_id), 0)
                        tenant_manual += pending
                        windows.append(
                            {
                                **item.to_dict(),
                                "quota_bytes": quota_bytes,
                                "remaining_bytes": max(
                                    0, quota_bytes - item.used_bytes
                                ),
                                "manual_pending_count": pending,
                            }
                        )
                    total_manual += tenant_manual
                    tenant_payload.append(
                        {
                            "tenant": tenant,
                            "logical_clock_watermark": tenant_clocks.get(tenant, 0),
                            "manual_pending_count": tenant_manual,
                            "windows": windows,
                        }
                    )
                return {
                    "ok": True,
                    "current_ledger_version": version,
                    "ledger_digest": self.get_meta(connection, "ledger_digest"),
                    "event_watermark": int(self.get_meta(connection, "event_watermark")),
                    "logical_clock_watermark": int(
                        self.get_meta(connection, "logical_clock_watermark")
                    ),
                    "quota_version": quota_version,
                    "policy_version": policy_version,
                    "window_ms": policy.window_ms,
                    "queue_by_state": dict(sorted(queue_by_state.items())),
                    "manual_pending_count": total_manual,
                    "manual_queue": manual_queue,
                    "tenants": tenant_payload,
                }

    def verify(self, expected: dict[str, Any] | None = None) -> dict[str, Any]:
        with self.exclusive():
            with self.connection() as connection:
                quota, policy = self.load_configs(connection)
                version = int(self.get_meta(connection, "current_ledger_version"))
                mismatches: list[dict[str, Any]] = []
                try:
                    self.assert_ledger_digest(connection)
                except AuditError as exc:
                    mismatches.append(exc.to_dict())
                items = self.load_ledger_items(connection, version)
                rebuilt: dict[tuple[str, str], dict[str, Any]] = {}

                def ensure_window(
                    tenant: str, logical_ts_ms: int
                ) -> dict[str, Any]:
                    window_id, start, end = logical_window(
                        logical_ts_ms, policy.window_ms
                    )
                    key = (tenant, window_id)
                    if key not in rebuilt:
                        rebuilt[key] = {
                            "tenant": tenant,
                            "window_id": window_id,
                            "window_start_ms": start,
                            "window_end_ms": end,
                            "used_bytes": 0,
                            "archived_bytes": 0,
                            "written_count": 0,
                            "archived_count": 0,
                            "overflow_count": 0,
                        }
                    return rebuilt[key]

                for row in connection.execute(
                    "SELECT * FROM audit_events ORDER BY event_seq, event_version"
                ):
                    event = dict(row)
                    actual_digest = sha256_digest(
                        json.loads(str(event["payload_json"]))
                    )
                    actual_bytes = len(
                        payload_bytes(json.loads(str(event["payload_json"])))
                    )
                    expected_window = logical_window(
                        int(event["logical_ts_ms"]), policy.window_ms
                    )[0]
                    if actual_digest != event["payload_digest"]:
                        mismatches.append(
                            {
                                "code": "AUD-EVENT-DIGEST-MISMATCH",
                                "event_seq": event["event_seq"],
                                "tenant": event["tenant"],
                                "window_id": event["window_id"],
                            }
                        )
                    if actual_bytes != event["event_bytes"]:
                        mismatches.append(
                            {
                                "code": "AUD-EVENT-BYTE-SIZE-MISMATCH",
                                "event_seq": event["event_seq"],
                                "tenant": event["tenant"],
                                "window_id": event["window_id"],
                            }
                        )
                    if expected_window != event["window_id"]:
                        mismatches.append(
                            {
                                "code": "AUD-WINDOW-MISMATCH",
                                "event_seq": event["event_seq"],
                                "tenant": event["tenant"],
                                "window_id": event["window_id"],
                            }
                        )
                    bucket = ensure_window(
                        str(event["tenant"]), int(event["logical_ts_ms"])
                    )
                    bucket["used_bytes"] += actual_bytes
                    bucket["written_count"] += 1
                    if event["verdict"] == "overflow_allowed":
                        bucket["overflow_count"] += 1

                for row in connection.execute(
                    "SELECT * FROM audit_archive ORDER BY event_seq, event_version"
                ):
                    event = dict(row)
                    actual_digest = sha256_digest(
                        json.loads(str(event["payload_json"]))
                    )
                    actual_bytes = len(
                        payload_bytes(json.loads(str(event["payload_json"])))
                    )
                    expected_window = logical_window(
                        int(event["logical_ts_ms"]), policy.window_ms
                    )[0]
                    if actual_digest != event["payload_digest"]:
                        mismatches.append(
                            {
                                "code": "AUD-EVENT-DIGEST-MISMATCH",
                                "event_seq": event["event_seq"],
                                "tenant": event["tenant"],
                                "window_id": event["window_id"],
                            }
                        )
                    if expected_window != event["window_id"]:
                        mismatches.append(
                            {
                                "code": "AUD-WINDOW-MISMATCH",
                                "event_seq": event["event_seq"],
                                "tenant": event["tenant"],
                                "window_id": event["window_id"],
                            }
                        )
                    bucket = ensure_window(
                        str(event["tenant"]), int(event["logical_ts_ms"])
                    )
                    bucket["archived_bytes"] += actual_bytes
                    bucket["archived_count"] += 1

                rebuilt_items = {
                    key: LedgerItem.from_dict(value)
                    for key, value in rebuilt.items()
                }
                if ledger_digest(rebuilt_items) != ledger_digest(items):
                    all_keys = sorted(set(items) | set(rebuilt_items))
                    for key in all_keys:
                        left = items.get(key)
                        right = rebuilt_items.get(key)
                        if left != right:
                            mismatches.append(
                                {
                                    "code": "AUD-LEDGER-REBUILD-MISMATCH",
                                    "tenant": key[0],
                                    "window_id": key[1],
                                    "ledger": left.to_dict() if left else None,
                                    "rebuilt": right.to_dict() if right else None,
                                }
                            )
                            break

                processed_rows = [
                    dict(row)
                    for row in connection.execute(
                        """
                        SELECT event_seq, event_version, tenant, category,
                               payload_digest, logical_ts_ms, idempotency_key,
                               event_bytes, window_id, status, verdict,
                               error_code, decision_digest
                        FROM processed_events
                        ORDER BY event_seq, event_version
                        """
                    )
                ]
                events_summary = [
                    {
                        "event_seq": row["event_seq"],
                        "event_version": row["event_version"],
                        "tenant": row["tenant"],
                        "status": row["status"],
                        "verdict": row["verdict"],
                        "error_code": row["error_code"],
                        "window_id": row["window_id"],
                        "event_bytes": row["event_bytes"],
                        "idempotency_key": row["idempotency_key"],
                    }
                    for row in processed_rows
                ]
                audit_digest = sha256_digest(events_summary)
                audit_rows = {
                    (int(row["event_seq"]), int(row["event_version"])): dict(row)
                    for row in connection.execute(
                        """
                        SELECT event_seq, event_version, tenant, window_id, verdict
                        FROM audit_events
                        """
                    ).fetchall()
                }
                archive_rows = {
                    (int(row["event_seq"]), int(row["event_version"])): dict(row)
                    for row in connection.execute(
                        """
                        SELECT event_seq, event_version, tenant, window_id, verdict
                        FROM audit_archive
                        """
                    ).fetchall()
                }
                processed_keys = {
                    (int(row["event_seq"]), int(row["event_version"]))
                    for row in processed_rows
                }
                for row in processed_rows:
                    key = (int(row["event_seq"]), int(row["event_version"]))
                    status = str(row["status"])
                    if status == "written":
                        if key not in audit_rows:
                            mismatches.append(
                                {
                                    "code": "AUD-PROCESSED-AUDIT-MISSING",
                                    "event_seq": key[0],
                                    "event_version": key[1],
                                    "tenant": row["tenant"],
                                    "window_id": row["window_id"],
                                }
                            )
                    elif status == "archived":
                        if key not in archive_rows:
                            mismatches.append(
                                {
                                    "code": "AUD-PROCESSED-ARCHIVE-MISSING",
                                    "event_seq": key[0],
                                    "event_version": key[1],
                                    "tenant": row["tenant"],
                                    "window_id": row["window_id"],
                                }
                            )
                    elif key in audit_rows or key in archive_rows:
                        mismatches.append(
                            {
                                "code": "AUD-TERMINAL-EVENT-MATERIALIZED",
                                "event_seq": key[0],
                                "event_version": key[1],
                                "status": status,
                                "tenant": row["tenant"],
                                "window_id": row["window_id"],
                            }
                        )
                for key, row in audit_rows.items():
                    if key not in processed_keys:
                        mismatches.append(
                            {
                                "code": "AUD-AUDIT-EVENT-UNTRACKED",
                                "event_seq": key[0],
                                "event_version": key[1],
                                "tenant": row["tenant"],
                                "window_id": row["window_id"],
                            }
                        )
                for key, row in archive_rows.items():
                    if key not in processed_keys:
                        mismatches.append(
                            {
                                "code": "AUD-ARCHIVE-EVENT-UNTRACKED",
                                "event_seq": key[0],
                                "event_version": key[1],
                                "tenant": row["tenant"],
                                "window_id": row["window_id"],
                            }
                        )
                materialized = connection.execute(
                    """
                    SELECT COUNT(*) AS count FROM (
                        SELECT event_seq, event_version FROM audit_events
                        UNION ALL
                        SELECT event_seq, event_version FROM audit_archive
                    )
                    """
                ).fetchone()
                processed_count = len(processed_rows)
                if materialized is not None and int(materialized["count"]) > processed_count:
                    mismatches.append(
                        {
                            "code": "AUD-MATERIALIZED-EVENT-UNTRACKED",
                            "count": int(materialized["count"]),
                        }
                    )

                event_watermark = int(self.get_meta(connection, "event_watermark"))
                logical_clock = int(
                    self.get_meta(connection, "logical_clock_watermark")
                )
                ledger_rows = [
                    {
                        "tenant": item.tenant,
                        "window_id": item.window_id,
                        "used_bytes": item.used_bytes,
                        "archived_bytes": item.archived_bytes,
                        "written_count": item.written_count,
                        "archived_count": item.archived_count,
                        "overflow_count": item.overflow_count,
                    }
                    for item in sorted(items.values(), key=lambda x: (x.tenant, x.window_id))
                ]
                if expected is not None:
                    comparisons = {
                        "record_count": processed_count,
                        "audit_digest": audit_digest,
                        "event_watermark": event_watermark,
                    }
                    for key, actual in comparisons.items():
                        if key in expected and expected[key] != actual:
                            mismatches.append(
                                {
                                    "code": "AUD-EXPECTED-SUMMARY-MISMATCH",
                                    "field": key,
                                    "expected": expected[key],
                                    "actual": actual,
                                }
                            )
                    if "ledger" in expected:
                        expected_ledger = expected["ledger"]
                        if canonical_json(ledger_rows) != canonical_json(expected_ledger):
                            mismatches.append(
                                {
                                    "code": "AUD-EXPECTED-LEDGER-MISMATCH",
                                    "expected": expected_ledger,
                                    "actual": ledger_rows,
                                }
                            )
                    if "events" in expected:
                        expected_events = expected["events"]
                        selected_events = [
                            {
                                key: value
                                for key, value in event.items()
                                if key in expected_events[0]
                            }
                            for event in events_summary
                        ] if expected_events else []
                        if canonical_json(selected_events) != canonical_json(
                            expected_events
                        ):
                            mismatches.append(
                                {
                                    "code": "AUD-EXPECTED-EVENTS-MISMATCH",
                                    "expected": expected_events,
                                    "actual": selected_events,
                                }
                            )
                return {
                    "ok": not mismatches,
                    "mismatches": mismatches,
                    "checked": {
                        "current_ledger_version": version,
                        "processed_event_count": processed_count,
                        "audit_event_count": int(
                            connection.execute(
                                "SELECT COUNT(*) AS count FROM audit_events"
                            ).fetchone()["count"]
                        ),
                        "archive_event_count": int(
                            connection.execute(
                                "SELECT COUNT(*) AS count FROM audit_archive"
                            ).fetchone()["count"]
                        ),
                        "event_watermark": event_watermark,
                        "logical_clock_watermark": logical_clock,
                        "quota_version": quota.quota_version,
                        "policy_version": policy.policy_version,
                    },
                    "ledger": ledger_rows,
                    "events": events_summary,
                    "audit_digest": audit_digest,
                }
