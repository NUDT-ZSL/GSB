from __future__ import annotations

import json
import math
import os
import sqlite3
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Iterable, Mapping

from .errors import (
    E_BACKLOG_LIMIT,
    E_CLOCK_REGRESSION,
    E_DB_CONFLICT,
    E_DELIVERY_NOT_FOUND,
    E_EVENT_CONFLICT,
    E_EVENT_NOT_FOUND,
    E_SUBSCRIBER_MISMATCH,
    E_SUBSCRIBER_NOT_FOUND,
    E_SEQUENCE_VIOLATION,
    GatewayError,
)
from .faults import maybe_fault
from .ledger import (
    TERMINAL_STATUSES,
    UNRESOLVED_STATUSES,
    acked_prefix,
    append_event,
    backoff_ms,
    backlog_depth,
    committed_prefix,
    delivery_cost_ms,
    delivery_id,
    first_unresolved_offset,
    idempotency_key,
    logical_state_fingerprint,
    max_generation,
    new_state,
    partition_summary,
    simulate_delivery_outcome,
    summarize_delivery,
)
from .routing import partition_id, route_partition
from .scheduling import decide_lock_mode, lock_key_for_decision
from .storage import Storage, canonical_json
from .validation import (
    EventSpec,
    GatewayConfig,
    Sample,
    SubscriptionSpec,
    TopicSpec,
    parse_config,
    parse_events,
    parse_subscriptions,
    parse_topics,
    validate_sample,
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class GatewayEngine:
    """SQLite-backed deterministic ordered-delivery gateway."""

    def __init__(self, db_path: str | Path):
        self.storage = Storage(db_path)

    @property
    def db_path(self) -> Path:
        return self.storage.db_path

    # ------------------------------------------------------------------
    # Bootstrap / metadata
    # ------------------------------------------------------------------
    def load_documents(
        self,
        topics_document: Any,
        events_document: Any,
        subscriptions_document: Any,
        config_document: Any,
        *,
        ingest_events: bool = True,
    ) -> dict[str, Any]:
        sample = validate_sample(
            topics_document,
            events_document,
            subscriptions_document,
            config_document,
        )
        return self.load_sample(sample, ingest_events=ingest_events)

    def load_sample(
        self,
        sample: Sample,
        *,
        ingest_events: bool = True,
    ) -> dict[str, Any]:
        connection = self.storage.connect()
        try:
            self.storage.ensure_schema(connection)
            topics_payload = [topic.to_dict() for topic in sample.topics]
            subscriptions_payload = [
                sub.to_dict() for sub in sample.subscriptions
            ]
            config_payload = sample.config.to_dict()
            events = list(sample.events) if ingest_events else []

            with self.storage.transaction(connection):
                existing_state = self.storage.load_state(connection)
                existing_meta = self.storage.read_meta(connection)
                if existing_state is not None:
                    if existing_meta.get("metadata_hash") != sample.metadata_hash:
                        raise GatewayError(
                            E_DB_CONFLICT,
                            "database metadata differs from the supplied sample",
                            {
                                "db_path": str(self.db_path),
                                "stored_metadata_hash": existing_meta.get("metadata_hash"),
                                "sample_metadata_hash": sample.metadata_hash,
                            },
                        )
                    appended, skipped = self._append_events_tx(
                        connection,
                        existing_state,
                        sample.topics,
                        sample.subscriptions,
                        sample.config,
                        events,
                    )
                    state_version = existing_state["state_version"]
                    if appended:
                        maybe_fault("append_before_commit")
                        state_version = self.storage.commit_state(
                            connection,
                            existing_state,
                        )
                    return self._load_result(
                        sample,
                        state_version=state_version,
                        ingested=len(appended),
                        skipped=len(skipped),
                        created=False,
                    )

                if existing_meta:
                    raise GatewayError(
                        E_DB_CONFLICT,
                        "database contains metadata without a ledger pointer",
                        {"db_path": str(self.db_path)},
                    )

                self.storage.write_metadata(
                    connection,
                    topics=topics_payload,
                    subscriptions=subscriptions_payload,
                    config=config_payload,
                    metadata_hash=sample.metadata_hash,
                    sample_hash=sample.sample_hash,
                )
                state = new_state(sample.topics, sample.subscriptions)
                appended, skipped = self._append_events_tx(
                    connection,
                    state,
                    sample.topics,
                    sample.subscriptions,
                    sample.config,
                    events,
                )
                maybe_fault("append_before_commit")
                state_version = self.storage.commit_state(connection, state)
                return self._load_result(
                    sample,
                    state_version=state_version,
                    ingested=len(appended),
                    skipped=len(skipped),
                    created=True,
                )
        except BaseException:
            connection.close()
            raise
        else:
            connection.close()

    def _load_result(
        self,
        sample: Sample,
        *,
        state_version: int,
        ingested: int,
        skipped: int,
        created: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "command": "load",
            "db_path": str(self.db_path),
            "created": created,
            "ingested_events": ingested,
            "idempotent_events": skipped,
            "ledger_version": state_version,
            "metadata_hash": sample.metadata_hash,
            "sample_hash": sample.sample_hash,
            "topic_count": len(sample.topics),
            "partition_count": sum(topic.partition_count for topic in sample.topics),
            "subscription_count": len(sample.subscriptions),
        }

    def _runtime_context(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[
        tuple[TopicSpec, ...],
        tuple[SubscriptionSpec, ...],
        GatewayConfig,
    ]:
        topics = parse_topics({"topics": self.storage.read_topics(connection)})
        subscriptions = parse_subscriptions(
            {"subscriptions": self.storage.read_subscriptions(connection)},
            topics,
        )
        config_payload = self.storage.read_config(connection)
        if config_payload is None:
            raise GatewayError(
                "E_NOT_LOADED",
                "gateway configuration is missing",
                {"db_path": str(self.db_path)},
            )
        config = parse_config(config_payload)
        return topics, subscriptions, config

    # ------------------------------------------------------------------
    # Production / append
    # ------------------------------------------------------------------
    def produce(self, events_document: Any) -> dict[str, Any]:
        connection = self.storage.connect()
        try:
            self.storage.ensure_schema(connection)
            topics, subscriptions, config = self._runtime_context(connection)
            events = parse_events(events_document, topics)
            if not events:
                return {
                    "ok": True,
                    "command": "produce",
                    "appended": 0,
                    "idempotent": 0,
                    "offsets": [],
                }
            topic_map = {topic.name: topic for topic in topics}
            affected_partitions = sorted(
                {
                    partition_id(
                        event.topic,
                        route_partition(
                            event.topic,
                            topic_map[event.topic].partition_count,
                            event.partition_key,
                            topic_map[event.topic].partition_key_rule,
                        ),
                    )
                    for event in events
                }
            )
            with ExitStack() as stack:
                for pid in affected_partitions:
                    stack.enter_context(
                        self.storage.partition_lock(f"{pid}:append")
                    )
                with self.storage.transaction(connection):
                    state = self.storage.require_state(connection)
                    appended, skipped = self._append_events_tx(
                        connection,
                        state,
                        topics,
                        subscriptions,
                        config,
                        list(events),
                    )
                    state_version = state["state_version"]
                    if appended:
                        maybe_fault("append_before_commit")
                        state_version = self.storage.commit_state(
                            connection,
                            state,
                        )
                    return {
                        "ok": True,
                        "command": "produce",
                        "appended": len(appended),
                        "idempotent": len(skipped),
                        "offsets": appended,
                        "ledger_version": state_version,
                    }
        finally:
            connection.close()

    def _append_events_tx(
        self,
        connection: sqlite3.Connection,
        state: dict[str, Any],
        topics: tuple[TopicSpec, ...],
        subscriptions: tuple[SubscriptionSpec, ...],
        config: GatewayConfig,
        events: Iterable[EventSpec],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        topic_map = {topic.name: topic for topic in topics}
        subscription_map = {sub.subscriber_id: sub for sub in subscriptions}
        appended: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        by_seq: dict[int, sqlite3.Row] = {}
        by_id: dict[str, sqlite3.Row] = {}
        for row in connection.execute(
            """
            SELECT event_id, event_seq, topic, partition_id, offset_no,
                   partition_key, payload_digest, logical_clock_ms,
                   expected_delivery_generation
            FROM partition_log
            """
        ):
            by_seq[int(row["event_seq"])] = row
            by_id[str(row["event_id"])] = row

        for event in sorted(events, key=lambda item: item.event_seq):
            existing_seq = by_seq.get(event.event_seq)
            existing_id = by_id.get(event.event_id)
            if existing_seq is not None or existing_id is not None:
                current = existing_seq or existing_id
                same = (
                    current is not None
                    and event.event_id == current["event_id"]
                    and event.event_seq == int(current["event_seq"])
                    and event.topic == current["topic"]
                    and event.partition_key == current["partition_key"]
                    and event.payload_digest == current["payload_digest"]
                    and event.logical_clock_ms == int(current["logical_clock_ms"])
                    and event.expected_delivery_generation
                    == int(current["expected_delivery_generation"])
                )
                if not same:
                    raise GatewayError(
                        E_EVENT_CONFLICT,
                        "event identifier was previously used with different content",
                        {
                            "event_seq": event.event_seq,
                            "event_id": event.event_id,
                            "existing": dict(current) if current is not None else None,
                        },
                    )
                skipped.append(
                    {
                        "event_seq": event.event_seq,
                        "event_id": event.event_id,
                        "partition_id": current["partition_id"],
                        "offset": int(current["offset_no"]),
                        "idempotent": True,
                    }
                )
                continue

            topic = topic_map[event.topic]
            partition_index = route_partition(
                topic.name,
                topic.partition_count,
                event.partition_key,
                topic.partition_key_rule,
            )
            pid = partition_id(topic.name, partition_index)
            partition = state["partitions"][pid]
            previous_clock = int(partition["clock_watermark"])
            if event.logical_clock_ms < previous_clock - config.clock_tolerance_ms:
                raise GatewayError(
                    E_CLOCK_REGRESSION,
                    "injected logical clock regressed beyond tolerance",
                    {
                        "partition_id": pid,
                        "event_seq": event.event_seq,
                        "previous_watermark": previous_clock,
                        "event_clock_ms": event.logical_clock_ms,
                        "tolerance_ms": config.clock_tolerance_ms,
                    },
                )
            backlog = backlog_depth(partition)
            if backlog >= topic.backlog_limit:
                raise GatewayError(
                    E_BACKLOG_LIMIT,
                    "partition backlog limit is exhausted",
                    {
                        "partition_id": pid,
                        "backlog_depth": backlog,
                        "backlog_limit": topic.backlog_limit,
                        "event_seq": event.event_seq,
                    },
                )

            offset = append_event(
                state,
                topic,
                event,
                pid,
                subscription_map,
            )
            connection.execute(
                """
                INSERT INTO partition_log(
                    partition_id, offset_no, event_seq, event_id, topic,
                    partition_key, payload_digest, logical_clock_ms,
                    expected_delivery_generation
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pid,
                    offset,
                    event.event_seq,
                    event.event_id,
                    event.topic,
                    event.partition_key,
                    event.payload_digest,
                    event.logical_clock_ms,
                    event.expected_delivery_generation,
                ),
            )
            appended.append(
                {
                    "event_seq": event.event_seq,
                    "event_id": event.event_id,
                    "topic": event.topic,
                    "partition_id": pid,
                    "offset": offset,
                    "clock_ms": event.logical_clock_ms,
                    "generation": 1,
                }
            )
            synthetic_row = {
                "event_id": event.event_id,
                "event_seq": event.event_seq,
                "topic": event.topic,
                "partition_id": pid,
                "offset_no": offset,
                "partition_key": event.partition_key,
                "payload_digest": event.payload_digest,
                "logical_clock_ms": event.logical_clock_ms,
                "expected_delivery_generation": event.expected_delivery_generation,
            }
            by_seq[event.event_seq] = synthetic_row  # type: ignore[assignment]
            by_id[event.event_id] = synthetic_row  # type: ignore[assignment]
        return appended, skipped

    # ------------------------------------------------------------------
    # Delivery scheduler
    # ------------------------------------------------------------------
    def deliver(
        self,
        subscriber_id: str,
        *,
        round_id: str | None = None,
        budget_override_ms: int | None = None,
    ) -> dict[str, Any]:
        if budget_override_ms is not None and budget_override_ms <= 0:
            raise GatewayError(
                "E_BAD_SAMPLE",
                "budget override must be a positive integer",
                {"budget_override_ms": budget_override_ms},
            )
        round_id = round_id or f"round-{uuid.uuid4().hex}"
        connection = self.storage.connect()
        try:
            self.storage.ensure_schema(connection)
            topics, subscriptions, config = self._runtime_context(connection)
            subscription_map = {sub.subscriber_id: sub for sub in subscriptions}
            if subscriber_id not in subscription_map:
                raise GatewayError(
                    E_SUBSCRIBER_NOT_FOUND,
                    "subscription does not exist",
                    {
                        "subscriber_id": subscriber_id,
                        "known_subscribers": sorted(subscription_map),
                    },
                )
            existing_round = self._read_round(connection, round_id)
            if existing_round is not None:
                payload = json.loads(existing_round)
                payload["idempotent_replay"] = True
                return payload
        finally:
            connection.close()

        with self.storage.round_lock(round_id):
            connection = self.storage.connect()
            try:
                topics, subscriptions, config = self._runtime_context(connection)
                subscription_map = {sub.subscriber_id: sub for sub in subscriptions}
                subscription = subscription_map[subscriber_id]
                existing_round = self._read_round(connection, round_id)
                if existing_round is not None:
                    payload = json.loads(existing_round)
                    payload["idempotent_replay"] = True
                    return payload
                state = self.storage.require_state(connection)
                topic = next(
                    item for item in topics if item.name == subscription.topic
                )
                plan = [
                    partition_id(topic.name, index)
                    for index in range(topic.partition_count)
                ]
                budget = (
                    budget_override_ms
                    if budget_override_ms is not None
                    else config.catchup_logical_time_budget_ms
                )
                budget = self._initialize_round_progress(
                    connection,
                    round_id,
                    subscriber_id,
                    plan,
                    subscription.pull_batch_size,
                    budget,
                )
                result = self._execute_round(
                    round_id=round_id,
                    subscriber_id=subscriber_id,
                    subscription=subscription,
                    config=config,
                    initial_state=state,
                    plan=plan,
                    budget=budget,
                )
                self._record_round(connection, round_id, result)
                return result
            finally:
                connection.close()

    def _read_round(
        self,
        connection: sqlite3.Connection,
        round_id: str,
    ) -> str | None:
        row = connection.execute(
            "SELECT result FROM rounds WHERE round_id = ?",
            (round_id,),
        ).fetchone()
        return str(row["result"]) if row else None

    def _initialize_round_progress(
        self,
        connection: sqlite3.Connection,
        round_id: str,
        subscriber_id: str,
        plan: list[str],
        planned_batch: int,
        requested_budget: int,
    ) -> int:
        with self.storage.transaction(connection):
            existing = connection.execute(
                """
                SELECT budget_ms FROM round_state WHERE round_id = ?
                """,
                (round_id,),
            ).fetchone()
            if existing is None:
                effective_budget = int(requested_budget)
                connection.execute(
                    """
                    INSERT INTO round_state(
                        round_id, subscriber_id, budget_ms,
                        budget_remaining_ms, status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'in_progress', ?, ?)
                    """,
                    (
                        round_id,
                        subscriber_id,
                        effective_budget,
                        effective_budget,
                        _now_iso(),
                        _now_iso(),
                    ),
                )
            else:
                effective_budget = int(existing["budget_ms"])
            for pid in plan:
                connection.execute(
                    """
                    INSERT INTO round_partitions(
                        round_id, partition_id, planned_batch,
                        completed_attempts, estimated_cost_ms,
                        status, reason, updated_at
                    ) VALUES (?, ?, ?, 0, 0, 'planned', NULL, ?)
                    ON CONFLICT(round_id, partition_id) DO NOTHING
                    """,
                    (round_id, pid, planned_batch, _now_iso()),
                )
        return effective_budget

    def _reserve_round_start(
        self,
        round_id: str,
        pid: str,
        estimate: int,
    ) -> tuple[bool, str]:
        connection = self.storage.connect()
        try:
            with self.storage.transaction(connection):
                state_row = connection.execute(
                    """
                    SELECT budget_remaining_ms
                    FROM round_state WHERE round_id = ?
                    """,
                    (round_id,),
                ).fetchone()
                if state_row is None:
                    raise GatewayError(
                        E_SEQUENCE_VIOLATION,
                        "round state is missing",
                        {"round_id": round_id},
                    )
                remaining = int(state_row["budget_remaining_ms"])
                if estimate > remaining:
                    reason = (
                        f"remaining_budget_ms={remaining}"
                        f"<estimated_partition_cost_ms={estimate}"
                    )
                    connection.execute(
                        """
                        UPDATE round_partitions
                        SET status = 'abandoned', reason = ?, updated_at = ?
                        WHERE round_id = ? AND status = 'planned'
                        """,
                        (reason, _now_iso(), round_id),
                    )
                    return False, reason
                connection.execute(
                    """
                    UPDATE round_state
                    SET budget_remaining_ms = budget_remaining_ms - ?,
                        updated_at = ?
                    WHERE round_id = ?
                    """,
                    (estimate, _now_iso(), round_id),
                )
                connection.execute(
                    """
                    UPDATE round_partitions
                    SET status = 'scheduled', estimated_cost_ms = ?,
                        reason = NULL, updated_at = ?
                    WHERE round_id = ? AND partition_id = ?
                    """,
                    (estimate, _now_iso(), round_id, pid),
                )
                return True, ""
        finally:
            connection.close()

    def _execute_round(
        self,
        *,
        round_id: str,
        subscriber_id: str,
        subscription: SubscriptionSpec,
        config: GatewayConfig,
        initial_state: dict[str, Any],
        plan: list[str],
        budget: int,
    ) -> dict[str, Any]:
        remaining_budget = int(budget)
        outcomes: dict[str, dict[str, Any]] = {}
        active: dict[Future[dict[str, Any]], dict[str, Any]] = {}
        pending = list(plan)
        max_workers = max(1, min(config.global_concurrency_limit, len(plan)))
        planned_concurrency = self._planned_work_concurrency(
            initial_state,
            plan,
            subscriber_id,
            config.global_concurrency_limit,
        )
        executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=f"deliver-{round_id[:16]}",
        )
        try:
            while pending or active:
                while pending and len(active) < max_workers:
                    pid = pending[0]
                    inspection = self._inspect_partition(
                        round_id,
                        pid,
                        subscriber_id,
                        subscription,
                        config,
                        planned_concurrency,
                    )
                    progress_status = inspection["progress_status"]
                    if progress_status == "abandoned":
                        outcomes[pid] = {
                            "partition_id": pid,
                            "outcome": "ABANDONED_BUDGET",
                            "reason": inspection["progress_reason"]
                            or "partition was abandoned by the persisted round plan",
                            "started": False,
                            "estimated_cost_ms": int(
                                inspection["progress_estimated_cost_ms"]
                            ),
                            "actual_cost_ms": 0,
                            "deliveries_attempted": 0,
                            "acked": 0,
                            "failed_attempts": 0,
                            "dead_letters": 0,
                            "mode_decision": inspection["mode_decision"],
                        }
                        pending.pop(0)
                        continue
                    if inspection["no_work"]:
                        outcomes[pid] = {
                            "partition_id": pid,
                            "outcome": "NOT_STARTED_IN_BUDGET",
                            "reason": inspection["reason"],
                            "started": False,
                            "estimated_cost_ms": 0,
                            "actual_cost_ms": 0,
                            "deliveries_attempted": 0,
                            "acked": 0,
                            "failed_attempts": 0,
                            "dead_letters": 0,
                            "mode_decision": inspection["mode_decision"],
                        }
                        pending.pop(0)
                        continue
                    estimate = int(inspection["estimated_cost_ms"])
                    if progress_status == "planned":
                        reserved, reason = self._reserve_round_start(
                            round_id,
                            pid,
                            estimate,
                        )
                        if not reserved:
                            for abandoned in pending:
                                outcomes[abandoned] = {
                                    "partition_id": abandoned,
                                    "outcome": "ABANDONED_BUDGET",
                                    "reason": reason,
                                    "started": False,
                                    "estimated_cost_ms": estimate,
                                    "actual_cost_ms": 0,
                                    "deliveries_attempted": 0,
                                    "acked": 0,
                                    "failed_attempts": 0,
                                    "dead_letters": 0,
                                    "mode_decision": None,
                                }
                            pending.clear()
                            break
                        remaining_budget -= estimate
                    elif int(inspection["progress_estimated_cost_ms"]) > 0:
                        estimate = int(inspection["progress_estimated_cost_ms"])
                    decision = inspection["mode_decision"]
                    future = executor.submit(
                        self._run_partition,
                        round_id,
                        subscriber_id,
                        subscription,
                        config,
                        pid,
                        decision,
                        inspection["batch_remaining"],
                    )
                    active[future] = {
                        "partition_id": pid,
                        "estimated_cost_ms": estimate,
                        "mode_decision": decision,
                    }
                    pending.pop(0)

                if not active:
                    break
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    metadata = active.pop(future)
                    pid = metadata["partition_id"]
                    try:
                        worker_result = future.result()
                    except BaseException as exc:
                        if isinstance(exc, GatewayError):
                            error_payload = {
                                "code": exc.code,
                                "message": exc.message,
                                "details": exc.details,
                            }
                            reason = f"{exc.code}: {exc.message}"
                        else:
                            error_payload = {
                                "code": "E_INTERNAL",
                                "message": str(exc),
                                "details": {"type": type(exc).__name__},
                            }
                            reason = f"E_INTERNAL: {exc}"
                        worker_result = {
                            "partition_id": pid,
                            "outcome": "STARTED_FAILED",
                            "reason": reason,
                            "started": True,
                            "estimated_cost_ms": metadata["estimated_cost_ms"],
                            "actual_cost_ms": 0,
                            "deliveries_attempted": 0,
                            "acked": 0,
                            "failed_attempts": 0,
                            "dead_letters": 0,
                            "mode_decision": metadata["mode_decision"],
                            "error": error_payload,
                        }
                    worker_result["estimated_cost_ms"] = int(
                        metadata["estimated_cost_ms"]
                    )
                    outcomes[pid] = worker_result
        finally:
            executor.shutdown(wait=True, cancel_futures=False)

        connection = self.storage.connect()
        try:
            state = self.storage.require_state(connection)
            round_state_row = connection.execute(
                """
                SELECT budget_ms, budget_remaining_ms
                FROM round_state WHERE round_id = ?
                """,
                (round_id,),
            ).fetchone()
            effective_budget = (
                int(round_state_row["budget_ms"])
                if round_state_row is not None
                else int(budget)
            )
            stored_remaining = (
                int(round_state_row["budget_remaining_ms"])
                if round_state_row is not None
                else int(remaining_budget)
            )
            for pid in plan:
                outcome = outcomes.get(pid)
                if outcome is None:
                    outcome = {
                        "partition_id": pid,
                        "outcome": "NOT_STARTED_IN_BUDGET",
                        "reason": "round ended before the partition was scheduled",
                        "started": False,
                        "estimated_cost_ms": 0,
                        "actual_cost_ms": 0,
                        "deliveries_attempted": 0,
                        "acked": 0,
                        "failed_attempts": 0,
                        "dead_letters": 0,
                        "mode_decision": None,
                    }
                    outcomes[pid] = outcome
                round_counts = connection.execute(
                    """
                    SELECT
                        COUNT(*) AS attempts,
                        SUM(CASE WHEN status = 'acked' THEN 1 ELSE 0 END) AS acked,
                        SUM(CASE WHEN status IN ('failed', 'dead') THEN 1 ELSE 0 END)
                            AS failed_attempts,
                        SUM(CASE WHEN status = 'dead' THEN 1 ELSE 0 END) AS dead_letters
                    FROM deliveries
                    WHERE round_id = ? AND partition_id = ?
                    """,
                    (round_id, pid),
                ).fetchone()
                attempts = int(round_counts["attempts"] or 0)
                if attempts > 0:
                    outcome["outcome"] = "STARTED_COMPLETED"
                    outcome["started"] = True
                    outcome["deliveries_attempted"] = attempts
                    outcome["acked"] = int(round_counts["acked"] or 0)
                    outcome["failed_attempts"] = int(
                        round_counts["failed_attempts"] or 0
                    )
                    outcome["dead_letters"] = int(
                        round_counts["dead_letters"] or 0
                    )
                    if outcome.get("reason", "").startswith(
                        "round batch already completed"
                    ):
                        outcome["reason"] = "batch_limit"
                cost_row = connection.execute(
                    """
                    SELECT actual_cost_ms FROM round_partitions
                    WHERE round_id = ? AND partition_id = ?
                    """,
                    (round_id, pid),
                ).fetchone()
                if cost_row is not None:
                    outcome["actual_cost_ms"] = int(cost_row["actual_cost_ms"])
                partition = state["partitions"][pid]
                outcome["reconciliation"] = summarize_delivery(
                    partition,
                    subscriber_id,
                )
                outcome["partition_status"] = partition_summary(partition)
            ordered = [outcomes[pid] for pid in plan]
            total_attempts = sum(
                int(item.get("deliveries_attempted", 0)) for item in ordered
            )
            total_acks = sum(int(item.get("acked", 0)) for item in ordered)
            total_failures = sum(
                int(item.get("failed_attempts", 0)) for item in ordered
            )
            total_dead = sum(int(item.get("dead_letters", 0)) for item in ordered)
            total_actual_cost = sum(
                int(item.get("actual_cost_ms", 0)) for item in ordered
            )
            budget_reserved = effective_budget - stored_remaining
            return {
                "ok": True,
                "command": "deliver",
                "round_id": round_id,
                "subscriber_id": subscriber_id,
                "budget_ms": effective_budget,
                "budget_remaining_ms": stored_remaining,
                "partitions": ordered,
                "summary": {
                    "partitions": len(ordered),
                    "started": sum(1 for item in ordered if item.get("started")),
                    "completed": sum(
                        1
                        for item in ordered
                        if item.get("outcome") == "STARTED_COMPLETED"
                    ),
                    "failed_after_start": sum(
                        1
                        for item in ordered
                        if item.get("outcome") == "STARTED_FAILED"
                    ),
                    "not_started_in_budget": sum(
                        1
                        for item in ordered
                        if item.get("outcome") == "NOT_STARTED_IN_BUDGET"
                    ),
                    "abandoned_budget": sum(
                        1
                        for item in ordered
                        if item.get("outcome") == "ABANDONED_BUDGET"
                    ),
                    "delivery_attempts": total_attempts,
                    "acked": total_acks,
                    "failed_attempts": total_failures,
                    "dead_letters": total_dead,
                    "actual_cost_ms": total_actual_cost,
                    "budget_reserved_ms": budget_reserved,
                    "budget_overshoot_ms": max(
                        0,
                        total_actual_cost - budget_reserved,
                    ),
                },
                "ledger_version": state["state_version"],
            }
        finally:
            connection.close()

    def _inspect_partition(
        self,
        round_id: str,
        pid: str,
        subscriber_id: str,
        subscription: SubscriptionSpec,
        config: GatewayConfig,
        planned_concurrency: int,
    ) -> dict[str, Any]:
        connection = self.storage.connect()
        try:
            state = self.storage.require_state(connection)
            partition = state["partitions"][pid]
            delivery = summarize_delivery(partition, subscriber_id)
            head = first_unresolved_offset(partition, subscriber_id)
            next_offset = int(partition["next_offset"])
            inflight = int(planned_concurrency)
            progress = connection.execute(
                """
                SELECT planned_batch, completed_attempts, estimated_cost_ms,
                       status, reason
                FROM round_partitions
                WHERE round_id = ? AND partition_id = ?
                """,
                (round_id, pid),
            ).fetchone()
            if progress is not None:
                planned_batch = int(progress["planned_batch"])
                completed_attempts = int(progress["completed_attempts"])
            else:
                planned_batch = int(subscription.pull_batch_size)
                completed_attempts = 0
            batch_remaining = max(0, planned_batch - completed_attempts)
            progress_status = progress["status"] if progress is not None else "planned"
            progress_reason = progress["reason"] if progress is not None else None
            progress_estimated = (
                int(progress["estimated_cost_ms"]) if progress is not None else 0
            )
            decision = decide_lock_mode(
                config,
                partition_id=pid,
                backlog_depth=backlog_depth(partition),
                inflight_count=inflight,
                deliverable_offset=head,
                logical_clock_ms=int(partition["clock_watermark"]),
            )
            if (
                batch_remaining == 0
                and not partition.get("inflight")
            ):
                return {
                    "no_work": True,
                    "reason": (
                        "round batch already completed "
                        f"({completed_attempts}/{planned_batch} attempts)"
                    ),
                    "estimated_cost_ms": 0,
                    "mode_decision": decision,
                    "batch_remaining": 0,
                    "progress_status": progress_status,
                    "progress_reason": progress_reason,
                    "progress_estimated_cost_ms": progress_estimated,
                }
            if head >= next_offset and not partition.get("inflight"):
                return {
                    "no_work": True,
                    "reason": "no unresolved offsets",
                    "estimated_cost_ms": 0,
                    "mode_decision": decision,
                    "batch_remaining": batch_remaining,
                    "progress_status": progress_status,
                    "progress_reason": progress_reason,
                    "progress_estimated_cost_ms": progress_estimated,
                }
            pending_count = max(1, next_offset - head)
            per_attempt = delivery_cost_ms(config, subscription) + config.lock_cost_ms
            estimate = max(
                per_attempt,
                min(batch_remaining, pending_count) * per_attempt,
            )
            return {
                "no_work": False,
                "reason": None,
                "estimated_cost_ms": int(estimate),
                "mode_decision": decision,
                "delivery_summary": delivery,
                "batch_remaining": batch_remaining,
                "progress_status": progress_status,
                "progress_reason": progress_reason,
                "progress_estimated_cost_ms": progress_estimated,
            }
        finally:
            connection.close()

    @staticmethod
    def _planned_work_concurrency(
        state: Mapping[str, Any],
        plan: list[str],
        subscriber_id: str,
        global_limit: int,
    ) -> int:
        work_partitions = 0
        for pid in plan:
            partition = state["partitions"][pid]
            if (
                partition.get("inflight")
                or first_unresolved_offset(partition, subscriber_id)
                < int(partition["next_offset"])
            ):
                work_partitions += 1
        return max(1, min(int(global_limit), work_partitions))

    def _run_partition(
        self,
        round_id: str,
        subscriber_id: str,
        subscription: SubscriptionSpec,
        config: GatewayConfig,
        pid: str,
        decision: dict[str, Any],
        batch_remaining: int,
    ) -> dict[str, Any]:
        lock_key = lock_key_for_decision(decision)
        with self.storage.partition_lock(f"{pid}:gate"), self.storage.partition_lock(
            lock_key
        ):
            recovery = self._recover_partition(pid)
            self._persist_mode(pid, decision)
            start_clock = self._partition_clock(pid)
            details: list[dict[str, Any]] = []
            failed_attempts = 0
            acked = 0
            dead = 0
            stop_reason = "batch_limit"

            for _ in range(batch_remaining):
                reservation = self._reserve_attempt(
                    round_id,
                    subscriber_id,
                    subscription,
                    config,
                    pid,
                )
                if reservation["kind"] == "no_work":
                    stop_reason = "no_work"
                    break
                if reservation["kind"] == "global_quota":
                    stop_reason = "global_delivery_quota_exhausted"
                    break
                if reservation["kind"] != "reserved":
                    stop_reason = reservation.get("reason", "reservation_blocked")
                    break

                reserved = reservation["reserved"]
                maybe_fault("deliver_after_reserve")
                success, failure_reason = simulate_delivery_outcome(
                    subscription,
                    reserved["entry"],
                    reserved["generation"],
                    config,
                )
                maybe_fault("deliver_before_commit")
                final = self._finalize_attempt(
                    subscriber_id,
                    subscription,
                    config,
                    pid,
                    reserved,
                    success=success,
                    failure_reason=failure_reason,
                )
                maybe_fault("deliver_after_commit")
                details.append(final)
                if final["outcome"] == "acked":
                    acked += 1
                elif final["outcome"] == "dead":
                    dead += 1
                    failed_attempts += 1
                else:
                    failed_attempts += 1

            end_clock = self._partition_clock(pid)
            connection = self.storage.connect()
            try:
                state = self.storage.require_state(connection)
                partition = state["partitions"][pid]
                summary = summarize_delivery(partition, subscriber_id)
                reconciliation = summary
                partition_view = partition_summary(partition)
            finally:
                connection.close()
            return {
                "partition_id": pid,
                "outcome": "STARTED_COMPLETED",
                "reason": stop_reason,
                "started": True,
                "estimated_cost_ms": 0,
                "actual_cost_ms": max(0, end_clock - start_clock),
                "deliveries_attempted": len(details),
                "acked": acked,
                "failed_attempts": failed_attempts,
                "dead_letters": dead,
                "recovered_inflight": recovery,
                "mode_decision": decision,
                "attempts": details,
                "reconciliation": reconciliation,
                "partition_status": partition_view,
            }

    def _persist_mode(self, pid: str, decision: dict[str, Any]) -> None:
        connection = self.storage.connect()
        try:
            with self.storage.transaction(connection):
                state = self.storage.require_state(connection)
                partition = state["partitions"][pid]
                partition["lock_mode"] = decision["actual_mode"]
                partition["mode_decision"] = decision
                self.storage.commit_state(connection, state)
        finally:
            connection.close()

    def _partition_clock(self, pid: str) -> int:
        connection = self.storage.connect()
        try:
            state = self.storage.require_state(connection)
            return int(state["partitions"][pid]["clock_watermark"])
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Atomic delivery attempts
    # ------------------------------------------------------------------
    def _reserve_attempt(
        self,
        round_id: str,
        subscriber_id: str,
        subscription: SubscriptionSpec,
        config: GatewayConfig,
        pid: str,
    ) -> dict[str, Any]:
        connection = self.storage.connect()
        try:
            with self.storage.transaction(connection):
                state = self.storage.require_state(connection)
                partition = state["partitions"][pid]
                if partition.get("inflight"):
                    raise GatewayError(
                        E_SEQUENCE_VIOLATION,
                        "partition already has an in-flight delivery",
                        {
                            "partition_id": pid,
                            "inflight": partition["inflight"],
                        },
                    )
                head = first_unresolved_offset(partition, subscriber_id)
                if head >= int(partition["next_offset"]):
                    return {"kind": "no_work"}
                subscription_state = partition["subscribers"].get(subscriber_id)
                if subscription_state is None:
                    raise GatewayError(
                        E_SUBSCRIBER_NOT_FOUND,
                        "subscriber has no delivery state for this partition",
                        {"subscriber_id": subscriber_id, "partition_id": pid},
                    )
                entry = subscription_state["deliveries"].get(str(head))
                if entry is None:
                    raise GatewayError(
                        E_EVENT_NOT_FOUND,
                        "delivery references an event absent from the partition log",
                        {
                            "partition_id": pid,
                            "offset": head,
                            "subscriber_id": subscriber_id,
                        },
                    )
                if entry["status"] == "in_flight":
                    raise GatewayError(
                        E_SEQUENCE_VIOLATION,
                        "delivery state is in-flight without a partition reservation",
                        {
                            "partition_id": pid,
                            "offset": head,
                            "subscriber_id": subscriber_id,
                        },
                    )
                if entry["status"] in TERMINAL_STATUSES:
                    raise GatewayError(
                        E_SEQUENCE_VIOLATION,
                        "terminal delivery state was selected for delivery",
                        {
                            "partition_id": pid,
                            "offset": head,
                            "status": entry["status"],
                        },
                    )
                if int(state["global"]["inflight_count"]) >= config.global_concurrency_limit:
                    return {"kind": "global_quota"}

                generation = int(entry["generation"])
                if generation <= 0:
                    raise GatewayError(
                        E_SEQUENCE_VIOLATION,
                        "delivery generation must be positive",
                        {
                            "partition_id": pid,
                            "offset": head,
                            "generation": generation,
                        },
                    )
                did = delivery_id(subscriber_id, pid, head, generation)
                attempts = entry.setdefault("attempts", [])
                existing_attempt = next(
                    (
                        attempt
                        for attempt in attempts
                        if attempt.get("delivery_id") == did
                    ),
                    None,
                )
                now = max(
                    int(partition["clock_watermark"]),
                    int(entry.get("next_attempt_clock_ms") or 0),
                    int(partition["log"][str(head)]["logical_clock_ms"]),
                )
                clock_before = int(partition["clock_watermark"])
                if existing_attempt is None:
                    attempt_clock = now
                    clock_after = (
                        attempt_clock
                        + delivery_cost_ms(config, subscription)
                        + config.lock_cost_ms
                    )
                    partition["clock_watermark"] = clock_after
                    attempt_record = {
                        "delivery_id": did,
                        "generation": generation,
                        "attempt_clock_ms": attempt_clock,
                        "clock_after_ms": clock_after,
                        "clock_before_ms": clock_before,
                        "status": "in_flight",
                    }
                    attempts.append(attempt_record)
                    entry["attempt_count"] = len(attempts)
                else:
                    attempt_clock = int(existing_attempt.get("attempt_clock_ms", now))
                    clock_after = int(
                        existing_attempt.get(
                            "clock_after_ms",
                            partition["clock_watermark"],
                        )
                    )
                    clock_before = int(
                        existing_attempt.get("clock_before_ms", clock_before)
                    )
                    existing_attempt["status"] = "in_flight"
                    attempt_record = existing_attempt

                entry["status"] = "in_flight"
                entry["delivery_id"] = did
                entry["next_attempt_clock_ms"] = attempt_clock
                reservation = {
                    "delivery_id": did,
                    "partition_id": pid,
                    "subscriber_id": subscriber_id,
                    "offset": head,
                    "event_id": entry["event_id"],
                    "generation": generation,
                    "attempt_clock_ms": attempt_clock,
                    "clock_after_ms": clock_after,
                    "clock_before_ms": clock_before,
                    "round_id": round_id,
                    "owner_pid": os.getpid(),
                    "idempotency_key": entry["idempotency_key"],
                }
                partition["inflight"] = reservation
                state["global"]["inflight_count"] = (
                    int(state["global"]["inflight_count"]) + 1
                )

                row = connection.execute(
                    "SELECT status FROM deliveries WHERE delivery_id = ?",
                    (did,),
                ).fetchone()
                if row is not None and row["status"] in {"acked", "dead"}:
                    raise GatewayError(
                        E_SEQUENCE_VIOLATION,
                        "generation already has a terminal delivery record",
                        {"delivery_id": did, "status": row["status"]},
                    )
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO deliveries(
                            delivery_id, round_id, partition_id, subscriber_id,
                            event_id, event_offset, generation, status,
                            reserved_clock_ms, completed_clock_ms,
                            failure_reason, owner_pid
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'in_flight', ?, NULL, NULL, ?)
                        """,
                        (
                            did,
                            round_id,
                            pid,
                            subscriber_id,
                            entry["event_id"],
                            head,
                            generation,
                            attempt_clock,
                            os.getpid(),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE deliveries
                        SET round_id = ?, status = 'in_flight',
                            reserved_clock_ms = ?, completed_clock_ms = NULL,
                            failure_reason = NULL, owner_pid = ?
                        WHERE delivery_id = ?
                        """,
                        (round_id, attempt_clock, os.getpid(), did),
                    )
                self.storage.commit_state(connection, state)
                return {
                    "kind": "reserved",
                    "reserved": {
                        "delivery_id": did,
                        "partition_id": pid,
                        "subscriber_id": subscriber_id,
                        "offset": head,
                        "generation": generation,
                        "entry": dict(entry),
                        "attempt_clock_ms": attempt_clock,
                        "clock_after_ms": clock_after,
                        "clock_before_ms": clock_before,
                        "round_id": round_id,
                    },
                }
        finally:
            connection.close()

    def _put_inbox(
        self,
        connection: sqlite3.Connection,
        *,
        subscriber_id: str,
        key: str,
        event_id: str,
        generation: int,
        delivery_id_value: str,
    ) -> None:
        row = connection.execute(
            """
            SELECT actual_count, duplicate_count
            FROM subscriber_inbox
            WHERE subscriber_id = ? AND idempotency_key = ?
            """,
            (subscriber_id, key),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO subscriber_inbox(
                    subscriber_id, idempotency_key, event_id,
                    first_generation, last_generation, actual_count,
                    duplicate_count, first_delivery_id, last_delivery_id
                ) VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?)
                """,
                (
                    subscriber_id,
                    key,
                    event_id,
                    generation,
                    generation,
                    delivery_id_value,
                    delivery_id_value,
                ),
            )
        else:
            connection.execute(
                """
                UPDATE subscriber_inbox
                SET last_generation = ?, duplicate_count = duplicate_count + 1,
                    last_delivery_id = ?
                WHERE subscriber_id = ? AND idempotency_key = ?
                """,
                (generation, delivery_id_value, subscriber_id, key),
            )

    def _finalize_attempt(
        self,
        subscriber_id: str,
        subscription: SubscriptionSpec,
        config: GatewayConfig,
        pid: str,
        reserved: dict[str, Any],
        *,
        success: bool,
        failure_reason: str | None,
    ) -> dict[str, Any]:
        connection = self.storage.connect()
        try:
            with self.storage.transaction(connection):
                state = self.storage.require_state(connection)
                partition = state["partitions"][pid]
                inflight = partition.get("inflight")
                if (
                    not inflight
                    or inflight.get("delivery_id") != reserved["delivery_id"]
                ):
                    raise GatewayError(
                        E_SEQUENCE_VIOLATION,
                        "delivery reservation no longer matches partition state",
                        {
                            "partition_id": pid,
                            "expected_delivery_id": reserved["delivery_id"],
                            "actual_inflight": inflight,
                        },
                    )
                entry = partition["subscribers"][subscriber_id]["deliveries"][
                    str(reserved["offset"])
                ]
                generation = int(reserved["generation"])
                if int(entry["generation"]) != generation:
                    raise GatewayError(
                        E_SEQUENCE_VIOLATION,
                        "delivery generation changed while in flight",
                        {
                            "partition_id": pid,
                            "offset": reserved["offset"],
                            "reserved_generation": generation,
                            "current_generation": entry["generation"],
                        },
                    )
                now = int(partition["clock_watermark"])
                self._put_inbox(
                    connection,
                    subscriber_id=subscriber_id,
                    key=entry["idempotency_key"],
                    event_id=entry["event_id"],
                    generation=generation,
                    delivery_id_value=reserved["delivery_id"],
                )
                attempts = entry.setdefault("attempts", [])
                attempt_record = next(
                    (
                        attempt
                        for attempt in attempts
                        if attempt.get("delivery_id") == reserved["delivery_id"]
                    ),
                    None,
                )
                if attempt_record is None:
                    attempt_record = {
                        "delivery_id": reserved["delivery_id"],
                        "generation": generation,
                        "attempt_clock_ms": reserved["attempt_clock_ms"],
                        "clock_after_ms": reserved["clock_after_ms"],
                    }
                    attempts.append(attempt_record)
                entry["attempt_count"] = len(attempts)

                if success:
                    final_outcome = "acked"
                    entry["status"] = "acked"
                    entry["acked_clock_ms"] = now
                    entry["last_outcome"] = "acked"
                    entry["dead_letter_reason"] = None
                    entry["delivery_id"] = reserved["delivery_id"]
                    attempt_record["status"] = "acked"
                    attempt_record["completed_clock_ms"] = now
                    attempt_record["failure_reason"] = None
                    connection.execute(
                        """
                        UPDATE deliveries
                        SET status = 'acked', completed_clock_ms = ?,
                            failure_reason = NULL
                        WHERE delivery_id = ?
                        """,
                        (now, reserved["delivery_id"]),
                    )
                else:
                    limit = max_generation(subscription)
                    entry["last_outcome"] = "failed"
                    attempt_record["status"] = "failed"
                    attempt_record["completed_clock_ms"] = now
                    attempt_record["failure_reason"] = failure_reason
                    if generation >= limit:
                        final_outcome = "dead"
                        entry["status"] = "dead"
                        entry["dead_letter_reason"] = (
                            f"retry_budget_exhausted:{failure_reason}"
                            f":generation={generation}:limit={limit}"
                        )
                        entry["dead_clock_ms"] = now
                        entry["delivery_id"] = reserved["delivery_id"]
                        attempt_record["status"] = "dead"
                        connection.execute(
                            """
                            UPDATE deliveries
                            SET status = 'dead', completed_clock_ms = ?,
                                failure_reason = ?
                            WHERE delivery_id = ?
                            """,
                            (now, failure_reason, reserved["delivery_id"]),
                        )
                    else:
                        final_outcome = "retry"
                        delay = backoff_ms(
                            subscriber_id,
                            entry["event_id"],
                            generation,
                            config,
                        )
                        entry["generation"] = generation + 1
                        entry["status"] = "pending"
                        entry["next_attempt_clock_ms"] = now + delay
                        entry["delivery_id"] = None
                        connection.execute(
                            """
                            UPDATE deliveries
                            SET status = 'failed', completed_clock_ms = ?,
                                failure_reason = ?
                            WHERE delivery_id = ?
                            """,
                            (now, failure_reason, reserved["delivery_id"]),
                        )

                partition["inflight"] = None
                state["global"]["inflight_count"] = max(
                    0,
                    int(state["global"]["inflight_count"]) - 1,
                )
                updated = connection.execute(
                    """
                    UPDATE round_partitions
                    SET completed_attempts = completed_attempts + 1,
                        status = CASE
                            WHEN completed_attempts + 1 >= planned_batch
                                THEN 'completed'
                            ELSE 'in_progress'
                        END,
                        updated_at = ?
                    WHERE round_id = ? AND partition_id = ?
                    """,
                    (_now_iso(), reserved["round_id"], pid),
                )
                if updated.rowcount == 0:
                    connection.execute(
                        """
                        INSERT INTO round_partitions(
                            round_id, partition_id, planned_batch,
                            completed_attempts, status, updated_at
                        ) VALUES (?, ?, ?, 1, ?, ?)
                        """,
                        (
                            reserved["round_id"],
                            pid,
                            subscription.pull_batch_size,
                            "completed"
                            if subscription.pull_batch_size <= 1
                            else "in_progress",
                            _now_iso(),
                        ),
                    )
                cost_delta = max(
                    0,
                    int(reserved["clock_after_ms"])
                    - int(reserved["clock_before_ms"]),
                )
                connection.execute(
                    """
                    UPDATE round_partitions
                    SET actual_cost_ms = actual_cost_ms + ?
                    WHERE round_id = ? AND partition_id = ?
                    """,
                    (cost_delta, reserved["round_id"], pid),
                )
                self.storage.commit_state(connection, state)
                return {
                    "delivery_id": reserved["delivery_id"],
                    "partition_id": pid,
                    "subscriber_id": subscriber_id,
                    "offset": reserved["offset"],
                    "generation": generation,
                    "outcome": final_outcome,
                    "failure_reason": failure_reason,
                    "next_generation": entry["generation"],
                    "next_attempt_clock_ms": entry["next_attempt_clock_ms"],
                    "clock_ms": now,
                }
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------
    def resume(
        self,
        *,
        subscriber_id: str | None = None,
        round_id: str | None = None,
        budget_override_ms: int | None = None,
    ) -> dict[str, Any]:
        recovered = self.recover_stale()
        if subscriber_id is None:
            return {
                "ok": True,
                "command": "resume",
                "recovered": recovered,
                "delivery": None,
            }
        delivery = self.deliver(
            subscriber_id,
            round_id=round_id or f"resume-{uuid.uuid4().hex}",
            budget_override_ms=budget_override_ms,
        )
        return {
            "ok": True,
            "command": "resume",
            "recovered": recovered,
            "delivery": delivery,
        }

    def recover_stale(self) -> list[dict[str, Any]]:
        connection = self.storage.connect()
        try:
            self.storage.ensure_schema(connection)
            state = self.storage.require_state(connection)
            pids = sorted(
                pid
                for pid, partition in state["partitions"].items()
                if partition.get("inflight")
            )
        finally:
            connection.close()
        recovered: list[dict[str, Any]] = []
        for pid in pids:
            with self.storage.partition_lock(f"{pid}:recovery"):
                item = self._recover_partition(pid)
                if item:
                    recovered.append(item)
        return recovered

    def _recover_partition(self, pid: str) -> dict[str, Any] | None:
        connection = self.storage.connect()
        try:
            with self.storage.transaction(connection):
                state = self.storage.require_state(connection)
                partition = state["partitions"][pid]
                inflight = partition.get("inflight")
                if not inflight:
                    return None
                owner_pid = int(inflight.get("owner_pid") or 0)
                if owner_pid != os.getpid() and self.storage.pid_alive(owner_pid):
                    return None
                subscriber_id = inflight["subscriber_id"]
                offset = int(inflight["offset"])
                generation = int(inflight["generation"])
                entry = partition["subscribers"][subscriber_id]["deliveries"][
                    str(offset)
                ]
                if entry["status"] != "in_flight":
                    raise GatewayError(
                        E_SEQUENCE_VIOLATION,
                        "in-flight reservation points to a non in-flight entry",
                        {
                            "partition_id": pid,
                            "offset": offset,
                            "status": entry["status"],
                        },
                    )
                for attempt in entry.get("attempts", []):
                    if attempt.get("delivery_id") == inflight["delivery_id"]:
                        attempt["status"] = "recovered_uncommitted"
                        break
                entry["status"] = "pending"
                entry["delivery_id"] = None
                entry["next_attempt_clock_ms"] = int(
                    partition["clock_watermark"]
                )
                partition["inflight"] = None
                state["global"]["inflight_count"] = max(
                    0,
                    int(state["global"]["inflight_count"]) - 1,
                )
                connection.execute(
                    """
                    UPDATE deliveries
                    SET status = 'recovered', failure_reason = 'process_terminated'
                    WHERE delivery_id = ?
                    """,
                    (inflight["delivery_id"],),
                )
                self.storage.commit_state(connection, state)
                return {
                    "partition_id": pid,
                    "delivery_id": inflight["delivery_id"],
                    "subscriber_id": subscriber_id,
                    "offset": offset,
                    "generation": generation,
                    "generation_unchanged": True,
                }
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Confirmation idempotency
    # ------------------------------------------------------------------
    def confirm(
        self,
        delivery_id_value: str,
        *,
        subscriber_id: str,
    ) -> dict[str, Any]:
        connection = self.storage.connect()
        try:
            with self.storage.transaction(connection):
                state = self.storage.require_state(connection)
                row = connection.execute(
                    """
                    SELECT delivery_id, partition_id, subscriber_id, event_id,
                           event_offset, generation, status
                    FROM deliveries WHERE delivery_id = ?
                    """,
                    (delivery_id_value,),
                ).fetchone()
                if row is None:
                    raise GatewayError(
                        E_DELIVERY_NOT_FOUND,
                        "confirmation references an unknown delivery",
                        {"delivery_id": delivery_id_value},
                    )
                if row["subscriber_id"] != subscriber_id:
                    raise GatewayError(
                        E_SUBSCRIBER_MISMATCH,
                        "confirmation belongs to a different subscriber",
                        {
                            "delivery_id": delivery_id_value,
                            "delivery_subscriber": row["subscriber_id"],
                            "confirmation_subscriber": subscriber_id,
                            "cross_subscriber_confirmation": True,
                        },
                    )
                pid = row["partition_id"]
                offset = int(row["event_offset"])
                generation = int(row["generation"])
                partition = state["partitions"][pid]
                entry = partition["subscribers"][subscriber_id]["deliveries"][
                    str(offset)
                ]
                if entry["status"] == "acked":
                    return {
                        "ok": True,
                        "command": "confirm",
                        "applied": False,
                        "duplicate_confirmation": True,
                        "late_confirmation": False,
                        "delivery_id": delivery_id_value,
                        "generation": generation,
                    }
                if entry["status"] == "dead":
                    return {
                        "ok": True,
                        "command": "confirm",
                        "applied": False,
                        "duplicate_confirmation": False,
                        "late_confirmation": True,
                        "reason": "delivery_is_dead",
                        "delivery_id": delivery_id_value,
                        "generation": generation,
                    }
                if (
                    entry["status"] != "in_flight"
                    or not partition.get("inflight")
                    or partition["inflight"].get("delivery_id")
                    != delivery_id_value
                ):
                    return {
                        "ok": True,
                        "command": "confirm",
                        "applied": False,
                        "duplicate_confirmation": False,
                        "late_confirmation": True,
                        "reason": "delivery_is_not_currently_in_flight",
                        "delivery_id": delivery_id_value,
                        "generation": generation,
                    }
                now = int(partition["clock_watermark"])
                self._put_inbox(
                    connection,
                    subscriber_id=subscriber_id,
                    key=entry["idempotency_key"],
                    event_id=entry["event_id"],
                    generation=generation,
                    delivery_id_value=delivery_id_value,
                )
                entry["status"] = "acked"
                entry["acked_clock_ms"] = now
                entry["last_outcome"] = "acked"
                entry["delivery_id"] = delivery_id_value
                for attempt in entry.get("attempts", []):
                    if attempt.get("delivery_id") == delivery_id_value:
                        attempt["status"] = "acked"
                        attempt["completed_clock_ms"] = now
                        break
                partition["inflight"] = None
                state["global"]["inflight_count"] = max(
                    0,
                    int(state["global"]["inflight_count"]) - 1,
                )
                connection.execute(
                    """
                    UPDATE deliveries
                    SET status = 'acked', completed_clock_ms = ?,
                        failure_reason = NULL
                    WHERE delivery_id = ?
                    """,
                    (now, delivery_id_value),
                )
                self.storage.commit_state(connection, state)
                return {
                    "ok": True,
                    "command": "confirm",
                    "applied": True,
                    "duplicate_confirmation": False,
                    "late_confirmation": False,
                    "delivery_id": delivery_id_value,
                    "generation": generation,
                }
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Status / verification
    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        connection = self.storage.connect()
        try:
            self.storage.ensure_schema(connection)
            state = self.storage.require_state(connection)
            topics, subscriptions, config = self._runtime_context(connection)
            topic_map = {topic.name: topic for topic in topics}
            partitions = []
            for pid in sorted(state["partitions"]):
                summary = partition_summary(state["partitions"][pid])
                summary["topic_config"] = topic_map[summary["topic"]].to_dict()
                partitions.append(summary)
            return {
                "ok": True,
                "command": "status",
                "ledger_version": state["state_version"],
                "ledger_fingerprint": logical_state_fingerprint(state),
                "schema_version": state["schema_version"],
                "global": {
                    "inflight_count": int(state["global"]["inflight_count"]),
                    "rounds_committed": int(state["global"]["rounds_committed"]),
                    "last_round_id": state["global"]["last_round_id"],
                    "topic_count": len(topics),
                    "subscription_count": len(subscriptions),
                },
                "partitions": partitions,
                "config": config.to_dict(),
            }
        finally:
            connection.close()

    def verify(self) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        failures: list[str] = []

        def add(name: str, ok: bool, details: Any) -> None:
            checks.append({"name": name, "ok": bool(ok), "details": details})
            if not ok:
                failures.append(name)

        connection = self.storage.connect()
        try:
            self.storage.ensure_schema(connection)
            try:
                state = self.storage.require_state(connection)
            except GatewayError as exc:
                add("ledger_readable", False, exc.to_dict())
                return {
                    "ok": False,
                    "command": "verify",
                    "checks": checks,
                    "failures": failures,
                }
            topics, subscriptions, config = self._runtime_context(connection)
            topic_map = {topic.name: topic for topic in topics}
            add(
                "ledger_checksum",
                True,
                {"ledger_version": state["state_version"]},
            )
            db_rows = connection.execute(
                """
                SELECT partition_id, offset_no, event_seq, event_id, topic,
                       partition_key, payload_digest, logical_clock_ms,
                       expected_delivery_generation
                FROM partition_log
                ORDER BY partition_id, offset_no
                """
            ).fetchall()
            unique_seq = connection.execute(
                "SELECT COUNT(*) AS c, COUNT(DISTINCT event_seq) AS u FROM partition_log"
            ).fetchone()
            unique_id = connection.execute(
                "SELECT COUNT(*) AS c, COUNT(DISTINCT event_id) AS u FROM partition_log"
            ).fetchone()
            add(
                "event_sequence_unique",
                int(unique_seq["c"]) == int(unique_seq["u"]),
                {"count": int(unique_seq["c"]), "unique": int(unique_seq["u"])},
            )
            add(
                "event_id_unique",
                int(unique_id["c"]) == int(unique_id["u"]),
                {"count": int(unique_id["c"]), "unique": int(unique_id["u"])},
            )

            rows_by_partition: dict[str, list[sqlite3.Row]] = {}
            for row in db_rows:
                rows_by_partition.setdefault(row["partition_id"], []).append(row)

            for pid in sorted(state["partitions"]):
                partition = state["partitions"][pid]
                topic = topic_map[partition["topic"]]
                log = partition["log"]
                expected_offsets = list(range(int(partition["next_offset"])))
                actual_offsets = sorted(int(value) for value in log)
                add(
                    f"{pid}:snapshot_offsets_contiguous",
                    actual_offsets == expected_offsets,
                    {
                        "expected": len(expected_offsets),
                        "actual": len(actual_offsets),
                        "first_gap": _first_gap(actual_offsets),
                    },
                )
                db_partition_rows = rows_by_partition.get(pid, [])
                db_offsets = [int(row["offset_no"]) for row in db_partition_rows]
                add(
                    f"{pid}:database_offsets_contiguous",
                    db_offsets == expected_offsets,
                    {
                        "expected": len(expected_offsets),
                        "actual": len(db_offsets),
                        "first_gap": _first_gap(db_offsets),
                    },
                )
                route_ok = True
                route_details: list[dict[str, Any]] = []
                for row in db_partition_rows:
                    routed = partition_id(
                        row["topic"],
                        route_partition(
                            row["topic"],
                            topic_map[row["topic"]].partition_count,
                            row["partition_key"],
                            topic_map[row["topic"]].partition_key_rule,
                        ),
                    )
                    if routed != pid:
                        route_ok = False
                        route_details.append(
                            {
                                "event_id": row["event_id"],
                                "expected_partition": routed,
                                "actual_partition": pid,
                            }
                        )
                add(
                    f"{pid}:deterministic_routing",
                    route_ok,
                    {"mismatches": route_details},
                )
                snapshot_match = len(log) == len(db_partition_rows)
                if snapshot_match:
                    for row in db_partition_rows:
                        snapshot_row = log.get(str(int(row["offset_no"])))
                        snapshot_match = snapshot_match and snapshot_row == {
                            "event_seq": int(row["event_seq"]),
                            "event_id": row["event_id"],
                            "partition_key": row["partition_key"],
                            "payload_digest": row["payload_digest"],
                            "logical_clock_ms": int(row["logical_clock_ms"]),
                            "expected_delivery_generation": int(
                                row["expected_delivery_generation"]
                            ),
                        }
                add(
                    f"{pid}:snapshot_matches_log_summary",
                    snapshot_match,
                    {"rows": len(db_partition_rows)},
                )
                max_clock = max(
                    (int(value["logical_clock_ms"]) for value in log.values()),
                    default=0,
                )
                add(
                    f"{pid}:logical_clock_monotonic",
                    int(partition["clock_watermark"]) >= max_clock,
                    {
                        "clock_watermark": int(partition["clock_watermark"]),
                        "max_event_clock": max_clock,
                    },
                )

                inflight_entries = 0
                for subscriber_id, sub_state in partition["subscribers"].items():
                    deliveries = sub_state["deliveries"]
                    keys = sorted(int(value) for value in deliveries)
                    add(
                        f"{pid}:{subscriber_id}:delivery_offsets_complete",
                        keys == expected_offsets,
                        {
                            "expected": len(expected_offsets),
                            "actual": len(keys),
                            "first_gap": _first_gap(keys),
                        },
                    )
                    strict = True
                    first_open: int | None = None
                    bad_offsets: list[dict[str, Any]] = []
                    generation_ok = True
                    generation_details: list[dict[str, Any]] = []
                    for offset in expected_offsets:
                        entry = deliveries.get(str(offset))
                        if entry is None:
                            strict = False
                            bad_offsets.append({"offset": offset, "reason": "missing"})
                            continue
                        status = entry["status"]
                        if status not in TERMINAL_STATUSES | UNRESOLVED_STATUSES:
                            strict = False
                            bad_offsets.append(
                                {"offset": offset, "reason": "bad_status", "status": status}
                            )
                        if first_open is not None and status != "pending":
                            strict = False
                            bad_offsets.append(
                                {
                                    "offset": offset,
                                    "reason": "overtakes_unresolved_prefix",
                                    "status": status,
                                    "first_open": first_open,
                                }
                            )
                        if first_open is None and status not in TERMINAL_STATUSES:
                            first_open = offset
                        generation = int(entry.get("generation", 0))
                        if generation <= 0:
                            generation_ok = False
                            generation_details.append(
                                {"offset": offset, "generation": generation}
                            )
                        attempt_ids = [
                            attempt.get("delivery_id")
                            for attempt in entry.get("attempts", [])
                        ]
                        if len(attempt_ids) != len(set(attempt_ids)):
                            generation_ok = False
                            generation_details.append(
                                {
                                    "offset": offset,
                                    "reason": "duplicate_attempt_delivery_id",
                                }
                            )
                        attempt_generations = [
                            int(attempt.get("generation", 0))
                            for attempt in entry.get("attempts", [])
                        ]
                        if attempt_generations != sorted(attempt_generations):
                            generation_ok = False
                            generation_details.append(
                                {
                                    "offset": offset,
                                    "reason": "generation_regressed",
                                    "attempt_generations": attempt_generations,
                                }
                            )
                        if status == "in_flight":
                            inflight_entries += 1
                            delivery_row = connection.execute(
                                "SELECT status FROM deliveries WHERE delivery_id = ?",
                                (entry.get("delivery_id"),),
                            ).fetchone()
                            if delivery_row is None or delivery_row["status"] != "in_flight":
                                strict = False
                                bad_offsets.append(
                                    {
                                        "offset": offset,
                                        "reason": "missing_inflight_delivery_row",
                                    }
                                )
                        if status == "dead" and not entry.get("dead_letter_reason"):
                            strict = False
                            bad_offsets.append(
                                {"offset": offset, "reason": "dead_without_reason"}
                            )
                        if status == "acked":
                            inbox = connection.execute(
                                """
                                SELECT actual_count FROM subscriber_inbox
                                WHERE subscriber_id = ? AND idempotency_key = ?
                                """,
                                (subscriber_id, entry["idempotency_key"]),
                            ).fetchone()
                            if inbox is None or int(inbox["actual_count"]) != 1:
                                strict = False
                                bad_offsets.append(
                                    {
                                        "offset": offset,
                                        "reason": "acked_without_single_actual_delivery",
                                    }
                                )
                    add(
                        f"{pid}:{subscriber_id}:strict_prefix_order",
                        strict,
                        {"violations": bad_offsets},
                    )
                    add(
                        f"{pid}:{subscriber_id}:generation_monotonic",
                        generation_ok,
                        {"violations": generation_details},
                    )
                    summary = summarize_delivery(partition, subscriber_id)
                    unexplained = [
                        gap
                        for gap in summary["gaps"]
                        if gap["reason"] in {"missing_delivery_state", "unexplained"}
                    ]
                    add(
                        f"{pid}:{subscriber_id}:gaps_explained",
                        not unexplained,
                        {"unexplained": unexplained, "gaps": summary["gaps"]},
                    )

                partition_inflight = 1 if partition.get("inflight") else 0
                add(
                    f"{pid}:max_one_inflight",
                    inflight_entries <= 1 and partition_inflight <= 1,
                    {
                        "delivery_entries": inflight_entries,
                        "partition_reservation": partition_inflight,
                    },
                )

            actual_global_inflight = sum(
                1
                for partition in state["partitions"].values()
                if partition.get("inflight")
            )
            add(
                "global_inflight_accounting",
                actual_global_inflight == int(state["global"]["inflight_count"]),
                {
                    "actual": actual_global_inflight,
                    "ledger": int(state["global"]["inflight_count"]),
                },
            )
            duplicate_delivery_rows = connection.execute(
                """
                SELECT COUNT(*) AS c FROM (
                    SELECT subscriber_id, partition_id, event_offset, generation
                    FROM deliveries
                    GROUP BY subscriber_id, partition_id, event_offset, generation
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()
            add(
                "delivery_generation_uniqueness",
                int(duplicate_delivery_rows["c"]) == 0,
                {"duplicate_groups": int(duplicate_delivery_rows["c"])},
            )
            inbox_bad = connection.execute(
                "SELECT COUNT(*) AS c FROM subscriber_inbox WHERE actual_count <> 1"
            ).fetchone()
            add(
                "subscriber_actual_delivery_once",
                int(inbox_bad["c"]) == 0,
                {"bad_rows": int(inbox_bad["c"])},
            )
            add(
                "metadata_consistency",
                self.storage.read_meta(connection).get("metadata_hash") is not None,
                {"metadata_hash": self.storage.read_meta(connection).get("metadata_hash")},
            )
            return {
                "ok": not failures,
                "command": "verify",
                "ledger_version": state["state_version"],
                "ledger_fingerprint": logical_state_fingerprint(state),
                "checks": checks,
                "failures": failures,
                "summary": {
                    "partitions": len(state["partitions"]),
                    "events": len(db_rows),
                    "topics": len(topics),
                    "subscriptions": len(subscriptions),
                },
            }
        finally:
            connection.close()

    def _record_round(
        self,
        connection: sqlite3.Connection,
        round_id: str,
        result: dict[str, Any],
    ) -> None:
        with self.storage.transaction(connection):
            existing = self._read_round(connection, round_id)
            if existing is not None:
                return
            state = self.storage.require_state(connection)
            result["ledger_version_before_round_record"] = state["state_version"]
            state["global"]["rounds_committed"] = (
                int(state["global"]["rounds_committed"]) + 1
            )
            state["global"]["last_round_id"] = round_id
            result["ledger_version"] = self.storage.commit_state(connection, state)
            connection.execute(
                """
                UPDATE round_state
                SET status = 'completed', updated_at = ?
                WHERE round_id = ?
                """,
                (_now_iso(), round_id),
            )
            connection.execute(
                "INSERT INTO rounds(round_id, result, created_at) VALUES (?, ?, ?)",
                (round_id, canonical_json(result), _now_iso()),
            )


def _first_gap(offsets: list[int]) -> int | None:
    for index, value in enumerate(offsets):
        if value != index:
            return index
    return None
