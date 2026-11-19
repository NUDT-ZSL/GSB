"""Deterministic partition delivery, transaction and recovery engine."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    E_ACK_GAP,
    E_ACK_NOT_DELIVERED,
    E_ACK_UNCOMMITTED,
    E_BATCH_CONFLICT,
    E_BATCH_SEQ,
    E_COMMIT_DIGEST_MISMATCH,
    E_CONFIG_CONFLICT,
    E_CONSUMER_ID,
    E_DATABASE,
    E_INJECTION_CONFLICT,
    E_LOG_FULL,
    E_PARTITION_NOT_FOUND,
    E_PREPARE_TX_CONFLICT,
    E_ROUTING_CONFLICT,
    E_TX_NOT_FOUND,
    E_TX_STATE,
    E_TX_TIMEOUT,
    EngineError,
)
from .model import (
    Batch,
    EngineConfig,
    Injection,
    KeyRule,
    Topic,
    ValidatedSample,
    canonical_json,
    digest_text,
    normalize_batch,
    payload_digest_for,
    transaction_deadline,
    transaction_digest,
    validate_sample,
)
from .storage import (
    DatabaseLock,
    Store,
    compute_state_digest,
    genesis_append_digest,
    next_append_digest,
    refresh_state_digest,
)

CONFIG_META_KEY = "engine_config"
SAMPLE_DIGEST_META_KEY = "sample_digest"


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else {key: row[key] for key in row.keys()}


class Engine:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _store(self, *, create: bool) -> Iterator[Store]:
        with DatabaseLock(self.db_path):
            with Store(self.db_path).open(create=create) as store:
                yield store

    @staticmethod
    def _config_from_json(value: str) -> EngineConfig:
        obj = json.loads(value)
        return EngineConfig(
            max_partitions_per_topic=int(obj["max_partitions_per_topic"]),
            max_batch_messages=int(obj["max_batch_messages"]),
            transaction_timeout_ms=int(obj["transaction_timeout_ms"]),
            retry_budget=int(obj["retry_budget"]),
            logical_clock_start_ms=int(obj["logical_clock_start_ms"]),
            injection_manifest=str(obj["injection_manifest"]),
        )

    def _read_config(self, store: Store) -> EngineConfig:
        value = store.meta_get(CONFIG_META_KEY)
        if value is None:
            raise EngineError(
                E_DATABASE,
                "database has no engine configuration; run load first",
                exit_code=3,
            )
        return self._config_from_json(value)

    def _topics_from_store(self, store: Store) -> dict[str, Topic]:
        assert store.conn is not None
        rows = store.conn.execute("SELECT topic_json, digest FROM topics ORDER BY name").fetchall()
        topics: dict[str, Topic] = {}
        for row in rows:
            obj = json.loads(row["topic_json"])
            rules = tuple(
                KeyRule(
                    partition=int(rule["partition"]),
                    algorithm=str(rule["algorithm"]),
                    modulus=int(rule["modulus"]),
                    remainder=int(rule["remainder"]),
                    salt=str(rule["salt"]),
                    rule_type=str(rule["type"]),
                )
                for rule in obj["ordering_key_rules"]
            )
            topic = Topic(
                name=str(obj["name"]),
                partition_count=int(obj["partition_count"]),
                partition_capacities=tuple(int(value) for value in obj["partition_capacities"]),
                ordering_key_rules=rules,
                digest=str(row["digest"]),
            )
            topics[topic.name] = topic
        return topics

    def _register_sample(self, store: Store, sample: ValidatedSample) -> None:
        assert store.conn is not None
        config_json = canonical_json(sample.config.as_dict())
        with store.transaction() as conn:
            existing_config = store.meta_get(CONFIG_META_KEY)
            if existing_config is None:
                store.meta_set(conn, CONFIG_META_KEY, config_json)
                conn.execute(
                    "INSERT INTO clock_state (singleton, watermark_ms) VALUES (1, ?)",
                    (sample.config.logical_clock_start_ms,),
                )
            else:
                existing = self._config_from_json(existing_config)
                comparable_keys = (
                    "logical_clock_start_ms",
                    "max_batch_messages",
                    "max_partitions_per_topic",
                    "retry_budget",
                    "transaction_timeout_ms",
                )
                differences = {
                    key: {
                        "existing": getattr(existing, key),
                        "incoming": getattr(sample.config, key),
                    }
                    for key in comparable_keys
                    if getattr(existing, key) != getattr(sample.config, key)
                }
                if differences:
                    raise EngineError(
                        E_CONFIG_CONFLICT,
                        "sample configuration conflicts with persisted configuration",
                        details={"differences": differences},
                    )

            for topic in sample.topics:
                existing_topic = conn.execute(
                    "SELECT digest FROM topics WHERE name = ?",
                    (topic.name,),
                ).fetchone()
                topic_json = canonical_json(topic.as_dict())
                if existing_topic is not None:
                    if existing_topic["digest"] != topic.digest:
                        raise EngineError(
                            E_CONFIG_CONFLICT,
                            "topic definition conflicts with persisted metadata",
                            details={
                                "topic": topic.name,
                                "existing_digest": existing_topic["digest"],
                                "incoming_digest": topic.digest,
                            },
                        )
                    continue
                conn.execute(
                    """
                    INSERT INTO topics
                        (name, partition_count, capacities_json, rules_json, topic_json, digest)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        topic.name,
                        topic.partition_count,
                        canonical_json(list(topic.partition_capacities)),
                        canonical_json([rule.as_dict() for rule in topic.ordering_key_rules]),
                        topic_json,
                        topic.digest,
                    ),
                )
                for partition in range(topic.partition_count):
                    store.initialize_partition(conn, topic.name, partition)

            for injection in sample.injections:
                existing = conn.execute(
                    "SELECT canonical_json FROM injections WHERE injection_id = ?",
                    (injection.injection_id,),
                ).fetchone()
                if existing is not None:
                    if existing["canonical_json"] != injection.canonical:
                        raise EngineError(
                            E_INJECTION_CONFLICT,
                            "injection id conflicts with a persisted definition",
                            details={
                                "injection_id": injection.injection_id,
                                "existing": json.loads(existing["canonical_json"]),
                                "incoming": json.loads(injection.canonical),
                            },
                        )
                    continue
                conn.execute(
                    """
                    INSERT INTO injections
                        (injection_id, stage, action, timing, error_code, occurrence,
                         fired_count, match_json, canonical_json)
                    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
                    """,
                    (
                        injection.injection_id,
                        injection.stage,
                        injection.action,
                        injection.timing,
                        injection.error_code,
                        injection.occurrence,
                        canonical_json(injection.match),
                        injection.canonical,
                    ),
                )
            existing_sample_digest = store.meta_get(SAMPLE_DIGEST_META_KEY)
            if existing_sample_digest is None:
                store.meta_set(conn, SAMPLE_DIGEST_META_KEY, sample.sample_digest)

    def load_sample(
        self,
        sample_path: str | Path,
        *,
        injection_override: str | Path | None = None,
    ) -> dict[str, Any]:
        # Validation is intentionally complete before the database is opened.
        sample = validate_sample(sample_path, injection_override=injection_override)
        loaded: list[dict[str, Any]] = []
        skipped: list[int] = []
        resumed: list[dict[str, Any]] = []
        with self._store(create=True) as store:
            self._register_sample(store, sample)
            for batch in sample.batches:
                outcome = self._produce_normalized_batch(store, batch, sample.config)
                if outcome["state"] == "inserted":
                    loaded.append(
                        {
                            "batch_seq": batch.batch_seq,
                            "partition": outcome["partition"],
                            "first_offset": outcome["first_offset"],
                            "message_count": batch.message_count,
                            "transaction_id": batch.tx_id,
                        }
                    )
                elif outcome["state"] == "resumed":
                    resumed.append(
                        {
                            "batch_seq": batch.batch_seq,
                            "partition": outcome["partition"],
                            "first_offset": outcome["first_offset"],
                            "message_count": batch.message_count,
                            "transaction_id": batch.tx_id,
                        }
                    )
                else:
                    skipped.append(batch.batch_seq)
            assert store.conn is not None
            prepared = [
                row["tx_id"]
                for row in store.conn.execute(
                    "SELECT tx_id FROM transactions WHERE status = 'prepared' ORDER BY tx_id"
                ).fetchall()
            ]
            return {
                "command": "load",
                "loaded_batches": loaded,
                "logical_clock_ms": store.read_clock(),
                "prepared_transactions": prepared,
                "resumed_batches": resumed,
                "sample_digest": sample.sample_digest,
                "skipped_batches": skipped,
            }

    def _ensure_new_batch_allowed(
        self,
        conn: sqlite3.Connection,
        batch: Batch,
    ) -> None:
        row = conn.execute(
            "SELECT MAX(batch_seq) AS max_batch_seq FROM batches WHERE topic = ?",
            (batch.topic,),
        ).fetchone()
        max_batch_seq = row["max_batch_seq"]
        if max_batch_seq is not None and batch.batch_seq <= int(max_batch_seq):
            raise EngineError(
                E_BATCH_SEQ,
                "new batch_seq must be greater than the latest batch for the topic",
                details={
                    "batch_seq": batch.batch_seq,
                    "topic": batch.topic,
                    "latest_batch_seq": int(max_batch_seq),
                },
            )

    def _produce_normalized_batch(
        self,
        store: Store,
        batch: Batch,
        config: EngineConfig,
    ) -> dict[str, Any]:
        assert store.conn is not None
        state = "inserted"
        with store.transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM batches WHERE batch_seq = ?",
                (batch.batch_seq,),
            ).fetchone()
            if existing is not None:
                if existing["canonical_json"] != batch.canonical or existing["batch_digest"] != batch.digest:
                    raise EngineError(
                        E_BATCH_CONFLICT,
                        "batch_seq already exists with different content",
                        details={
                            "batch_seq": batch.batch_seq,
                            "existing_digest": existing["batch_digest"],
                            "incoming_digest": batch.digest,
                        },
                    )
                if existing["status"] in {"pending_append", "appended"}:
                    state = "resumed"
                else:
                    return {
                        "state": "duplicate",
                        "batch_seq": batch.batch_seq,
                        "partition": existing["partition"],
                        "first_offset": existing["first_offset"],
                    }
            else:
                self._ensure_new_batch_allowed(conn, batch)
                store.advance_clock(conn, batch.logical_ts_ms)
                conn.execute(
                    """
                    INSERT INTO batches
                        (batch_seq, topic, ordering_key, tx_id, logical_ts_ms,
                         canonical_json, batch_digest, status, partition,
                         message_count, first_offset)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_append', NULL, ?, NULL)
                    """,
                    (
                        batch.batch_seq,
                        batch.topic,
                        batch.ordering_key,
                        batch.tx_id,
                        batch.logical_ts_ms,
                        batch.canonical,
                        batch.digest,
                        batch.message_count,
                    ),
                )
        self._append_batch(store, batch.batch_seq, batch.topic)
        if batch.tx_id is not None:
            self._prepare_batch(store, batch.batch_seq)
        row = store.conn.execute(
            "SELECT partition, first_offset, status FROM batches WHERE batch_seq = ?",
            (batch.batch_seq,),
        ).fetchone()
        return {
            "state": state,
            "batch_seq": batch.batch_seq,
            "partition": int(row["partition"]),
            "first_offset": int(row["first_offset"]),
            "status": str(row["status"]),
        }

    def produce(self, batch_value: Any) -> dict[str, Any]:
        with self._store(create=False) as store:
            config = self._read_config(store)
            topics = self._topics_from_store(store)
            batch = normalize_batch(batch_value, topics_by_name=topics, config=config)
            outcome = self._produce_normalized_batch(store, batch, config)
            return {
                "command": "produce",
                "batch_seq": batch.batch_seq,
                "first_offset": outcome["first_offset"],
                "message_count": batch.message_count,
                "partition": outcome["partition"],
                "state": outcome["state"],
                "transaction_id": batch.tx_id,
            }

    def _topic_from_store(self, store: Store, name: str) -> Topic:
        topics = self._topics_from_store(store)
        topic = topics.get(name)
        if topic is None:
            raise EngineError(
                E_PARTITION_NOT_FOUND,
                "topic does not exist",
                details={"topic": name},
            )
        return topic

    def _select_injection(
        self,
        conn: sqlite3.Connection,
        stage: str,
        context: dict[str, Any],
    ) -> tuple[sqlite3.Row, Injection] | None:
        rows = conn.execute(
            """
            SELECT * FROM injections
            WHERE stage = ? AND fired_count < occurrence
            ORDER BY injection_id
            """,
            (stage,),
        ).fetchall()
        for row in rows:
            match = json.loads(row["match_json"])
            if all(context.get(key) == value for key, value in match.items()):
                canonical = json.loads(row["canonical_json"])
                injection = Injection(
                    injection_id=row["injection_id"],
                    stage=row["stage"],
                    action=row["action"],
                    timing=row["timing"],
                    error_code=row["error_code"],
                    occurrence=int(row["occurrence"]),
                    match=match,
                    canonical=row["canonical_json"],
                )
                return row, injection
        return None

    def _mark_injection(
        self,
        conn: sqlite3.Connection,
        injection: Injection,
        clock_ms: int,
    ) -> None:
        conn.execute(
            """
            UPDATE injections
            SET fired_count = fired_count + 1, last_clock_ms = ?
            WHERE injection_id = ?
            """,
            (clock_ms, injection.injection_id),
        )

    def _injection_error(
        self,
        injection: Injection,
        context: dict[str, Any],
        extra: dict[str, Any],
    ) -> EngineError:
        details = {"injection_id": injection.injection_id, "stage": injection.stage}
        details.update(context)
        details.update(extra)
        return EngineError(
            injection.error_code or "E_INJECTION_CONFIG",
            f"injected {injection.stage} failure",
            details=details,
        )

    def _apply_injection_before(
        self,
        store: Store,
        selected: tuple[sqlite3.Row, Injection] | None,
        context: dict[str, Any],
        *,
        clock_ms: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if selected is None:
            return
        _, injection = selected
        if injection.timing != "before":
            return
        with store.transaction() as conn:
            self._mark_injection(conn, injection, clock_ms)
        if injection.action == "crash":
            os._exit(137)
        raise self._injection_error(injection, context, extra or {})

    def _apply_injection_after(
        self,
        store: Store,
        selected: tuple[sqlite3.Row, Injection] | None,
        *,
        clock_ms: int,
    ) -> None:
        if selected is None:
            return
        _, injection = selected
        if injection.timing != "after":
            return
        with store.transaction() as conn:
            self._mark_injection(conn, injection, clock_ms)
        if injection.action == "crash":
            os._exit(137)

    def _append_batch(self, store: Store, batch_seq: int, topic_name: str) -> None:
        assert store.conn is not None
        batch_row = store.conn.execute(
            "SELECT * FROM batches WHERE batch_seq = ?",
            (batch_seq,),
        ).fetchone()
        if batch_row is None:
            raise EngineError(
                E_DATABASE,
                "batch disappeared before append",
                exit_code=3,
            )
        if batch_row["status"] != "pending_append":
            return
        topic = self._topic_from_store(store, topic_name)
        partition = topic.route(batch_row["ordering_key"])
        messages = json.loads(batch_row["canonical_json"])["messages"]
        message_count = len(messages)
        batch_clock = int(batch_row["logical_ts_ms"])

        routing_row = store.conn.execute(
            "SELECT partition FROM routing_keys WHERE topic = ? AND ordering_key = ?",
            (topic_name, batch_row["ordering_key"]),
        ).fetchone()
        if routing_row is not None and int(routing_row["partition"]) != partition:
            raise EngineError(
                E_ROUTING_CONFLICT,
                "ordering key was previously routed to another partition",
                details={
                    "topic": topic_name,
                    "ordering_key": batch_row["ordering_key"],
                    "previous_partition": int(routing_row["partition"]),
                    "computed_partition": partition,
                },
            )

        state_row = store.conn.execute(
            "SELECT * FROM partition_state WHERE topic = ? AND partition = ?",
            (topic_name, partition),
        ).fetchone()
        next_offset = int(state_row["next_offset"])
        capacity = topic.partition_capacities[partition]
        if next_offset + message_count > capacity:
            raise EngineError(
                E_LOG_FULL,
                "partition log capacity would be exceeded",
                details={
                    "batch_seq": batch_seq,
                    "capacity": capacity,
                    "message_count": message_count,
                    "next_offset": next_offset,
                    "partition": partition,
                    "topic": topic_name,
                },
            )
        context = {
            "batch_seq": batch_seq,
            "message_count": message_count,
            "partition": partition,
            "topic": topic_name,
            "tx_id": batch_row["tx_id"],
        }
        selected = self._select_injection(store.conn, "append", context)
        self._apply_injection_before(
            store,
            selected,
            context,
            clock_ms=batch_clock,
            extra={
                "capacity": capacity,
                "next_offset": next_offset,
                "required_capacity": next_offset + message_count,
            },
        )

        with store.transaction() as conn:
            current_batch = conn.execute(
                "SELECT * FROM batches WHERE batch_seq = ?",
                (batch_seq,),
            ).fetchone()
            if current_batch["status"] != "pending_append":
                return
            state = conn.execute(
                "SELECT * FROM partition_state WHERE topic = ? AND partition = ?",
                (topic_name, partition),
            ).fetchone()
            offset = int(state["next_offset"])
            if offset + message_count > capacity:
                raise EngineError(
                    E_LOG_FULL,
                    "partition log capacity would be exceeded",
                    details={
                        "batch_seq": batch_seq,
                        "capacity": capacity,
                        "message_count": message_count,
                        "next_offset": offset,
                        "partition": partition,
                        "topic": topic_name,
                    },
                )
            if routing_row is None:
                conn.execute(
                    "INSERT INTO routing_keys (topic, ordering_key, partition) VALUES (?, ?, ?)",
                    (topic_name, current_batch["ordering_key"], partition),
                )
            append_digest = state["append_digest"]
            visibility = "prepared" if current_batch["tx_id"] is not None else "visible"
            for message_index, message in enumerate(messages):
                payload = message["payload"]
                record = {
                    "batch_seq": batch_seq,
                    "logical_ts_ms": int(current_batch["logical_ts_ms"]),
                    "message_id": message["message_id"],
                    "message_index": message_index,
                    "offset": offset + message_index,
                    "ordering_key": current_batch["ordering_key"],
                    "partition": partition,
                    "payload_digest": message["payload_digest"],
                    "payload_encoding": payload["encoding"],
                    "topic": topic_name,
                    "tx_id": current_batch["tx_id"],
                }
                append_digest = next_append_digest(append_digest, record)
                conn.execute(
                    """
                    INSERT INTO partition_log
                        (topic, partition, "offset", batch_seq, message_index,
                         message_id, ordering_key, payload_encoding,
                         payload_json, payload_digest, logical_ts_ms, tx_id,
                         visibility, append_digest)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        topic_name,
                        partition,
                        offset + message_index,
                        batch_seq,
                        message_index,
                        message["message_id"],
                        current_batch["ordering_key"],
                        payload["encoding"],
                        canonical_json(payload),
                        message["payload_digest"],
                        int(current_batch["logical_ts_ms"]),
                        current_batch["tx_id"],
                        visibility,
                        append_digest,
                    ),
                )
            conn.execute(
                """
                UPDATE partition_state
                SET next_offset = ?, append_digest = ?
                WHERE topic = ? AND partition = ?
                """,
                (offset + message_count, append_digest, topic_name, partition),
            )
            refresh_state_digest(conn, topic_name, partition)
            next_status = "appended" if current_batch["tx_id"] is not None else "finished"
            conn.execute(
                """
                UPDATE batches
                SET status = ?, partition = ?, first_offset = ?
                WHERE batch_seq = ?
                """,
                (next_status, partition, offset, batch_seq),
            )
        self._apply_injection_after(store, selected, clock_ms=batch_clock)

    def _prepare_batch(self, store: Store, batch_seq: int) -> None:
        assert store.conn is not None
        batch = store.conn.execute(
            "SELECT * FROM batches WHERE batch_seq = ?",
            (batch_seq,),
        ).fetchone()
        if batch is None or batch["status"] != "appended" or batch["tx_id"] is None:
            return
        context = {
            "batch_seq": batch_seq,
            "topic": batch["topic"],
            "tx_id": batch["tx_id"],
        }
        selected = self._select_injection(store.conn, "prepare", context)
        existing_tx = store.conn.execute(
            "SELECT status FROM transactions WHERE tx_id = ?",
            (batch["tx_id"],),
        ).fetchone()
        self._apply_injection_before(
            store,
            selected,
            context,
            clock_ms=int(batch["logical_ts_ms"]),
            extra={"existing_status": None if existing_tx is None else existing_tx["status"]},
        )
        with store.transaction() as conn:
            current = conn.execute(
                "SELECT * FROM batches WHERE batch_seq = ?",
                (batch_seq,),
            ).fetchone()
            if current["status"] != "appended":
                return
            tx = conn.execute(
                "SELECT * FROM transactions WHERE tx_id = ?",
                (current["tx_id"],),
            ).fetchone()
            if tx is None:
                prepared_at = int(current["logical_ts_ms"])
                deadline = transaction_deadline(
                    prepared_at,
                    self._read_config(store).transaction_timeout_ms,
                )
                conn.execute(
                    """
                    INSERT INTO transactions
                        (tx_id, topic, status, prepared_at_ms, deadline_ms,
                         timeout_ms, committed_at_ms, finalized_at_ms,
                         tx_digest, diagnostic_json)
                    VALUES (?, ?, 'prepared', ?, ?, ?, NULL, NULL, ?, '{}')
                    """,
                    (
                        current["tx_id"],
                        current["topic"],
                        prepared_at,
                        deadline,
                        self._read_config(store).transaction_timeout_ms,
                        digest_text("[]"),
                    ),
                )
            elif tx["status"] != "prepared":
                raise EngineError(
                    E_PREPARE_TX_CONFLICT,
                    "transaction cannot accept a new batch in its current state",
                    details={
                        "batch_seq": batch_seq,
                        "tx_id": current["tx_id"],
                        "transaction_status": tx["status"],
                    },
                )
            elif tx["topic"] != current["topic"]:
                raise EngineError(
                    E_PREPARE_TX_CONFLICT,
                    "transaction spans more than one topic",
                    details={
                        "tx_id": current["tx_id"],
                        "transaction_topic": tx["topic"],
                        "batch_topic": current["topic"],
                    },
                )
            records = conn.execute(
                """
                SELECT batch_seq, message_index, payload_digest
                FROM partition_log
                WHERE tx_id = ?
                ORDER BY batch_seq, message_index
                """,
                (current["tx_id"],),
            ).fetchall()
            conn.execute(
                "UPDATE transactions SET tx_digest = ? WHERE tx_id = ?",
                (
                    transaction_digest([_row_to_dict(record) for record in records]),
                    current["tx_id"],
                ),
            )
            conn.execute(
                "UPDATE batches SET status = 'prepared' WHERE batch_seq = ?",
                (batch_seq,),
            )
        self._apply_injection_after(
            store,
            selected,
            clock_ms=int(batch["logical_ts_ms"]),
        )

    def _transaction_records(self, conn: sqlite3.Connection, tx_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT batch_seq, message_index, payload_digest
            FROM partition_log
            WHERE tx_id = ?
            ORDER BY topic, partition, "offset"
            """,
            (tx_id,),
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def _begin_operation(
        self,
        conn: sqlite3.Connection,
        op_id: str,
        kind: str,
        *,
        clock_ms: int,
        topic: str | None = None,
        partition: int | None = None,
        batch_seq: int | None = None,
        tx_id: str | None = None,
        consumer_id: str | None = None,
        offset: int | None = None,
        expected_digest: str | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO operations
                (op_id, kind, status, topic, partition, batch_seq, tx_id,
                 consumer_id, "offset", expected_digest, requested_clock_ms,
                 created_clock_ms, updated_clock_ms, diagnostic_json)
            VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}')
            ON CONFLICT(op_id) DO UPDATE SET
                status = 'pending',
                topic = excluded.topic,
                partition = excluded.partition,
                batch_seq = excluded.batch_seq,
                tx_id = excluded.tx_id,
                consumer_id = excluded.consumer_id,
                "offset" = excluded."offset",
                expected_digest = excluded.expected_digest,
                requested_clock_ms = excluded.requested_clock_ms,
                updated_clock_ms = excluded.updated_clock_ms,
                diagnostic_json = '{}'
            """,
            (
                op_id,
                kind,
                topic,
                partition,
                batch_seq,
                tx_id,
                consumer_id,
                offset,
                expected_digest,
                clock_ms,
                clock_ms,
                clock_ms,
            ),
        )

    def _finish_operation(
        self,
        conn: sqlite3.Connection,
        op_id: str,
        status: str,
        *,
        clock_ms: int,
        diagnostic: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            UPDATE operations
            SET status = ?, updated_clock_ms = ?, diagnostic_json = ?
            WHERE op_id = ?
            """,
            (status, clock_ms, canonical_json(diagnostic or {}), op_id),
        )

    def _refresh_tx_state_digests(
        self,
        conn: sqlite3.Connection,
        tx_id: str,
    ) -> None:
        rows = conn.execute(
            "SELECT DISTINCT topic, partition FROM partition_log WHERE tx_id = ?",
            (tx_id,),
        ).fetchall()
        for row in rows:
            refresh_state_digest(conn, row["topic"], row["partition"])

    def _timeout_transaction(
        self,
        conn: sqlite3.Connection,
        tx_id: str,
        *,
        clock_ms: int,
        reason: str,
    ) -> dict[str, Any]:
        tx = conn.execute(
            "SELECT * FROM transactions WHERE tx_id = ?",
            (tx_id,),
        ).fetchone()
        if tx is None:
            raise EngineError(
                E_TX_NOT_FOUND,
                "transaction does not exist",
                details={"tx_id": tx_id},
            )
        if tx["status"] != "prepared":
            raise EngineError(
                E_TX_STATE,
                "only a prepared transaction can be timed out",
                details={"tx_id": tx_id, "transaction_status": tx["status"]},
            )
        conn.execute(
            """
            UPDATE transactions
            SET status = 'timed_out', finalized_at_ms = ?, diagnostic_json = ?
            WHERE tx_id = ? AND status = 'prepared'
            """,
            (
                clock_ms,
                canonical_json(
                    {
                        "deadline_ms": tx["deadline_ms"],
                        "prepared_at_ms": tx["prepared_at_ms"],
                        "reason": reason,
                        "timed_out_at_ms": clock_ms,
                    }
                ),
                tx_id,
            ),
        )
        cursor = conn.execute(
            """
            UPDATE partition_log
            SET visibility = 'tombstone'
            WHERE tx_id = ? AND visibility = 'prepared'
            """,
            (tx_id,),
        )
        conn.execute(
            """
            UPDATE batches
            SET status = 'tombstoned'
            WHERE tx_id = ? AND status IN ('appended', 'prepared')
            """,
            (tx_id,),
        )
        self._refresh_tx_state_digests(conn, tx_id)
        return {
            "deadline_ms": int(tx["deadline_ms"]),
            "prepared_at_ms": int(tx["prepared_at_ms"]),
            "reason": reason,
            "timed_out_at_ms": clock_ms,
            "tombstoned_messages": cursor.rowcount,
            "tx_id": tx_id,
        }

    def _commit_in_transaction(
        self,
        conn: sqlite3.Connection,
        tx_id: str,
        *,
        clock_ms: int,
        operation_id: str,
    ) -> dict[str, Any]:
        tx = conn.execute(
            "SELECT * FROM transactions WHERE tx_id = ?",
            (tx_id,),
        ).fetchone()
        if tx is None:
            raise EngineError(
                E_TX_NOT_FOUND,
                "transaction does not exist",
                details={"tx_id": tx_id},
            )
        if tx["status"] == "committed":
            self._finish_operation(conn, operation_id, "done", clock_ms=clock_ms)
            return {
                "already_committed": True,
                "commit_at_ms": tx["committed_at_ms"],
                "tx_id": tx_id,
                "visible_messages": 0,
            }
        if tx["status"] == "timed_out":
            self._finish_operation(
                conn,
                operation_id,
                "failed",
                clock_ms=clock_ms,
                diagnostic={"reason": "transaction_timed_out"},
            )
            raise EngineError(
                E_TX_TIMEOUT,
                "transaction has timed out",
                details={
                    "deadline_ms": tx["deadline_ms"],
                    "prepared_at_ms": tx["prepared_at_ms"],
                    "tx_id": tx_id,
                },
            )
        if tx["status"] != "prepared":
            self._finish_operation(
                conn,
                operation_id,
                "failed",
                clock_ms=clock_ms,
                diagnostic={"reason": f"transaction_{tx['status']}"},
            )
            raise EngineError(
                E_TX_STATE,
                "transaction is not committable",
                details={"tx_id": tx_id, "transaction_status": tx["status"]},
            )
        incomplete = conn.execute(
            """
            SELECT batch_seq, status FROM batches
            WHERE tx_id = ? AND status != 'prepared'
            ORDER BY batch_seq
            """,
            (tx_id,),
        ).fetchall()
        if incomplete:
            self._finish_operation(
                conn,
                operation_id,
                "failed",
                clock_ms=clock_ms,
                diagnostic={"reason": "transaction_has_unprepared_batches"},
            )
            raise EngineError(
                E_TX_STATE,
                "transaction has batches that are not prepared",
                details={
                    "tx_id": tx_id,
                    "incomplete_batches": [_row_to_dict(row) for row in incomplete],
                },
            )
        if clock_ms >= int(tx["deadline_ms"]):
            timeout = self._timeout_transaction(
                conn,
                tx_id,
                clock_ms=clock_ms,
                reason="commit_after_deadline",
            )
            self._finish_operation(
                conn,
                operation_id,
                "failed",
                clock_ms=clock_ms,
                diagnostic=timeout,
            )
            return {"_error": "timeout", "details": timeout}
        cursor = conn.execute(
            """
            UPDATE transactions
            SET status = 'committed', committed_at_ms = ?, finalized_at_ms = ?
            WHERE tx_id = ? AND status = 'prepared'
            """,
            (clock_ms, clock_ms, tx_id),
        )
        if cursor.rowcount != 1:
            raise EngineError(
                E_TX_STATE,
                "transaction state changed during commit",
                details={"tx_id": tx_id},
            )
        visible = conn.execute(
            """
            UPDATE partition_log
            SET visibility = 'visible'
            WHERE tx_id = ? AND visibility = 'prepared'
            """,
            (tx_id,),
        ).rowcount
        conn.execute(
            """
            UPDATE batches SET status = 'committed'
            WHERE tx_id = ? AND status = 'prepared'
            """,
            (tx_id,),
        )
        self._refresh_tx_state_digests(conn, tx_id)
        self._finish_operation(
            conn,
            operation_id,
            "done",
            clock_ms=clock_ms,
            diagnostic={"visible_messages": visible},
        )
        return {
            "already_committed": False,
            "commit_at_ms": clock_ms,
            "tx_id": tx_id,
            "visible_messages": visible,
        }

    def _begin_commit_operation(
        self,
        store: Store,
        tx_id: str,
        *,
        clock_ms: int,
        expected_digest: str | None,
    ) -> str:
        operation_id = f"commit:{tx_id}"
        with store.transaction() as conn:
            self._begin_operation(
                conn,
                operation_id,
                "commit",
                clock_ms=clock_ms,
                tx_id=tx_id,
                expected_digest=expected_digest,
            )
        return operation_id

    def _commit_transaction(
        self,
        store: Store,
        tx_id: str,
        *,
        clock_ms: int,
        expected_digest: str | None,
        operation_id: str,
        apply_injection: bool,
    ) -> dict[str, Any]:
        assert store.conn is not None
        tx = store.conn.execute(
            "SELECT * FROM transactions WHERE tx_id = ?",
            (tx_id,),
        ).fetchone()
        if tx is None:
            raise EngineError(
                E_TX_NOT_FOUND,
                "transaction does not exist",
                details={"tx_id": tx_id},
            )
        if tx["status"] == "committed":
            with store.transaction() as conn:
                self._finish_operation(
                    conn,
                    operation_id,
                    "done",
                    clock_ms=clock_ms,
                    diagnostic={"already_committed": True},
                )
            return {
                "already_committed": True,
                "commit_at_ms": tx["committed_at_ms"],
                "tx_id": tx_id,
                "visible_messages": 0,
            }
        if tx["status"] == "timed_out":
            with store.transaction() as conn:
                self._finish_operation(
                    conn,
                    operation_id,
                    "failed",
                    clock_ms=clock_ms,
                    diagnostic={"reason": "transaction_timed_out"},
                )
            raise EngineError(
                E_TX_TIMEOUT,
                "transaction has timed out",
                details={
                    "deadline_ms": tx["deadline_ms"],
                    "prepared_at_ms": tx["prepared_at_ms"],
                    "tx_id": tx_id,
                },
            )
        if tx["status"] != "prepared":
            with store.transaction() as conn:
                self._finish_operation(
                    conn,
                    operation_id,
                    "failed",
                    clock_ms=clock_ms,
                    diagnostic={"reason": f"transaction_{tx['status']}"},
                )
            raise EngineError(
                E_TX_STATE,
                "transaction is not committable",
                details={"tx_id": tx_id, "transaction_status": tx["status"]},
            )
        records = self._transaction_records(store.conn, tx_id)
        actual_digest = transaction_digest(records)
        if expected_digest is not None and expected_digest != actual_digest:
            with store.transaction() as conn:
                self._finish_operation(
                    conn,
                    operation_id,
                    "failed",
                    clock_ms=clock_ms,
                    diagnostic={
                        "actual_digest": actual_digest,
                        "expected_digest": expected_digest,
                    },
                )
            raise EngineError(
                E_COMMIT_DIGEST_MISMATCH,
                "commit digest does not match the prepared transaction",
                details={
                    "actual_digest": actual_digest,
                    "expected_digest": expected_digest,
                    "tx_id": tx_id,
                },
            )

        selected = None
        context = {"tx_id": tx_id, "topic": tx["topic"]}
        if apply_injection:
            selected = self._select_injection(store.conn, "commit", context)
            self._apply_injection_before(
                store,
                selected,
                context,
                clock_ms=clock_ms,
                extra={
                    "actual_digest": actual_digest,
                    "expected_digest": digest_text(f"injected:{tx_id}"),
                },
            )
        try:
            with store.transaction() as conn:
                result = self._commit_in_transaction(
                    conn,
                    tx_id,
                    clock_ms=clock_ms,
                    operation_id=operation_id,
                )
        except EngineError as exc:
            if exc.code != E_TX_TIMEOUT:
                with store.transaction() as conn:
                    self._finish_operation(
                        conn,
                        operation_id,
                        "failed",
                        clock_ms=clock_ms,
                        diagnostic=exc.as_dict(),
                    )
            raise
        if result.get("_error") == "timeout":
            raise EngineError(
                E_TX_TIMEOUT,
                "transaction timed out before commit",
                details=result["details"],
            )
        self._apply_injection_after(store, selected, clock_ms=clock_ms)
        return result

    def commit(
        self,
        tx_id: str,
        logical_ms: int,
        *,
        expected_digest: str | None = None,
    ) -> dict[str, Any]:
        with self._store(create=False) as store:
            with store.transaction() as conn:
                store.advance_clock(conn, logical_ms)
            tx = store.conn.execute(
                "SELECT * FROM transactions WHERE tx_id = ?",
                (tx_id,),
            ).fetchone()
            if tx is None:
                raise EngineError(
                    E_TX_NOT_FOUND,
                    "transaction does not exist",
                    details={"tx_id": tx_id},
                )
            if tx["status"] == "committed":
                return {
                    "command": "commit",
                    "commit_at_ms": tx["committed_at_ms"],
                    "state": "already_committed",
                    "tx_id": tx_id,
                    "visible_messages": 0,
                }
            if tx["status"] == "timed_out":
                raise EngineError(
                    E_TX_TIMEOUT,
                    "transaction has timed out",
                    details={
                        "deadline_ms": tx["deadline_ms"],
                        "prepared_at_ms": tx["prepared_at_ms"],
                        "tx_id": tx_id,
                    },
                )
            operation_id = self._begin_commit_operation(
                store,
                tx_id,
                clock_ms=logical_ms,
                expected_digest=expected_digest,
            )
            result = self._commit_transaction(
                store,
                tx_id,
                clock_ms=logical_ms,
                expected_digest=expected_digest,
                operation_id=operation_id,
                apply_injection=True,
            )
            return {
                "command": "commit",
                "commit_at_ms": result["commit_at_ms"],
                "state": "already_committed" if result["already_committed"] else "committed",
                "tx_id": tx_id,
                "visible_messages": result["visible_messages"],
            }

    def rollback(self, tx_id: str, logical_ms: int) -> dict[str, Any]:
        with self._store(create=False) as store:
            with store.transaction() as conn:
                store.advance_clock(conn, logical_ms)
            tx = store.conn.execute(
                "SELECT * FROM transactions WHERE tx_id = ?",
                (tx_id,),
            ).fetchone()
            if tx is None:
                raise EngineError(
                    E_TX_NOT_FOUND,
                    "transaction does not exist",
                    details={"tx_id": tx_id},
                )
            if tx["status"] in {"timed_out", "rolled_back"}:
                return {
                    "command": "rollback",
                    "state": tx["status"],
                    "tombstoned_messages": 0,
                    "tx_id": tx_id,
                }
            if tx["status"] != "prepared":
                raise EngineError(
                    E_TX_STATE,
                    "only a prepared transaction can be rolled back",
                    details={"tx_id": tx_id, "transaction_status": tx["status"]},
                )
            with store.transaction() as conn:
                current = conn.execute(
                    "SELECT status FROM transactions WHERE tx_id = ?",
                    (tx_id,),
                ).fetchone()
                if current["status"] != "prepared":
                    raise EngineError(
                        E_TX_STATE,
                        "transaction state changed before rollback",
                        details={"tx_id": tx_id, "transaction_status": current["status"]},
                    )
                conn.execute(
                    """
                    UPDATE transactions
                    SET status = 'rolled_back', finalized_at_ms = ?, diagnostic_json = ?
                    WHERE tx_id = ? AND status = 'prepared'
                    """,
                    (
                        logical_ms,
                        canonical_json({"reason": "explicit_rollback", "rolled_back_at_ms": logical_ms}),
                        tx_id,
                    ),
                )
                cursor = conn.execute(
                    """
                    UPDATE partition_log SET visibility = 'tombstone'
                    WHERE tx_id = ? AND visibility = 'prepared'
                    """,
                    (tx_id,),
                )
                conn.execute(
                    """
                    UPDATE batches SET status = 'tombstoned'
                    WHERE tx_id = ? AND status IN ('appended', 'prepared')
                    """,
                    (tx_id,),
                )
                self._refresh_tx_state_digests(conn, tx_id)
            return {
                "command": "rollback",
                "state": "rolled_back",
                "tombstoned_messages": cursor.rowcount,
                "tx_id": tx_id,
            }

    @staticmethod
    def _validate_consumer(consumer_id: Any) -> str:
        if not isinstance(consumer_id, str) or not consumer_id or len(consumer_id) > 128:
            raise EngineError(
                E_CONSUMER_ID,
                "consumer_id must be a non-empty string of at most 128 characters",
                details={"consumer_id": consumer_id},
            )
        return consumer_id

    def _validate_partition(
        self,
        store: Store,
        topic_name: str,
        partition: int,
    ) -> Topic:
        if isinstance(partition, bool) or not isinstance(partition, int):
            raise EngineError(
                E_PARTITION_NOT_FOUND,
                "partition must be an integer",
                details={"partition": partition, "topic": topic_name},
            )
        topic = self._topic_from_store(store, topic_name)
        if partition < 0 or partition >= topic.partition_count:
            raise EngineError(
                E_PARTITION_NOT_FOUND,
                "partition does not exist",
                details={
                    "partition": partition,
                    "partition_count": topic.partition_count,
                    "topic": topic_name,
                },
            )
        return topic

    def _ensure_consumer(
        self,
        conn: sqlite3.Connection,
        consumer_id: str,
        topic: str,
        partition: int,
        clock_ms: int,
    ) -> int:
        row = conn.execute(
            """
            SELECT committed_offset FROM consumer_offsets
            WHERE consumer_id = ? AND topic = ? AND partition = ?
            """,
            (consumer_id, topic, partition),
        ).fetchone()
        if row is not None:
            return int(row["committed_offset"])
        conn.execute(
            """
            INSERT INTO consumer_offsets
                (consumer_id, topic, partition, committed_offset, updated_clock_ms)
            VALUES (?, ?, ?, 0, ?)
            """,
            (consumer_id, topic, partition, clock_ms),
        )
        return 0

    @staticmethod
    def _delivery_id(
        consumer_id: str,
        topic: str,
        partition: int,
        offset: int,
        payload_digest: str,
    ) -> str:
        return digest_text(
            canonical_json(
                {
                    "consumer_id": consumer_id,
                    "offset": offset,
                    "partition": partition,
                    "payload_digest": payload_digest,
                    "topic": topic,
                }
            )
        )[:32]

    def _delivery_projection(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "batch_seq": int(row["batch_seq"]),
            "delivery_id": row["delivery_id"] if "delivery_id" in row.keys() else None,
            "delivery_status": (
                row["delivery_status"]
                if "delivery_status" in row.keys()
                else (row["status"] if "status" in row.keys() else "pending")
            ),
            "logical_ts_ms": int(row["logical_ts_ms"]),
            "message_id": row["message_id"],
            "message_index": int(row["message_index"]),
            "offset": int(row["offset"]),
            "ordering_key": row["ordering_key"],
            "payload": json.loads(row["payload_json"]),
            "payload_digest": row["payload_digest"],
            "tx_id": row["tx_id"],
        }

    def consume(
        self,
        consumer_id: str,
        topic_name: str,
        partition: int,
        max_messages: int,
        logical_ms: int,
    ) -> dict[str, Any]:
        consumer_id = self._validate_consumer(consumer_id)
        if isinstance(max_messages, bool) or not isinstance(max_messages, int) or max_messages < 1:
            raise EngineError(
                "E_CONSUME_LIMIT",
                "max_messages must be a positive integer",
                details={"max_messages": max_messages},
            )
        with self._store(create=False) as store:
            self._validate_partition(store, topic_name, partition)
            with store.transaction() as conn:
                store.advance_clock(conn, logical_ms)
                committed = self._ensure_consumer(
                    conn, consumer_id, topic_name, partition, logical_ms
                )
                pending = conn.execute(
                    """
                    SELECT d.*, l.batch_seq AS batch_seq, l.message_index AS message_index,
                           l.message_id AS message_id, l.ordering_key AS ordering_key,
                           l.payload_json AS payload_json, l.payload_digest AS payload_digest,
                           l.logical_ts_ms AS logical_ts_ms, l.tx_id AS tx_id,
                           l.visibility AS visibility
                    FROM deliveries d
                    JOIN partition_log l
                      ON l.topic = d.topic AND l.partition = d.partition
                     AND l."offset" = d."offset"
                    WHERE d.consumer_id = ? AND d.topic = ? AND d.partition = ?
                      AND d.status = 'pending'
                    ORDER BY d."offset"
                    LIMIT ?
                    """,
                    (consumer_id, topic_name, partition, max_messages),
                ).fetchall()
                if pending:
                    return {
                        "command": "consume",
                        "committed_offset": committed,
                        "consumer_id": consumer_id,
                        "logical_clock_ms": store.read_clock(),
                        "messages": [self._delivery_projection(row) for row in pending],
                        "partition": partition,
                        "pending_replay": True,
                        "topic": topic_name,
                    }

                scan = committed
                messages: list[dict[str, Any]] = []
                advanced_over_tombstones = 0
                while len(messages) < max_messages:
                    row = conn.execute(
                        """
                        SELECT * FROM partition_log
                        WHERE topic = ? AND partition = ? AND "offset" >= ?
                        ORDER BY "offset"
                        LIMIT 1
                        """,
                        (topic_name, partition, scan),
                    ).fetchone()
                    if row is None or int(row["offset"]) != scan:
                        break
                    if row["visibility"] == "prepared":
                        break
                    if row["visibility"] == "tombstone":
                        scan += 1
                        advanced_over_tombstones += 1
                        continue
                    delivery_id = self._delivery_id(
                        consumer_id,
                        topic_name,
                        partition,
                        scan,
                        row["payload_digest"],
                    )
                    existing = conn.execute(
                        """
                        SELECT status FROM deliveries
                        WHERE consumer_id = ? AND topic = ? AND partition = ? AND "offset" = ?
                        """,
                        (consumer_id, topic_name, partition, scan),
                    ).fetchone()
                    if existing is None:
                        conn.execute(
                            """
                            INSERT INTO deliveries
                                (consumer_id, topic, partition, "offset", delivery_id,
                                 batch_seq, message_index, payload_digest, status,
                                 delivered_clock_ms, acked_clock_ms)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, NULL)
                            """,
                            (
                                consumer_id,
                                topic_name,
                                partition,
                                scan,
                                delivery_id,
                                row["batch_seq"],
                                row["message_index"],
                                row["payload_digest"],
                                logical_ms,
                            ),
                        )
                    message = self._delivery_projection(row)
                    message["delivery_id"] = delivery_id
                    message["delivery_status"] = "pending"
                    messages.append(message)
                    scan += 1
                if advanced_over_tombstones and scan > committed:
                    first_delivered = min((item["offset"] for item in messages), default=scan)
                    new_committed = max(committed, first_delivered)
                    if new_committed > committed:
                        conn.execute(
                            """
                            UPDATE consumer_offsets
                            SET committed_offset = ?, updated_clock_ms = ?
                            WHERE consumer_id = ? AND topic = ? AND partition = ?
                            """,
                            (new_committed, logical_ms, consumer_id, topic_name, partition),
                        )
                        committed = new_committed
                elif advanced_over_tombstones:
                    conn.execute(
                        """
                        UPDATE consumer_offsets
                        SET committed_offset = ?, updated_clock_ms = ?
                        WHERE consumer_id = ? AND topic = ? AND partition = ?
                        """,
                        (scan, logical_ms, consumer_id, topic_name, partition),
                    )
                    committed = scan
                return {
                    "command": "consume",
                    "committed_offset": committed,
                    "consumer_id": consumer_id,
                    "logical_clock_ms": store.read_clock(),
                    "messages": messages,
                    "partition": partition,
                    "pending_replay": False,
                    "topic": topic_name,
                }

    def _ack_check_and_advance(
        self,
        conn: sqlite3.Connection,
        consumer_id: str,
        topic: str,
        partition: int,
        offset: int,
        logical_ms: int,
    ) -> dict[str, Any]:
        consumer = conn.execute(
            """
            SELECT committed_offset FROM consumer_offsets
            WHERE consumer_id = ? AND topic = ? AND partition = ?
            """,
            (consumer_id, topic, partition),
        ).fetchone()
        committed = 0 if consumer is None else int(consumer["committed_offset"])
        if offset < committed:
            return {
                "acked_offset": offset,
                "committed_offset": committed,
                "state": "stale_idempotent",
            }
        blockers = conn.execute(
            """
            SELECT "offset", visibility FROM partition_log
            WHERE topic = ? AND partition = ? AND "offset" >= ? AND "offset" < ?
              AND visibility != 'tombstone'
            ORDER BY "offset"
            """,
            (topic, partition, committed, offset),
        ).fetchall()
        if blockers:
            prepared = [row for row in blockers if row["visibility"] == "prepared"]
            if prepared:
                raise EngineError(
                    E_ACK_UNCOMMITTED,
                    "acknowledgement would skip an uncommitted message",
                    details={
                        "blocked_offsets": [int(row["offset"]) for row in blockers],
                        "consumer_id": consumer_id,
                        "partition": partition,
                        "topic": topic,
                    },
                )
            raise EngineError(
                E_ACK_GAP,
                "acknowledgement would skip a visible unacknowledged message",
                details={
                    "blocked_offsets": [int(row["offset"]) for row in blockers],
                    "consumer_id": consumer_id,
                    "committed_offset": committed,
                    "requested_offset": offset,
                    "topic": topic,
                    "partition": partition,
                },
            )
        log_row = conn.execute(
            """
            SELECT * FROM partition_log
            WHERE topic = ? AND partition = ? AND "offset" = ?
            """,
            (topic, partition, offset),
        ).fetchone()
        if log_row is None or log_row["visibility"] == "tombstone":
            raise EngineError(
                E_ACK_NOT_DELIVERED,
                "offset has no delivered message",
                details={
                    "consumer_id": consumer_id,
                    "offset": offset,
                    "partition": partition,
                    "topic": topic,
                },
            )
        if log_row["visibility"] == "prepared":
            raise EngineError(
                E_ACK_UNCOMMITTED,
                "uncommitted message cannot be acknowledged",
                details={
                    "consumer_id": consumer_id,
                    "offset": offset,
                    "partition": partition,
                    "topic": topic,
                    "tx_id": log_row["tx_id"],
                },
            )
        delivery = conn.execute(
            """
            SELECT * FROM deliveries
            WHERE consumer_id = ? AND topic = ? AND partition = ? AND "offset" = ?
            """,
            (consumer_id, topic, partition, offset),
        ).fetchone()
        if delivery is None:
            raise EngineError(
                E_ACK_NOT_DELIVERED,
                "offset was not delivered to this consumer",
                details={
                    "consumer_id": consumer_id,
                    "offset": offset,
                    "partition": partition,
                    "topic": topic,
                },
            )
        if delivery["status"] == "acked":
            return {
                "acked_offset": offset,
                "committed_offset": committed,
                "state": "duplicate_idempotent",
            }
        conn.execute(
            """
            UPDATE deliveries
            SET status = 'acked', acked_clock_ms = ?
            WHERE consumer_id = ? AND topic = ? AND partition = ? AND "offset" = ?
              AND status = 'pending'
            """,
            (logical_ms, consumer_id, topic, partition, offset),
        )
        new_committed = offset + 1
        conn.execute(
            """
            INSERT INTO consumer_offsets
                (consumer_id, topic, partition, committed_offset, updated_clock_ms)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(consumer_id, topic, partition) DO UPDATE SET
                committed_offset = excluded.committed_offset,
                updated_clock_ms = excluded.updated_clock_ms
            """,
            (consumer_id, topic, partition, new_committed, logical_ms),
        )
        return {
            "acked_offset": offset,
            "committed_offset": new_committed,
            "state": "acked",
        }

    def ack(
        self,
        consumer_id: str,
        topic_name: str,
        partition: int,
        offset: int,
        logical_ms: int,
    ) -> dict[str, Any]:
        consumer_id = self._validate_consumer(consumer_id)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise EngineError(
                E_ACK_NOT_DELIVERED,
                "offset must be a non-negative integer",
                details={"offset": offset},
            )
        with self._store(create=False) as store:
            self._validate_partition(store, topic_name, partition)
            with store.transaction() as conn:
                store.advance_clock(conn, logical_ms)
                consumer = self._ensure_consumer(
                    conn, consumer_id, topic_name, partition, logical_ms
                )
                operation_id = (
                    f"ack:{consumer_id}:{topic_name}:{partition}:{offset}"
                )
                self._begin_operation(
                    conn,
                    operation_id,
                    "ack",
                    clock_ms=logical_ms,
                    topic=topic_name,
                    partition=partition,
                    consumer_id=consumer_id,
                    offset=offset,
                )
            context = {
                "consumer_id": consumer_id,
                "offset": offset,
                "partition": partition,
                "topic": topic_name,
            }
            selected = self._select_injection(store.conn, "ack", context)
            self._apply_injection_before(
                store,
                selected,
                context,
                clock_ms=logical_ms,
                extra={
                    "committed_offset": consumer,
                    "forced_offset": max(-1, consumer - 1),
                },
            )
            try:
                with store.transaction() as conn:
                    result = self._ack_check_and_advance(
                        conn,
                        consumer_id,
                        topic_name,
                        partition,
                        offset,
                        logical_ms,
                    )
                    self._finish_operation(
                        conn,
                        operation_id,
                        "done",
                        clock_ms=logical_ms,
                        diagnostic=result,
                    )
            except EngineError as exc:
                with store.transaction() as conn:
                    self._finish_operation(
                        conn,
                        operation_id,
                        "failed",
                        clock_ms=logical_ms,
                        diagnostic=exc.as_dict(),
                    )
                raise
            self._apply_injection_after(store, selected, clock_ms=logical_ms)
            return {
                "acked_offset": result["acked_offset"],
                "command": "ack",
                "committed_offset": result["committed_offset"],
                "consumer_id": consumer_id,
                "partition": partition,
                "state": result["state"],
                "topic": topic_name,
            }

    def _resume_ack_operation(
        self,
        store: Store,
        operation: sqlite3.Row,
    ) -> dict[str, Any]:
        assert store.conn is not None
        clock_ms = int(operation["requested_clock_ms"])
        try:
            with store.transaction() as conn:
                result = self._ack_check_and_advance(
                    conn,
                    operation["consumer_id"],
                    operation["topic"],
                    int(operation["partition"]),
                    int(operation["offset"]),
                    clock_ms,
                )
                self._finish_operation(
                    conn,
                    operation["op_id"],
                    "done",
                    clock_ms=clock_ms,
                    diagnostic=result,
                )
            return {
                "op_id": operation["op_id"],
                "result": result,
                "state": "completed",
            }
        except EngineError as exc:
            with store.transaction() as conn:
                self._finish_operation(
                    conn,
                    operation["op_id"],
                    "failed",
                    clock_ms=clock_ms,
                    diagnostic=exc.as_dict(),
                )
            return {
                "error": exc.as_dict(),
                "op_id": operation["op_id"],
                "state": "failed",
            }

    def _cleanup_placeholders(self, store: Store) -> list[dict[str, Any]]:
        assert store.conn is not None
        cleaned: list[dict[str, Any]] = []
        with store.transaction() as conn:
            rows = conn.execute(
                """
                SELECT l.topic, l.partition, l."offset", l.tx_id, l.batch_seq,
                       t.status AS transaction_status
                FROM partition_log l
                LEFT JOIN transactions t ON t.tx_id = l.tx_id
                WHERE l.visibility = 'prepared'
                  AND (l.tx_id IS NULL OR t.tx_id IS NULL OR t.status != 'prepared')
                ORDER BY l.topic, l.partition, l."offset"
                """
            ).fetchall()
            if not rows:
                return []
            touched: set[tuple[str, int]] = set()
            for row in rows:
                conn.execute(
                    """
                    UPDATE partition_log SET visibility = 'tombstone'
                    WHERE topic = ? AND partition = ? AND "offset" = ?
                      AND visibility = 'prepared'
                    """,
                    (row["topic"], row["partition"], row["offset"]),
                )
                touched.add((row["topic"], int(row["partition"])))
                cleaned.append(
                    {
                        "batch_seq": int(row["batch_seq"]),
                        "offset": int(row["offset"]),
                        "partition": int(row["partition"]),
                        "reason": "unreferenced_placeholder",
                        "topic": row["topic"],
                        "transaction_id": row["tx_id"],
                    }
                )
            for topic, partition in sorted(touched):
                refresh_state_digest(conn, topic, partition)
            orphan_batches = conn.execute(
                """
                SELECT b.batch_seq
                FROM batches b
                LEFT JOIN transactions t ON t.tx_id = b.tx_id
                WHERE b.status = 'prepared' AND (b.tx_id IS NULL OR t.tx_id IS NULL)
                ORDER BY b.batch_seq
                """
            ).fetchall()
            for row in orphan_batches:
                conn.execute(
                    "UPDATE batches SET status = 'tombstoned' WHERE batch_seq = ?",
                    (row["batch_seq"],),
                )
        return cleaned

    def resume(self, logical_ms: int | None = None) -> dict[str, Any]:
        with self._store(create=False) as store:
            if logical_ms is not None:
                with store.transaction() as conn:
                    store.advance_clock(conn, logical_ms)
            now = store.read_clock()
            completed_batches: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            pending_batches = store.conn.execute(
                """
                SELECT batch_seq, topic, status FROM batches
                WHERE status IN ('pending_append', 'appended')
                ORDER BY batch_seq
                """
            ).fetchall()
            for row in pending_batches:
                try:
                    if row["status"] == "pending_append":
                        self._append_batch(store, int(row["batch_seq"]), row["topic"])
                    current = store.conn.execute(
                        "SELECT status FROM batches WHERE batch_seq = ?",
                        (row["batch_seq"],),
                    ).fetchone()
                    if current["status"] == "appended":
                        self._prepare_batch(store, int(row["batch_seq"]))
                    current = store.conn.execute(
                        "SELECT status, partition, first_offset FROM batches WHERE batch_seq = ?",
                        (row["batch_seq"],),
                    ).fetchone()
                    completed_batches.append(
                        {
                            "batch_seq": int(row["batch_seq"]),
                            "first_offset": None
                            if current["first_offset"] is None
                            else int(current["first_offset"]),
                            "partition": None
                            if current["partition"] is None
                            else int(current["partition"]),
                            "status": current["status"],
                        }
                    )
                except EngineError as exc:
                    errors.append(
                        {
                            "batch_seq": int(row["batch_seq"]),
                            "error": exc.as_dict(),
                        }
                    )

            completed_operations: list[dict[str, Any]] = []
            pending_operations = store.conn.execute(
                """
                SELECT * FROM operations
                WHERE status = 'pending'
                ORDER BY op_seq
                """
            ).fetchall()
            for operation in pending_operations:
                if operation["kind"] == "commit":
                    requested_clock = operation["requested_clock_ms"]
                    if requested_clock is None:
                        requested_clock = now
                    try:
                        result = self._commit_transaction(
                            store,
                            operation["tx_id"],
                            clock_ms=int(requested_clock),
                            expected_digest=operation["expected_digest"],
                            operation_id=operation["op_id"],
                            apply_injection=False,
                        )
                        completed_operations.append(
                            {
                                "op_id": operation["op_id"],
                                "result": result,
                                "state": "completed",
                            }
                        )
                    except EngineError as exc:
                        errors.append(
                            {
                                "error": exc.as_dict(),
                                "op_id": operation["op_id"],
                            }
                        )
                elif operation["kind"] == "ack":
                    ack_result = self._resume_ack_operation(store, operation)
                    completed_operations.append(ack_result)

            cleaned = self._cleanup_placeholders(store)
            timed_out: list[dict[str, Any]] = []
            prepared = store.conn.execute(
                """
                SELECT tx_id FROM transactions
                WHERE status = 'prepared' AND deadline_ms <= ?
                ORDER BY tx_id
                """,
                (now,),
            ).fetchall()
            for row in prepared:
                try:
                    with store.transaction() as conn:
                        timed_out.append(
                            self._timeout_transaction(
                                conn,
                                row["tx_id"],
                                clock_ms=now,
                                reason="recovery_timeout_sweep",
                            )
                        )
                except EngineError as exc:
                    errors.append(
                        {
                            "error": exc.as_dict(),
                            "tx_id": row["tx_id"],
                        }
                    )

            pending_deliveries = store.conn.execute(
                """
                SELECT consumer_id, topic, partition, "offset"
                FROM deliveries
                WHERE status = 'pending'
                ORDER BY consumer_id, topic, partition, "offset"
                """
            ).fetchall()
            return {
                "cleaned_placeholders": cleaned,
                "command": "resume",
                "completed_batches": completed_batches,
                "completed_operations": completed_operations,
                "errors": errors,
                "logical_clock_ms": now,
                "pending_deliveries": [
                    {
                        "consumer_id": row["consumer_id"],
                        "offset": int(row["offset"]),
                        "partition": int(row["partition"]),
                        "topic": row["topic"],
                    }
                    for row in pending_deliveries
                ],
                "timed_out_transactions": timed_out,
            }

    def status(self) -> dict[str, Any]:
        with self._store(create=False) as store:
            assert store.conn is not None
            partitions: list[dict[str, Any]] = []
            state_rows = store.conn.execute(
                """
                SELECT topic, partition, next_offset
                FROM partition_state
                ORDER BY topic, partition
                """
            ).fetchall()
            for state in state_rows:
                counts = {
                    row["visibility"]: int(row["count"])
                    for row in store.conn.execute(
                        """
                        SELECT visibility, COUNT(*) AS count
                        FROM partition_log
                        WHERE topic = ? AND partition = ?
                        GROUP BY visibility
                        """,
                        (state["topic"], state["partition"]),
                    ).fetchall()
                }
                pending_transactions = store.conn.execute(
                    """
                    SELECT COUNT(DISTINCT l.tx_id) AS count
                    FROM partition_log l
                    JOIN transactions t ON t.tx_id = l.tx_id
                    WHERE l.topic = ? AND l.partition = ? AND t.status = 'prepared'
                    """,
                    (state["topic"], state["partition"]),
                ).fetchone()["count"]
                partitions.append(
                    {
                        "offset_watermark": int(state["next_offset"]),
                        "partition": int(state["partition"]),
                        "pending_transactions": int(pending_transactions),
                        "prepared_messages": counts.get("prepared", 0),
                        "tombstones": counts.get("tombstone", 0),
                        "topic": state["topic"],
                        "visible_messages": counts.get("visible", 0),
                    }
                )
            transaction_counts = {
                row["status"]: int(row["count"])
                for row in store.conn.execute(
                    "SELECT status, COUNT(*) AS count FROM transactions GROUP BY status"
                ).fetchall()
            }
            consumers = [
                {
                    "committed_offset": int(row["committed_offset"]),
                    "consumer_id": row["consumer_id"],
                    "partition": int(row["partition"]),
                    "topic": row["topic"],
                }
                for row in store.conn.execute(
                    """
                    SELECT consumer_id, topic, partition, committed_offset
                    FROM consumer_offsets
                    ORDER BY consumer_id, topic, partition
                    """
                ).fetchall()
            ]
            pending_operations = store.conn.execute(
                "SELECT COUNT(*) AS count FROM operations WHERE status = 'pending'"
            ).fetchone()["count"]
            return {
                "command": "status",
                "consumer_offsets": consumers,
                "logical_clock_ms": store.read_clock(),
                "partitions": partitions,
                "pending_operations": int(pending_operations),
                "transaction_counts": transaction_counts,
            }

    def verify(self) -> dict[str, Any]:
        with self._store(create=False) as store:
            assert store.conn is not None
            errors: list[dict[str, Any]] = []
            partition_summaries: list[dict[str, Any]] = []
            state_rows = store.conn.execute(
                """
                SELECT topic, partition, next_offset, append_digest, state_digest
                FROM partition_state
                ORDER BY topic, partition
                """
            ).fetchall()
            for state in state_rows:
                topic = state["topic"]
                partition = int(state["partition"])
                rows = store.conn.execute(
                    """
                    SELECT * FROM partition_log
                    WHERE topic = ? AND partition = ?
                    ORDER BY "offset"
                    """,
                    (topic, partition),
                ).fetchall()
                if len(rows) != int(state["next_offset"]):
                    errors.append(
                        {
                            "actual": len(rows),
                            "expected": int(state["next_offset"]),
                            "kind": "offset_watermark_mismatch",
                            "partition": partition,
                            "topic": topic,
                        }
                    )
                append_digest = genesis_append_digest(topic, partition)
                for expected_offset, row in enumerate(rows):
                    if int(row["offset"]) != expected_offset:
                        errors.append(
                            {
                                "actual": int(row["offset"]),
                                "expected": expected_offset,
                                "kind": "offset_gap",
                                "partition": partition,
                                "topic": topic,
                            }
                        )
                    record = _row_to_dict(row)
                    append_digest = next_append_digest(append_digest, record)
                    if append_digest != row["append_digest"]:
                        errors.append(
                            {
                                "kind": "append_digest_mismatch",
                                "offset": int(row["offset"]),
                                "partition": partition,
                                "topic": topic,
                            }
                        )
                    try:
                        payload = json.loads(row["payload_json"])
                        computed_payload_digest = payload_digest_for(
                            row["payload_encoding"], payload.get("data")
                        )
                    except (json.JSONDecodeError, EngineError) as exc:
                        errors.append(
                            {
                                "kind": "payload_decode_failure",
                                "offset": int(row["offset"]),
                                "partition": partition,
                                "reason": str(exc),
                                "topic": topic,
                            }
                        )
                    else:
                        if computed_payload_digest != row["payload_digest"]:
                            errors.append(
                                {
                                    "kind": "payload_digest_mismatch",
                                    "offset": int(row["offset"]),
                                    "partition": partition,
                                    "topic": topic,
                                }
                            )
                    if row["visibility"] == "prepared":
                        tx = store.conn.execute(
                            "SELECT status FROM transactions WHERE tx_id = ?",
                            (row["tx_id"],),
                        ).fetchone()
                        if row["tx_id"] is None or tx is None or tx["status"] != "prepared":
                            errors.append(
                                {
                                    "kind": "invalid_prepared_visibility",
                                    "offset": int(row["offset"]),
                                    "partition": partition,
                                    "topic": topic,
                                    "tx_id": row["tx_id"],
                                }
                            )
                    elif row["visibility"] == "visible" and row["tx_id"] is not None:
                        tx = store.conn.execute(
                            "SELECT status FROM transactions WHERE tx_id = ?",
                            (row["tx_id"],),
                        ).fetchone()
                        if tx is None or tx["status"] != "committed":
                            errors.append(
                                {
                                    "kind": "visible_uncommitted_transaction",
                                    "offset": int(row["offset"]),
                                    "partition": partition,
                                    "topic": topic,
                                    "tx_id": row["tx_id"],
                                }
                            )
                if append_digest != state["append_digest"]:
                    errors.append(
                        {
                            "actual": append_digest,
                            "expected": state["append_digest"],
                            "kind": "partition_append_digest_mismatch",
                            "partition": partition,
                            "topic": topic,
                        }
                    )
                computed_state = compute_state_digest(store.conn, topic, partition)
                if computed_state != state["state_digest"]:
                    errors.append(
                        {
                            "actual": computed_state,
                            "expected": state["state_digest"],
                            "kind": "partition_state_digest_mismatch",
                            "partition": partition,
                            "topic": topic,
                        }
                    )
                partition_summaries.append(
                    {
                        "append_digest": state["append_digest"],
                        "offset_watermark": int(state["next_offset"]),
                        "partition": partition,
                        "state_digest": state["state_digest"],
                        "topic": topic,
                    }
                )

            for batch in store.conn.execute(
                "SELECT * FROM batches ORDER BY batch_seq"
            ).fetchall():
                if digest_text(batch["canonical_json"]) != batch["batch_digest"]:
                    errors.append(
                        {
                            "batch_seq": int(batch["batch_seq"]),
                            "kind": "batch_digest_mismatch",
                        }
                    )
                logs = store.conn.execute(
                    """
                    SELECT "offset", message_index FROM partition_log
                    WHERE batch_seq = ?
                    ORDER BY message_index
                    """,
                    (batch["batch_seq"],),
                ).fetchall()
                if batch["status"] == "pending_append" and logs:
                    errors.append(
                        {
                            "actual": len(logs),
                            "batch_seq": int(batch["batch_seq"]),
                            "expected": 0,
                            "kind": "pending_batch_has_appended_messages",
                        }
                    )
                elif batch["status"] != "pending_append" and len(logs) != int(
                    batch["message_count"]
                ):
                    errors.append(
                        {
                            "actual": len(logs),
                            "batch_seq": int(batch["batch_seq"]),
                            "expected": int(batch["message_count"]),
                            "kind": "batch_message_count_mismatch",
                        }
                    )
                if logs and int(logs[0]["offset"]) != int(batch["first_offset"]):
                    errors.append(
                        {
                            "actual": int(logs[0]["offset"]),
                            "batch_seq": int(batch["batch_seq"]),
                            "expected": int(batch["first_offset"]),
                            "kind": "batch_first_offset_mismatch",
                        }
                    )
                expected_status = {
                    "pending_append": None,
                    "appended": "prepared",
                    "prepared": "prepared",
                    "finished": None,
                    "committed": "committed",
                    "tombstoned": "tombstoned",
                }[batch["status"]]
                if batch["status"] == "committed" and batch["tx_id"] is not None:
                    tx = store.conn.execute(
                        "SELECT status FROM transactions WHERE tx_id = ?",
                        (batch["tx_id"],),
                    ).fetchone()
                    if tx is None or tx["status"] != "committed":
                        errors.append(
                            {
                                "batch_seq": int(batch["batch_seq"]),
                                "kind": "batch_transaction_status_mismatch",
                                "tx_id": batch["tx_id"],
                            }
                        )
                elif expected_status == "prepared" and batch["tx_id"] is not None:
                    tx = store.conn.execute(
                        "SELECT status FROM transactions WHERE tx_id = ?",
                        (batch["tx_id"],),
                    ).fetchone()
                    if tx is None or tx["status"] != "prepared":
                        errors.append(
                            {
                                "batch_seq": int(batch["batch_seq"]),
                                "kind": "batch_transaction_status_mismatch",
                                "tx_id": batch["tx_id"],
                            }
                        )
                elif batch["status"] == "tombstoned" and batch["tx_id"] is not None:
                    tx = store.conn.execute(
                        "SELECT status FROM transactions WHERE tx_id = ?",
                        (batch["tx_id"],),
                    ).fetchone()
                    if tx is not None and tx["status"] not in {"timed_out", "rolled_back"}:
                        errors.append(
                            {
                                "batch_seq": int(batch["batch_seq"]),
                                "kind": "batch_transaction_status_mismatch",
                                "tx_id": batch["tx_id"],
                            }
                        )

            for tx in store.conn.execute(
                "SELECT * FROM transactions ORDER BY tx_id"
            ).fetchall():
                records = self._transaction_records(store.conn, tx["tx_id"])
                if transaction_digest(records) != tx["tx_digest"]:
                    errors.append(
                        {
                            "kind": "transaction_digest_mismatch",
                            "tx_id": tx["tx_id"],
                        }
                    )
                prepared_visibility = store.conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM partition_log
                    WHERE tx_id = ? AND visibility = 'prepared'
                    """,
                    (tx["tx_id"],),
                ).fetchone()["count"]
                visible_visibility = store.conn.execute(
                    """
                    SELECT COUNT(*) AS count FROM partition_log
                    WHERE tx_id = ? AND visibility = 'visible'
                    """,
                    (tx["tx_id"],),
                ).fetchone()["count"]
                if tx["status"] == "committed" and prepared_visibility:
                    errors.append(
                        {
                            "kind": "committed_transaction_has_prepared_messages",
                            "tx_id": tx["tx_id"],
                        }
                    )
                if tx["status"] in {"timed_out", "rolled_back"} and visible_visibility:
                    errors.append(
                        {
                            "kind": "terminal_transaction_has_visible_messages",
                            "tx_id": tx["tx_id"],
                        }
                    )
                if int(tx["deadline_ms"]) != transaction_deadline(
                    int(tx["prepared_at_ms"]), int(tx["timeout_ms"])
                ):
                    errors.append(
                        {
                            "kind": "transaction_deadline_mismatch",
                            "tx_id": tx["tx_id"],
                        }
                    )

            for consumer in store.conn.execute(
                """
                SELECT c.*, p.next_offset
                FROM consumer_offsets c
                LEFT JOIN partition_state p
                  ON p.topic = c.topic AND p.partition = c.partition
                ORDER BY c.consumer_id, c.topic, c.partition
                """
            ).fetchall():
                if consumer["next_offset"] is None or int(consumer["committed_offset"]) > int(
                    consumer["next_offset"]
                ):
                    errors.append(
                        {
                            "committed_offset": int(consumer["committed_offset"]),
                            "consumer_id": consumer["consumer_id"],
                            "kind": "consumer_offset_out_of_range",
                            "partition": int(consumer["partition"]),
                            "topic": consumer["topic"],
                        }
                    )
            for delivery in store.conn.execute(
                """
                SELECT d.*, l.visibility
                FROM deliveries d
                LEFT JOIN partition_log l
                  ON l.topic = d.topic AND l.partition = d.partition
                 AND l."offset" = d."offset"
                ORDER BY d.consumer_id, d.topic, d.partition, d."offset"
                """
            ).fetchall():
                if delivery["visibility"] != "visible":
                    errors.append(
                        {
                            "consumer_id": delivery["consumer_id"],
                            "kind": "delivery_not_from_visible_message",
                            "offset": int(delivery["offset"]),
                            "partition": int(delivery["partition"]),
                            "topic": delivery["topic"],
                        }
                    )
            return {
                "checks": {
                    "append_chains": len(partition_summaries),
                    "batches": store.conn.execute("SELECT COUNT(*) AS c FROM batches").fetchone()["c"],
                    "partitions": len(partition_summaries),
                    "transactions": store.conn.execute(
                        "SELECT COUNT(*) AS c FROM transactions"
                    ).fetchone()["c"],
                },
                "command": "verify",
                "errors": errors,
                "ok": not errors,
                "partitions": partition_summaries,
            }
            selected = self._select_injection(store.conn, "ack", context)
            self._apply_injection_before(
                store,
                selected,
                context,
                clock_ms=logical_ms,
                extra={
                    "committed_offset": consumer,
                    "forced_offset": max(-1, consumer - 1),
                },
            )
            try:
                with store.transaction() as conn:
                    result = self._ack_check_and_advance(
                        conn,
                        consumer_id,
                        topic_name,
                        partition,
                        offset,
                        logical_ms,
                    )
                    self._finish_operation(
                        conn,
                        operation_id,
                        "done",
                        clock_ms=logical_ms,
                        diagnostic=result,
                    )
            except EngineError as exc:
                with store.transaction() as conn:
                    self._finish_operation(
                        conn,
                        operation_id,
                        "failed",
                        clock_ms=logical_ms,
                        diagnostic=exc.as_dict(),
                    )
                raise
            self._apply_injection_after(store, selected, clock_ms=logical_ms)
            return {
                "acked_offset": result["acked_offset"],
                "command": "ack",
                "committed_offset": result["committed_offset"],
                "consumer_id": consumer_id,
                "partition": partition,
                "state": result["state"],
                "topic": topic_name,
            }



