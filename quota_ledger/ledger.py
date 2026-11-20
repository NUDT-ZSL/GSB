"""Persistent SQLite-backed public ledger API."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterator, Sequence

from .engine import Adjudication, adjudicate
from .errors import (
    E_BAD_EVENT,
    E_CONFIG_CONFLICT,
    E_DATABASE,
    E_INJECTED_FAILURE,
    E_NOT_LOADED,
    E_RECOVERY_MISMATCH,
    InjectedFailure,
    LedgerError,
)
from .locking import exclusive_file_lock
from .models import (
    Event,
    LimiterConfig,
    ObjectRecord,
    QuotaConfig,
    State,
    TokenState,
    Usage,
)
from .schema import SCHEMA_STATEMENTS, SCHEMA_VERSION
from .utils import LEVEL_RANK, canonical_json, layer_sort_key, tokens_text
from .validation import (
    parse_event,
    parse_limiter_document,
    parse_quota_document,
    validate_event_order,
)


def _as_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _connect(db_path: str | Path, write: bool) -> sqlite3.Connection:
    path = Path(db_path)
    if not write and not path.exists():
        raise LedgerError(E_NOT_LOADED, "ledger database does not exist")
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(path),
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    if write:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA wal_autocheckpoint=1000")
    else:
        connection.execute("PRAGMA query_only=ON")
    return connection


@contextmanager
def _transaction(connection: sqlite3.Connection, immediate: bool = True) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    else:
        try:
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def _config_from_connection(
    connection: sqlite3.Connection,
) -> tuple[QuotaConfig, LimiterConfig]:
    rows = connection.execute(
        "SELECT key, value FROM meta WHERE key IN ('quota_config_json', 'limiter_config_json')"
    ).fetchall()
    values = {row["key"]: row["value"] for row in rows}
    if "quota_config_json" not in values or "limiter_config_json" not in values:
        raise LedgerError(E_NOT_LOADED, "quota ledger configuration is not loaded")
    try:
        quota_document = json.loads(values["quota_config_json"])
        limiter_document = json.loads(values["limiter_config_json"])
    except json.JSONDecodeError as exc:
        raise LedgerError(E_DATABASE, "stored configuration is not valid JSON") from exc
    quota = parse_quota_document(quota_document)
    limiter = parse_limiter_document(limiter_document, quota)
    return quota, limiter


def _state_from_connection(
    connection: sqlite3.Connection,
    quota: QuotaConfig,
    limiter: LimiterConfig,
) -> State:
    state = State.initial(quota, limiter)
    state.objects.clear()
    state.idempotency.clear()
    state.decision_results.clear()

    usage_rows = connection.execute(
        """
        SELECT path, used_bytes, used_objects, soft_active,
               soft_entry_seq, soft_entry_clock_ms,
               soft_entry_used_bytes, soft_entry_used_objects,
               soft_entry_soft_bytes, soft_entry_soft_objects,
               soft_exit_seq, soft_exit_clock_ms,
               soft_exit_used_bytes, soft_exit_used_objects,
               soft_exit_soft_bytes, soft_exit_soft_objects
        FROM usage
        """
    ).fetchall()
    seen_usage_paths: set[str] = set()
    for row in usage_rows:
        seen_usage_paths.add(row["path"])
        state.usage[row["path"]] = Usage(
            used_bytes=row["used_bytes"],
            used_objects=row["used_objects"],
            soft_active=bool(row["soft_active"]),
            soft_entry_seq=_as_int(row["soft_entry_seq"]),
            soft_entry_clock_ms=_as_int(row["soft_entry_clock_ms"]),
            soft_entry_used_bytes=_as_int(row["soft_entry_used_bytes"]),
            soft_entry_used_objects=_as_int(row["soft_entry_used_objects"]),
            soft_entry_soft_bytes=_as_int(row["soft_entry_soft_bytes"]),
            soft_entry_soft_objects=_as_int(row["soft_entry_soft_objects"]),
            soft_exit_seq=_as_int(row["soft_exit_seq"]),
            soft_exit_clock_ms=_as_int(row["soft_exit_clock_ms"]),
            soft_exit_used_bytes=_as_int(row["soft_exit_used_bytes"]),
            soft_exit_used_objects=_as_int(row["soft_exit_used_objects"]),
            soft_exit_soft_bytes=_as_int(row["soft_exit_soft_bytes"]),
            soft_exit_soft_objects=_as_int(row["soft_exit_soft_objects"]),
        )
    if seen_usage_paths != set(quota.by_path):
        raise LedgerError(
            E_DATABASE,
            "usage rows do not match declared quota layers",
            {
                "missing": sorted(set(quota.by_path) - seen_usage_paths),
                "unknown": sorted(seen_usage_paths - set(quota.by_path)),
            },
        )

    object_rows = connection.execute(
        """
        SELECT tenant, bucket, prefix, object_key, size_bytes, created_seq, updated_seq
        FROM objects
        """
    ).fetchall()
    for row in object_rows:
        record = ObjectRecord(
            tenant=row["tenant"],
            bucket=row["bucket"],
            prefix=row["prefix"],
            object_key=row["object_key"],
            size_bytes=row["size_bytes"],
            created_seq=row["created_seq"],
            updated_seq=row["updated_seq"],
        )
        state.objects[record.identity] = record

    token_rows = connection.execute(
        """
        SELECT tenant, tokens_micro, last_refill_ms, last_seen_ms,
               refill_remainder_num, capacity_micro
        FROM tokens
        """
    ).fetchall()
    seen_token_tenants: set[str] = set()
    for row in token_rows:
        seen_token_tenants.add(row["tenant"])
        state.tokens[row["tenant"]] = TokenState(
            tenant=row["tenant"],
            tokens_micro=row["tokens_micro"],
            last_refill_ms=row["last_refill_ms"],
            last_seen_ms=row["last_seen_ms"],
            refill_remainder_num=row["refill_remainder_num"],
            capacity_micro=row["capacity_micro"],
        )
    if seen_token_tenants != set(limiter.tenants):
        raise LedgerError(
            E_DATABASE,
            "token rows do not match declared tenants",
            {
                "missing": sorted(set(limiter.tenants) - seen_token_tenants),
                "unknown": sorted(seen_token_tenants - set(limiter.tenants)),
            },
        )

    idempotency_rows = connection.execute(
        "SELECT idempotency_key, fingerprint, result_json FROM idempotency"
    ).fetchall()
    for row in idempotency_rows:
        state.idempotency[row["idempotency_key"]] = (
            row["fingerprint"],
            json.loads(row["result_json"]),
        )

    meta_row = connection.execute(
        "SELECT value FROM meta WHERE key = 'last_decided_seq'"
    ).fetchone()
    state.last_decided_seq = int(meta_row["value"]) if meta_row else 0
    return state


def _persist_decision(
    connection: sqlite3.Connection,
    state: State,
    event: Event,
    result: dict[str, Any],
) -> None:
    # Paths are selected from persisted quota rows, not reconstructed from raw names.
    rows = connection.execute(
        "SELECT path, level, tenant, bucket, prefix FROM quota_layers"
    ).fetchall()
    selected = []
    for row in rows:
        if (
            row["level"] == "tenant"
            and row["tenant"] == event.tenant
        ) or (
            row["level"] == "bucket"
            and row["tenant"] == event.tenant
            and row["bucket"] == event.bucket
        ) or (
            row["level"] == "prefix"
            and row["tenant"] == event.tenant
            and row["bucket"] == event.bucket
            and row["prefix"] == event.prefix
        ):
            selected.append(row["path"])

    for path in selected:
        usage = state.usage[path]
        connection.execute(
            """
            UPDATE usage
            SET used_bytes = ?, used_objects = ?, soft_active = ?,
                soft_entry_seq = ?, soft_entry_clock_ms = ?,
                soft_entry_used_bytes = ?, soft_entry_used_objects = ?,
                soft_entry_soft_bytes = ?, soft_entry_soft_objects = ?,
                soft_exit_seq = ?, soft_exit_clock_ms = ?,
                soft_exit_used_bytes = ?, soft_exit_used_objects = ?,
                soft_exit_soft_bytes = ?, soft_exit_soft_objects = ?
            WHERE path = ?
            """,
            (
                usage.used_bytes,
                usage.used_objects,
                int(usage.soft_active),
                usage.soft_entry_seq,
                usage.soft_entry_clock_ms,
                usage.soft_entry_used_bytes,
                usage.soft_entry_used_objects,
                usage.soft_entry_soft_bytes,
                usage.soft_entry_soft_objects,
                usage.soft_exit_seq,
                usage.soft_exit_clock_ms,
                usage.soft_exit_used_bytes,
                usage.soft_exit_used_objects,
                usage.soft_exit_soft_bytes,
                usage.soft_exit_soft_objects,
                path,
            ),
        )

    token = state.tokens[event.tenant]
    connection.execute(
        """
        UPDATE tokens
        SET tokens_micro = ?, last_refill_ms = ?, last_seen_ms = ?,
            refill_remainder_num = ?, capacity_micro = ?
        WHERE tenant = ?
        """,
        (
            token.tokens_micro,
            token.last_refill_ms,
            token.last_seen_ms,
            token.refill_remainder_num,
            token.capacity_micro,
            event.tenant,
        ),
    )

    applied = result.get("applied")
    if applied:
        identity = (
            event.tenant,
            event.bucket,
            event.prefix,
            event.object_key,
        )
        if applied["object_action"] == "DELETE":
            connection.execute(
                """
                DELETE FROM objects
                WHERE tenant = ? AND bucket = ? AND prefix = ? AND object_key = ?
                """,
                identity,
            )
        else:
            record = state.objects[identity]
            connection.execute(
                """
                INSERT INTO objects(
                    tenant, bucket, prefix, object_key,
                    size_bytes, created_seq, updated_seq
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant, bucket, prefix, object_key)
                DO UPDATE SET
                    size_bytes = excluded.size_bytes,
                    updated_seq = excluded.updated_seq
                """,
                (
                    record.tenant,
                    record.bucket,
                    record.prefix,
                    record.object_key,
                    record.size_bytes,
                    record.created_seq,
                    record.updated_seq,
                ),
            )

    for transition in result["soft_transitions"]:
        connection.execute(
            """
            INSERT INTO soft_events(
                transition, level, path, event_seq, clock_ms,
                used_bytes, used_objects, soft_bytes_limit, soft_objects_limit
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                transition["transition"],
                transition["level"],
                transition["path"],
                transition["event_seq"],
                transition["clock_ms"],
                transition["used_bytes"],
                transition["used_objects"],
                transition["soft_bytes_limit"],
                transition["soft_objects_limit"],
            ),
        )

    result_json = canonical_json(result)
    payload_json = canonical_json(event.payload())
    connection.execute(
        """
        INSERT INTO events(
            seq, idempotency_key, fingerprint, payload_json,
            result_json, decision, code, accepted, clock_ms
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event.seq,
            event.idempotency_key,
            event.fingerprint,
            payload_json,
            result_json,
            result["decision"],
            result["code"],
            int(result["ok"]),
            event.clock_ms,
        ),
    )
    connection.execute(
        """
        INSERT INTO idempotency(idempotency_key, fingerprint, seq, result_json)
        VALUES (?, ?, ?, ?)
        """,
        (event.idempotency_key, event.fingerprint, event.seq, result_json),
    )
    connection.execute(
        "UPDATE meta SET value = ? WHERE key = 'last_decided_seq'",
        (str(state.last_decided_seq),),
    )


def load_configuration(
    db_path: str | Path,
    quota_document: Any,
    limiter_document: Any,
) -> dict[str, Any]:
    """Validate both documents, then atomically initialize an empty ledger."""

    quota = parse_quota_document(quota_document)
    limiter = parse_limiter_document(limiter_document, quota)
    quota_json = canonical_json(quota.document)
    limiter_json = canonical_json(limiter.document)
    fingerprint = sha256(
        (quota_json + "\n" + limiter_json).encode("utf-8")
    ).hexdigest()

    path = Path(db_path)
    lock_path = Path(str(path) + ".lock")
    with exclusive_file_lock(lock_path):
        connection = _connect(path, write=True)
        try:
            with _transaction(connection):
                for statement in SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                existing_rows = connection.execute(
                    """
                    SELECT key, value FROM meta
                    WHERE key IN ('quota_config_json', 'limiter_config_json')
                    """
                ).fetchall()
                existing = {row["key"]: row["value"] for row in existing_rows}
                if existing:
                    if (
                        existing.get("quota_config_json") != quota_json
                        or existing.get("limiter_config_json") != limiter_json
                    ):
                        raise LedgerError(
                            E_CONFIG_CONFLICT,
                            "database already contains a different configuration",
                        )
                    existing_state = connection.execute(
                        "SELECT COUNT(*) AS count FROM events"
                    ).fetchone()["count"]
                    return {
                        "ok": True,
                        "created": False,
                        "config_sha256": fingerprint,
                        "tenant_count": len(quota.tenants),
                        "layer_count": len(quota.layers),
                        "event_count": existing_state,
                    }

                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('quota_config_json', ?)",
                    (quota_json,),
                )
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('limiter_config_json', ?)",
                    (limiter_json,),
                )
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('config_sha256', ?)",
                    (fingerprint,),
                )
                connection.execute(
                    "INSERT INTO meta(key, value) VALUES ('last_decided_seq', '0')"
                )
                for layer in quota.layers:
                    connection.execute(
                        """
                        INSERT INTO quota_layers(
                            path, level, tenant, bucket, prefix,
                            max_bytes, max_objects, soft_pct, hard_pct,
                            soft_bytes, soft_objects, hard_bytes, hard_objects
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            layer.path,
                            layer.level,
                            layer.tenant,
                            layer.bucket,
                            layer.prefix,
                            layer.max_bytes,
                            layer.max_objects,
                            layer.soft_pct,
                            layer.hard_pct,
                            layer.soft_bytes,
                            layer.soft_objects,
                            layer.hard_bytes,
                            layer.hard_objects,
                        ),
                    )
                    connection.execute(
                        "INSERT INTO usage(path) VALUES (?)",
                        (layer.path,),
                    )
                for tenant in quota.tenants:
                    policy = limiter.tenant(tenant)
                    connection.execute(
                        """
                        INSERT INTO tokens(
                            tenant, tokens_micro, last_refill_ms, last_seen_ms,
                            refill_remainder_num, capacity_micro
                        ) VALUES (?, ?, 0, 0, 0, ?)
                        """,
                        (tenant, policy.capacity_micro, policy.capacity_micro),
                    )
                connection.execute(
                    "PRAGMA user_version = " + str(SCHEMA_VERSION)
                )
            return {
                "ok": True,
                "created": True,
                "config_sha256": fingerprint,
                "tenant_count": len(quota.tenants),
                "layer_count": len(quota.layers),
                "event_count": 0,
            }
        finally:
            connection.close()


class QuotaLedger:
    """Public library interface for deterministic quota adjudication."""

    def __init__(
        self,
        db_path: str | Path,
        quota: QuotaConfig,
        limiter: LimiterConfig,
    ) -> None:
        self.db_path = str(db_path)
        self.quota = quota
        self.limiter = limiter

    @classmethod
    def open(cls, db_path: str | Path) -> "QuotaLedger":
        path = Path(db_path)
        if not path.exists():
            raise LedgerError(E_NOT_LOADED, "ledger database does not exist")
        connection = _connect(path, write=False)
        try:
            quota, limiter = _config_from_connection(connection)
        finally:
            connection.close()
        return cls(path, quota, limiter)

    @classmethod
    def load(
        cls,
        db_path: str | Path,
        quota_document: Any,
        limiter_document: Any,
    ) -> "QuotaLedger":
        load_configuration(db_path, quota_document, limiter_document)
        return cls.open(db_path)

    @property
    def lock_path(self) -> str:
        return self.db_path + ".lock"

    def _coerce_event(self, event: Event | dict[str, Any]) -> Event:
        if isinstance(event, Event):
            return parse_event(event.payload(), self.quota)
        return parse_event(event, self.quota)

    def write(
        self,
        event: Event | dict[str, Any],
        *,
        fault_stage: str | None = None,
        crash_stage: str | None = None,
    ) -> dict[str, Any]:
        parsed = self._coerce_event(event)
        if parsed.deleted:
            return self._adjudicate(
                parsed,
                fault_stage=fault_stage,
                crash_stage=crash_stage,
            )
        return self._adjudicate(
            parsed,
            fault_stage=fault_stage,
            crash_stage=crash_stage,
        )

    def delete(
        self,
        event: Event | dict[str, Any],
        *,
        fault_stage: str | None = None,
        crash_stage: str | None = None,
    ) -> dict[str, Any]:
        parsed = self._coerce_event(event)
        if not parsed.deleted:
            raise LedgerError(
                E_BAD_EVENT,
                "delete subcommand requires deleted=true",
                {"seq": parsed.seq},
            )
        return self._adjudicate(
            parsed,
            fault_stage=fault_stage,
            crash_stage=crash_stage,
        )

    def write_batch(
        self,
        events: Sequence[Event | dict[str, Any]],
        *,
        fault_stage: str | None = None,
    ) -> list[dict[str, Any]]:
        parsed_events = [self._coerce_event(event) for event in events]
        validate_event_order(parsed_events)
        results = []
        for event in parsed_events:
            results.append(
                self.write(event, fault_stage=fault_stage)
            )
        return results

    def _adjudicate(
        self,
        event: Event,
        *,
        fault_stage: str | None,
        crash_stage: str | None,
    ) -> dict[str, Any]:
        if crash_stage == "after_intent":
            with exclusive_file_lock(self.lock_path):
                connection = _connect(self.db_path, write=True)
                try:
                    with _transaction(connection):
                        connection.execute(
                            """
                            INSERT OR REPLACE INTO pending_operations(
                                op_key, seq, idempotency_key,
                                fingerprint, payload_json, stage
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                event.idempotency_key,
                                event.seq,
                                event.idempotency_key,
                                event.fingerprint,
                                canonical_json(event.payload()),
                                "AFTER_INTENT",
                            ),
                        )
                    os._exit(74)
                finally:
                    connection.close()

        with exclusive_file_lock(self.lock_path):
            connection = _connect(self.db_path, write=True)
            try:
                with _transaction(connection):
                    quota, limiter = _config_from_connection(connection)
                    state = _state_from_connection(connection, quota, limiter)
                    outcome: Adjudication = adjudicate(
                        state,
                        event,
                        quota,
                        limiter,
                    )
                    if outcome.replayed:
                        return outcome.result

                    _persist_decision(
                        connection,
                        state,
                        event,
                        outcome.result,
                    )
                    if fault_stage in {
                        "after_rate",
                        "after_quota",
                        "after_token",
                        "after_ledger",
                        "before_commit",
                    }:
                        raise InjectedFailure(fault_stage)
                    if fault_stage == "after_object":
                        raise InjectedFailure(fault_stage)
                    if crash_stage == "before_commit":
                        os._exit(73)
                    return outcome.result
            except InjectedFailure as exc:
                raise LedgerError(
                    E_INJECTED_FAILURE,
                    "injected failure rolled back the entire write transaction",
                    {"stage": exc.stage},
                ) from exc
            except sqlite3.Error as exc:
                raise LedgerError(
                    E_DATABASE,
                    "SQLite transaction failed and was rolled back",
                    {"error": str(exc)},
                ) from exc
            finally:
                connection.close()

    def status(self) -> dict[str, Any]:
        connection = _connect(self.db_path, write=False)
        try:
            connection.execute("BEGIN")
            quota, limiter = _config_from_connection(connection)
            usage_rows = connection.execute(
                "SELECT * FROM usage"
            ).fetchall()
            usage_by_path = {row["path"]: row for row in usage_rows}
            layers = []
            for layer in sorted(quota.layers, key=layer_sort_key):
                row = usage_by_path[layer.path]
                used_bytes = row["used_bytes"]
                used_objects = row["used_objects"]
                if (
                    used_bytes >= layer.hard_bytes
                    or used_objects >= layer.hard_objects
                ):
                    quota_state = "HARD"
                elif (
                    row["soft_active"]
                    or used_bytes > layer.soft_bytes
                    or used_objects > layer.soft_objects
                ):
                    quota_state = "SOFT"
                else:
                    quota_state = "NORMAL"
                layers.append(
                    {
                        "level": layer.level,
                        "tenant": layer.tenant,
                        "bucket": layer.bucket or None,
                        "prefix": layer.prefix or None,
                        "path": layer.path,
                        "used_bytes": used_bytes,
                        "used_objects": used_objects,
                        "limits": {
                            "bytes": layer.max_bytes,
                            "objects": layer.max_objects,
                            "soft_pct": layer.soft_pct,
                            "hard_pct": layer.hard_pct,
                        },
                        "thresholds": {
                            "soft_bytes": layer.soft_bytes,
                            "soft_objects": layer.soft_objects,
                            "hard_bytes": layer.hard_bytes,
                            "hard_objects": layer.hard_objects,
                        },
                        "quota_state": quota_state,
                        "soft_active": bool(row["soft_active"]),
                        "soft_entry": (
                            {
                                "seq": row["soft_entry_seq"],
                                "clock_ms": row["soft_entry_clock_ms"],
                                "used_bytes": row["soft_entry_used_bytes"],
                                "used_objects": row["soft_entry_used_objects"],
                                "soft_bytes_limit": row["soft_entry_soft_bytes"],
                                "soft_objects_limit": row["soft_entry_soft_objects"],
                            }
                            if row["soft_entry_seq"] is not None
                            else None
                        ),
                        "soft_exit": (
                            {
                                "seq": row["soft_exit_seq"],
                                "clock_ms": row["soft_exit_clock_ms"],
                                "used_bytes": row["soft_exit_used_bytes"],
                                "used_objects": row["soft_exit_used_objects"],
                                "soft_bytes_limit": row["soft_exit_soft_bytes"],
                                "soft_objects_limit": row["soft_exit_soft_objects"],
                            }
                            if row["soft_exit_seq"] is not None
                            else None
                        ),
                    }
                )

            tokens = []
            token_rows = connection.execute(
                "SELECT * FROM tokens ORDER BY tenant"
            ).fetchall()
            for row in token_rows:
                policy = limiter.tenant(row["tenant"])
                tokens.append(
                    {
                        "tenant": row["tenant"],
                        "tokens_micro": row["tokens_micro"],
                        "tokens": tokens_text(row["tokens_micro"]),
                        "capacity_micro": row["capacity_micro"],
                        "capacity": tokens_text(row["capacity_micro"]),
                        "last_refill_ms": row["last_refill_ms"],
                        "last_seen_ms": row["last_seen_ms"],
                        "refill_remainder_num": row["refill_remainder_num"],
                        "policy": policy.policy_dict(),
                    }
                )

            meta = {
                row["key"]: row["value"]
                for row in connection.execute(
                    "SELECT key, value FROM meta"
                ).fetchall()
            }
            counts = {
                "events": connection.execute(
                    "SELECT COUNT(*) AS count FROM events"
                ).fetchone()["count"],
                "accepted": connection.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE accepted = 1"
                ).fetchone()["count"],
                "rejected": connection.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE accepted = 0"
                ).fetchone()["count"],
                "objects": connection.execute(
                    "SELECT COUNT(*) AS count FROM objects"
                ).fetchone()["count"],
                "soft_alerts": connection.execute(
                    "SELECT COUNT(*) AS count FROM soft_events"
                ).fetchone()["count"],
                "pending_operations": connection.execute(
                    "SELECT COUNT(*) AS count FROM pending_operations"
                ).fetchone()["count"],
            }
            return {
                "ok": True,
                "schema_version": SCHEMA_VERSION,
                "config_sha256": meta.get("config_sha256"),
                "last_decided_seq": int(meta.get("last_decided_seq", "0")),
                "layers": layers,
                "tokens": tokens,
                "counts": counts,
            }
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()

    def resume(self) -> dict[str, Any]:
        cleaned: list[dict[str, Any]] = []
        integrity = "unknown"
        with exclusive_file_lock(self.lock_path):
            connection = _connect(self.db_path, write=True)
            try:
                with _transaction(connection):
                    _config_from_connection(connection)
                    pending_rows = connection.execute(
                        "SELECT * FROM pending_operations ORDER BY op_key"
                    ).fetchall()
                    for row in pending_rows:
                        cleaned.append(
                            {
                                "op_key": row["op_key"],
                                "seq": row["seq"],
                                "idempotency_key": row["idempotency_key"],
                                "stage": row["stage"],
                                "occupancy_released": {
                                    "bytes": 0,
                                    "objects": 0,
                                    "tokens_micro": 0,
                                },
                            }
                        )
                    connection.execute("DELETE FROM pending_operations")
                    integrity = connection.execute(
                        "PRAGMA integrity_check"
                    ).fetchone()[0]
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                connection.close()

        verification = self.verify()
        ok = bool(verification["ok"]) and integrity == "ok"
        result = {
            "ok": ok,
            "code": "OK" if ok else E_RECOVERY_MISMATCH,
            "integrity_check": integrity,
            "cleaned_operations": cleaned,
            "cleaned_count": len(cleaned),
            "transaction_model": "single-phase commit; SQLite removes uncommitted journal frames",
            "verify": {
                "ok": verification["ok"],
                "event_count": verification["event_count"],
                "mismatch_count": verification["mismatch_count"],
            },
        }
        return result

    def verify(self, source_events: Sequence[Event | dict[str, Any]] | None = None) -> dict[str, Any]:
        connection = _connect(self.db_path, write=False)
        try:
            connection.execute("BEGIN")
            quota, limiter = _config_from_connection(connection)
            stored_state = _state_from_connection(connection, quota, limiter)
            event_rows = connection.execute(
                """
                SELECT seq, idempotency_key, fingerprint, payload_json,
                       result_json, decision, code, accepted, clock_ms
                FROM events ORDER BY seq
                """
            ).fetchall()
            idempotency_rows = connection.execute(
                """
                SELECT idempotency_key, fingerprint, seq, result_json
                FROM idempotency ORDER BY idempotency_key
                """
            ).fetchall()
            soft_rows = connection.execute(
                """
                SELECT transition, level, path, event_seq, clock_ms,
                       used_bytes, used_objects,
                       soft_bytes_limit, soft_objects_limit
                FROM soft_events
                """
            ).fetchall()
            quota_rows = connection.execute(
                """
                SELECT path, level, tenant, bucket, prefix,
                       max_bytes, max_objects, soft_pct, hard_pct,
                       soft_bytes, soft_objects, hard_bytes, hard_objects
                FROM quota_layers ORDER BY path
                """
            ).fetchall()
            usage_paths = {
                row["path"]
                for row in connection.execute("SELECT path FROM usage")
            }
            token_tenants = {
                row["tenant"]
                for row in connection.execute("SELECT tenant FROM tokens")
            }
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()

        mismatches: list[dict[str, Any]] = []
        replayed_state = State.initial(quota, limiter)
        replayed_soft: list[dict[str, Any]] = []
        stored_by_seq = {row["seq"]: row for row in event_rows}
        persisted_soft = [
            {
                "transition": row["transition"],
                "level": row["level"],
                "path": row["path"],
                "event_seq": row["event_seq"],
                "clock_ms": row["clock_ms"],
                "used_bytes": row["used_bytes"],
                "used_objects": row["used_objects"],
                "soft_bytes_limit": row["soft_bytes_limit"],
                "soft_objects_limit": row["soft_objects_limit"],
            }
            for row in soft_rows
        ]
        persisted_soft.sort(
            key=lambda item: (
                item["event_seq"],
                LEVEL_RANK[item["level"]],
                item["path"],
                item["transition"],
            )
        )

        expected_quota = {
            layer.path: {
                "path": layer.path,
                "level": layer.level,
                "tenant": layer.tenant,
                "bucket": layer.bucket,
                "prefix": layer.prefix,
                "max_bytes": layer.max_bytes,
                "max_objects": layer.max_objects,
                "soft_pct": layer.soft_pct,
                "hard_pct": layer.hard_pct,
                "soft_bytes": layer.soft_bytes,
                "soft_objects": layer.soft_objects,
                "hard_bytes": layer.hard_bytes,
                "hard_objects": layer.hard_objects,
            }
            for layer in quota.layers
        }
        persisted_quota = {
            row["path"]: dict(row)
            for row in quota_rows
        }
        if canonical_json(persisted_quota) != canonical_json(expected_quota):
            mismatches.append(
                {
                    "field": "quota_layers",
                    "persisted": persisted_quota,
                    "recomputed": expected_quota,
                }
            )
        if usage_paths != set(quota.by_path):
            mismatches.append(
                {
                    "field": "usage.paths",
                    "persisted": sorted(usage_paths),
                    "recomputed": sorted(quota.by_path),
                }
            )
        if token_tenants != set(quota.tenants):
            mismatches.append(
                {
                    "field": "tokens.tenants",
                    "persisted": sorted(token_tenants),
                    "recomputed": sorted(quota.tenants),
                }
            )

        for row in event_rows:
            try:
                event = parse_event(json.loads(row["payload_json"]), quota)
                outcome = adjudicate(
                    replayed_state,
                    event,
                    quota,
                    limiter,
                )
            except LedgerError as exc:
                mismatches.append(
                    {
                        "field": f"events[{row['seq']}]",
                        "persisted": "DECISION",
                        "recomputed": exc.code,
                        "details": exc.details,
                    }
                )
                continue
            if outcome.replayed:
                mismatches.append(
                    {
                        "field": f"events[{row['seq']}].idempotency",
                        "persisted": "UNIQUE_EVENT",
                        "recomputed": "IDEMPOTENT_REPLAY",
                    }
                )
            persisted_result = json.loads(row["result_json"])
            if canonical_json(persisted_result) != canonical_json(outcome.result):
                mismatches.append(
                    {
                        "field": f"events[{row['seq']}].decision",
                        "persisted": persisted_result.get("code"),
                        "recomputed": outcome.result.get("code"),
                    }
                )
            if row["fingerprint"] != event.fingerprint:
                mismatches.append(
                    {
                        "field": f"events[{row['seq']}].fingerprint",
                        "persisted": row["fingerprint"],
                        "recomputed": event.fingerprint,
                    }
                )
            if canonical_json(json.loads(row["payload_json"])) != canonical_json(
                event.payload()
            ):
                mismatches.append(
                    {
                        "field": f"events[{row['seq']}].payload",
                        "persisted": json.loads(row["payload_json"]),
                        "recomputed": event.payload(),
                    }
                )
            metadata = {
                "idempotency_key": event.idempotency_key,
                "decision": outcome.result["decision"],
                "code": outcome.result["code"],
                "accepted": int(outcome.result["ok"]),
                "clock_ms": event.clock_ms,
            }
            persisted_metadata = {
                "idempotency_key": row["idempotency_key"],
                "decision": row["decision"],
                "code": row["code"],
                "accepted": row["accepted"],
                "clock_ms": row["clock_ms"],
            }
            if persisted_metadata != metadata:
                mismatches.append(
                    {
                        "field": f"events[{row['seq']}].metadata",
                        "persisted": persisted_metadata,
                        "recomputed": metadata,
                    }
                )
            replayed_soft.extend(outcome.result["soft_transitions"])
        replayed_soft.sort(
            key=lambda item: (
                item["event_seq"],
                LEVEL_RANK[item["level"]],
                item["path"],
                item["transition"],
            )
        )

        if replayed_state.last_decided_seq != stored_state.last_decided_seq:
            mismatches.append(
                {
                    "field": "last_decided_seq",
                    "persisted": stored_state.last_decided_seq,
                    "recomputed": replayed_state.last_decided_seq,
                }
            )
        if len(replayed_state.decision_results) != len(event_rows):
            mismatches.append(
                {
                    "field": "decision_count",
                    "persisted": len(event_rows),
                    "recomputed": len(replayed_state.decision_results),
                }
            )

        for layer in quota.layers:
            persisted_usage = stored_state.usage[layer.path]
            recomputed_usage = replayed_state.usage[layer.path]
            for field in (
                "used_bytes",
                "used_objects",
                "soft_active",
                "soft_entry_seq",
                "soft_entry_clock_ms",
                "soft_entry_used_bytes",
                "soft_entry_used_objects",
                "soft_entry_soft_bytes",
                "soft_entry_soft_objects",
                "soft_exit_seq",
                "soft_exit_clock_ms",
                "soft_exit_used_bytes",
                "soft_exit_used_objects",
                "soft_exit_soft_bytes",
                "soft_exit_soft_objects",
            ):
                persisted_value = getattr(persisted_usage, field)
                recomputed_value = getattr(recomputed_usage, field)
                if persisted_value != recomputed_value:
                    mismatches.append(
                        {
                            "field": f"usage[{layer.path}].{field}",
                            "persisted": persisted_value,
                            "recomputed": recomputed_value,
                        }
                    )

        all_object_keys = sorted(
            set(stored_state.objects) | set(replayed_state.objects),
            key=lambda item: item,
        )
        for identity in all_object_keys:
            persisted_object = stored_state.objects.get(identity)
            recomputed_object = replayed_state.objects.get(identity)
            persisted_value = (
                None
                if persisted_object is None
                else {
                    "size_bytes": persisted_object.size_bytes,
                    "created_seq": persisted_object.created_seq,
                    "updated_seq": persisted_object.updated_seq,
                }
            )
            recomputed_value = (
                None
                if recomputed_object is None
                else {
                    "size_bytes": recomputed_object.size_bytes,
                    "created_seq": recomputed_object.created_seq,
                    "updated_seq": recomputed_object.updated_seq,
                }
            )
            if persisted_value != recomputed_value:
                mismatches.append(
                    {
                        "field": f"objects[{':'.join(identity)}]",
                        "persisted": persisted_value,
                        "recomputed": recomputed_value,
                    }
                )

        for tenant in quota.tenants:
            persisted_token = stored_state.tokens[tenant]
            recomputed_token = replayed_state.tokens[tenant]
            for field in (
                "tokens_micro",
                "last_refill_ms",
                "last_seen_ms",
                "refill_remainder_num",
                "capacity_micro",
            ):
                persisted_value = getattr(persisted_token, field)
                recomputed_value = getattr(recomputed_token, field)
                if persisted_value != recomputed_value:
                    mismatches.append(
                        {
                            "field": f"tokens[{tenant}].{field}",
                            "persisted": persisted_value,
                            "recomputed": recomputed_value,
                        }
                    )
            if persisted_token.tokens_micro < 0:
                mismatches.append(
                    {
                        "field": f"tokens[{tenant}].tokens_micro",
                        "persisted": persisted_token.tokens_micro,
                        "recomputed": "NON_NEGATIVE",
                    }
                )

        if canonical_json(persisted_soft) != canonical_json(replayed_soft):
            mismatches.append(
                {
                    "field": "soft_events",
                    "persisted": persisted_soft,
                    "recomputed": replayed_soft,
                }
            )

        if set(stored_state.idempotency) != set(replayed_state.idempotency):
            mismatches.append(
                {
                    "field": "idempotency.keys",
                    "persisted": sorted(stored_state.idempotency),
                    "recomputed": sorted(replayed_state.idempotency),
                }
            )
        for idempotency_row in idempotency_rows:
            key = idempotency_row["idempotency_key"]
            replayed_record = replayed_state.idempotency.get(key)
            if replayed_record is None:
                mismatches.append(
                    {
                        "field": f"idempotency[{key}]",
                        "persisted": "PRESENT",
                        "recomputed": None,
                    }
                )
                continue
            replayed_fingerprint, replayed_result = replayed_record
            if idempotency_row["fingerprint"] != replayed_fingerprint:
                mismatches.append(
                    {
                        "field": f"idempotency[{key}].fingerprint",
                        "persisted": idempotency_row["fingerprint"],
                        "recomputed": replayed_fingerprint,
                    }
                )
            if canonical_json(
                json.loads(idempotency_row["result_json"])
            ) != canonical_json(replayed_result):
                mismatches.append(
                    {
                        "field": f"idempotency[{key}].result",
                        "persisted": json.loads(idempotency_row["result_json"]),
                        "recomputed": replayed_result,
                    }
                )
            stored_event = stored_by_seq.get(idempotency_row["seq"])
            if stored_event is None or (
                stored_event["idempotency_key"] != key
            ):
                mismatches.append(
                    {
                        "field": f"idempotency[{key}].seq",
                        "persisted": idempotency_row["seq"],
                        "recomputed": "MISSING_OR_MISMATCHED_EVENT",
                    }
                )

        if source_events is not None:
            expected_events = [
                parse_event(event.payload() if isinstance(event, Event) else event, quota)
                for event in source_events
            ]
            validate_event_order(expected_events)
            for event in expected_events:
                stored_row = stored_by_seq.get(event.seq)
                if stored_row is None:
                    mismatches.append(
                        {
                            "field": f"source_events[{event.seq}]",
                            "persisted": None,
                            "recomputed": event.payload(),
                        }
                    )
                elif canonical_json(json.loads(stored_row["payload_json"])) != canonical_json(
                    event.payload()
                ):
                    mismatches.append(
                        {
                            "field": f"source_events[{event.seq}].payload",
                            "persisted": json.loads(stored_row["payload_json"]),
                            "recomputed": event.payload(),
                        }
                    )

        violations = _conservation_violations(quota, stored_state)
        mismatches.extend(violations)
        mismatches.sort(
            key=lambda item: (
                item.get("field", ""),
                canonical_json(item.get("persisted")),
                canonical_json(item.get("recomputed")),
            )
        )
        return {
            "ok": not mismatches,
            "event_count": len(event_rows),
            "last_decided_seq": stored_state.last_decided_seq,
            "mismatch_count": len(mismatches),
            "mismatches": mismatches,
            "checks": {
                "usage_matches_replay": all(
                    not item["field"].startswith("usage[") for item in mismatches
                ),
                "objects_match_replay": all(
                    not item["field"].startswith("objects[") for item in mismatches
                ),
                "tokens_match_replay": all(
                    not item["field"].startswith("tokens[") for item in mismatches
                ),
                "soft_events_match_replay": all(
                    not item["field"].startswith("soft_events")
                    for item in mismatches
                ),
                "idempotency_matches_replay": all(
                    not item["field"].startswith("idempotency")
                    for item in mismatches
                ),
                "event_metadata_matches_replay": all(
                    not item["field"].startswith("events[")
                    for item in mismatches
                ),
                "configuration_matches_declarations": all(
                    item["field"] != "quota_layers" for item in mismatches
                ),
                "usage_and_token_indexes_match": all(
                    item["field"] not in {"usage.paths", "tokens.tenants"}
                    for item in mismatches
                ),
                "conservation_holds": not violations,
                "no_negative_tokens": all(
                    token.tokens_micro >= 0 for token in stored_state.tokens.values()
                ),
            },
        }


def _conservation_violations(
    quota: QuotaConfig,
    state: State,
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for tenant in quota.tenants:
        buckets = [
            layer
            for layer in quota.layers
            if layer.level == "bucket" and layer.tenant == tenant
        ]
        tenant_usage = state.usage[quota.layer("tenant", tenant).path]
        bucket_bytes = sum(state.usage[layer.path].used_bytes for layer in buckets)
        bucket_objects = sum(
            state.usage[layer.path].used_objects for layer in buckets
        )
        if tenant_usage.used_bytes < bucket_bytes:
            violations.append(
                {
                    "field": f"conservation[{tenant}].tenant_bytes_lt_buckets",
                    "persisted": tenant_usage.used_bytes,
                    "recomputed": bucket_bytes,
                }
            )
        if tenant_usage.used_objects < bucket_objects:
            violations.append(
                {
                    "field": f"conservation[{tenant}].tenant_objects_lt_buckets",
                    "persisted": tenant_usage.used_objects,
                    "recomputed": bucket_objects,
                }
            )
        for bucket_layer in buckets:
            prefixes = [
                layer
                for layer in quota.layers
                if (
                    layer.level == "prefix"
                    and layer.tenant == tenant
                    and layer.bucket == bucket_layer.bucket
                )
            ]
            bucket_usage = state.usage[bucket_layer.path]
            prefix_bytes = sum(
                state.usage[layer.path].used_bytes for layer in prefixes
            )
            prefix_objects = sum(
                state.usage[layer.path].used_objects for layer in prefixes
            )
            prefix_path = f"{tenant}/{bucket_layer.bucket}"
            if bucket_usage.used_bytes < prefix_bytes:
                violations.append(
                    {
                        "field": f"conservation[{prefix_path}].bucket_bytes_lt_prefixes",
                        "persisted": bucket_usage.used_bytes,
                        "recomputed": prefix_bytes,
                    }
                )
            if bucket_usage.used_objects < prefix_objects:
                violations.append(
                    {
                        "field": f"conservation[{prefix_path}].bucket_objects_lt_prefixes",
                        "persisted": bucket_usage.used_objects,
                        "recomputed": prefix_objects,
                    }
                )
    return violations

