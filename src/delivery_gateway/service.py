from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .db import (
    advance_clock,
    bump_queue_version,
    clock_watermark,
    connect,
    get_meta,
    initialize_schema,
    read_snapshot,
    require_existing,
    set_meta,
    transaction,
    write_lock,
)
from .errors import ErrorCode, GatewayError
from .models import (
    TOKEN_SCALE,
    ClockConfig,
    ConsumerBundle,
    ConsumerConfig,
    FaultBundle,
    GeneratedMessage,
    QuotaConfig,
    TopicBundle,
    TopicConfig,
    backoff_delay_ms,
    canonical_json,
    combine_fingerprint,
    generate_messages,
    load_json,
    parse_clock,
    parse_consumers,
    parse_faults,
    parse_quota,
    parse_topics,
    rate_text,
    wait_ms_for_token,
)


@dataclass(frozen=True)
class RuntimeConfig:
    topics: tuple[TopicConfig, ...]
    consumers: tuple[ConsumerConfig, ...]
    quota: QuotaConfig
    clock: ClockConfig
    consumer_by_id: dict[str, ConsumerConfig]
    topic_by_name: dict[str, TopicConfig]


@dataclass(frozen=True)
class FaultPlan:
    fault_id: int | None
    action: str
    duration_ms: int
    planned_outcome: str
    planned_reason: str


def _read_all_config(paths: dict[str, str]) -> tuple[
    TopicBundle, ConsumerBundle, QuotaConfig, ClockConfig, FaultBundle, tuple[GeneratedMessage, ...], str
]:
    topics = parse_topics(load_json(paths["topics"], "topics"))
    consumers = parse_consumers(load_json(paths["consumers"], "consumers"), topics)
    quota = parse_quota(load_json(paths["quota"], "quota"))
    clock = parse_clock(load_json(paths["clock"], "clock"))
    messages = generate_messages(topics)
    faults = parse_faults(load_json(paths["faults"], "faults"), consumers, messages)
    fingerprint = combine_fingerprint(topics, consumers, quota, clock, faults)
    return topics, consumers, quota, clock, faults, messages, fingerprint


def _existing_config(path: str | Path) -> tuple[int, str] | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        conn = connect(p, readonly=True)
    except GatewayError:
        return None
    try:
        try:
            version = get_meta(conn, "config_version")
            fingerprint = get_meta(conn, "config_fingerprint")
        except sqlite3.Error:
            return None
        if version is None:
            return None
        return int(version), fingerprint or ""
    finally:
        conn.close()


def load_gateway(
    db_path: str | Path,
    *,
    topics_path: str | Path,
    consumers_path: str | Path,
    quota_path: str | Path,
    clock_path: str | Path,
    faults_path: str | Path,
) -> dict[str, Any]:
    topics, consumers, quota, clock, faults, messages, fingerprint = _read_all_config(
        {
            "topics": str(topics_path),
            "consumers": str(consumers_path),
            "quota": str(quota_path),
            "clock": str(clock_path),
            "faults": str(faults_path),
        }
    )

    existing = _existing_config(db_path)
    if existing is not None:
        old_version, old_fingerprint = existing
        if quota.config_version < old_version:
            raise GatewayError(
                ErrorCode.CONFIG_VERSION_ROLLBACK,
                "quota config version rollback is not allowed",
                {
                    "provided_version": quota.config_version,
                    "stored_version": old_version,
                    "path": str(db_path),
                },
            )
        if quota.config_version == old_version and fingerprint != old_fingerprint:
            raise GatewayError(
                ErrorCode.CONFIG_VERSION_CONFLICT,
                "same config version has different content",
                {
                    "config_version": quota.config_version,
                    "provided_fingerprint": fingerprint,
                    "stored_fingerprint": old_fingerprint,
                },
            )

    with write_lock(db_path):
        conn = connect(db_path, readonly=False)
        try:
            initialize_schema(conn)
            with transaction(conn):
                version = get_meta(conn, "config_version")
                if version is None:
                    _insert_fresh_config(conn, topics, consumers, quota, clock, faults, messages, fingerprint)
                    inserted = len(messages)
                    replaced = False
                else:
                    old_version = int(version)
                    old_fingerprint = get_meta(conn, "config_fingerprint") or ""
                    if quota.config_version < old_version:
                        raise GatewayError(
                            ErrorCode.CONFIG_VERSION_ROLLBACK,
                            "quota config version rollback is not allowed",
                            {"provided_version": quota.config_version, "stored_version": old_version},
                        )
                    if quota.config_version == old_version:
                        if fingerprint != old_fingerprint:
                            raise GatewayError(
                                ErrorCode.CONFIG_VERSION_CONFLICT,
                                "same config version has different content",
                                {
                                    "config_version": quota.config_version,
                                    "provided_fingerprint": fingerprint,
                                    "stored_fingerprint": old_fingerprint,
                                },
                            )
                        inserted = 0
                        replaced = False
                    else:
                        count = conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
                        if count:
                            raise GatewayError(
                                ErrorCode.CONFIG_VERSION_FORWARD_UNSUPPORTED,
                                "config version upgrade after delivery is not supported",
                                {
                                    "provided_version": quota.config_version,
                                    "stored_version": old_version,
                                    "deliveries": count,
                                },
                            )
                        _clear_config_state(conn)
                        _insert_fresh_config(conn, topics, consumers, quota, clock, faults, messages, fingerprint)
                        inserted = len(messages)
                        replaced = True
                topic_counts = {
                    row["topic"]: int(row["n"])
                    for row in conn.execute(
                        "SELECT topic, COUNT(*) AS n FROM messages GROUP BY topic ORDER BY topic"
                    ).fetchall()
                }
                message_count = int(conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"])
                queue_version = int(get_meta(conn, "queue_version") or "0")
                watermark = clock_watermark(conn)
        finally:
            conn.close()

    return {
        "ok": True,
        "command": "load",
        "config_version": quota.config_version,
        "config_fingerprint": fingerprint,
        "messages_inserted": inserted,
        "messages_total": message_count,
        "topic_messages": topic_counts,
        "clock_watermark_ms": watermark,
        "queue_version": queue_version,
        "config_replaced": replaced,
    }


def _clear_config_state(conn: sqlite3.Connection) -> None:
    for table in (
        "request_outcomes",
        "abandonments",
        "retry_events",
        "dead_letters",
        "token_waits",
        "ledger_events",
        "deliveries",
        "delivery_requests",
        "fault_events",
        "messages",
        "buckets",
        "bucket_baselines",
        "consumer_usage",
        "consumers",
        "topics",
        "quota_snapshot",
        "clock_config",
    ):
        conn.execute(f"DELETE FROM {table}")
    set_meta(conn, "queue_version", 0)


def _insert_fresh_config(
    conn: sqlite3.Connection,
    topics: TopicBundle,
    consumers: ConsumerBundle,
    quota: QuotaConfig,
    clock: ClockConfig,
    faults: FaultBundle,
    messages: tuple[GeneratedMessage, ...],
    fingerprint: str,
) -> None:
    set_meta(conn, "queue_version", 0)
    set_meta(conn, "clock_watermark_ms", clock.initial_now_ms)
    set_meta(conn, "initial_clock_ms", clock.initial_now_ms)
    set_meta(conn, "config_version", quota.config_version)
    set_meta(conn, "config_fingerprint", fingerprint)
    set_meta(conn, "token_scale", TOKEN_SCALE)
    for topic in topics.topics:
        conn.execute(
            "INSERT INTO topics(name, partitions, partition_key_rule, backlog_limit, global_seed) "
            "VALUES(?, ?, ?, ?, ?)",
            (
                topic.name,
                topic.partitions,
                topic.partition_key_rule,
                topic.backlog_limit,
                topics.global_seed,
            ),
        )
    for consumer in consumers.consumers:
        conn.execute(
            "INSERT INTO consumers("
            "consumer_id, topic, request_size, rate_limit_scaled, rate_limit_text, delivery_timeout_ms"
            ") VALUES(?, ?, ?, ?, ?, ?)",
            (
                consumer.consumer_id,
                consumer.topic,
                consumer.request_size,
                consumer.rate_limit_scaled,
                rate_text(consumer.rate_limit_scaled),
                consumer.delivery_timeout_ms,
            ),
        )
        conn.execute("INSERT INTO consumer_usage(consumer_id) VALUES(?)", (consumer.consumer_id,))
    conn.execute(
        "INSERT INTO quota_snapshot("
        "singleton, config_version, global_capacity, global_refill_scaled, per_consumer_capacity, "
        "round_budget_ms, delivery_timeout_ms, max_delivery_generation, retry_backoff_base_ms, "
        "retry_backoff_max_ms, attempt_duration_ms"
        ") VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            quota.config_version,
            quota.global_capacity,
            quota.global_refill_scaled,
            quota.per_consumer_capacity,
            quota.round_budget_ms,
            quota.delivery_timeout_ms,
            quota.max_delivery_generation,
            quota.retry_backoff_base_ms,
            quota.retry_backoff_max_ms,
            quota.attempt_duration_ms,
        ),
    )
    conn.execute(
        "INSERT INTO clock_config(singleton, initial_now_ms, regression_tolerance_ms) VALUES(1, ?, ?)",
        (clock.initial_now_ms, clock.regression_tolerance_ms),
    )
    global_capacity = quota.global_capacity * TOKEN_SCALE
    _insert_bucket(
        conn,
        bucket_id="global",
        scope="global",
        owner="global",
        capacity_scaled=global_capacity,
        balance_scaled=global_capacity,
        last_refill_ms=clock.initial_now_ms,
        rate_scaled=quota.global_refill_scaled,
    )
    consumer_capacity = quota.per_consumer_capacity * TOKEN_SCALE
    for consumer in consumers.consumers:
        _insert_bucket(
            conn,
            bucket_id=f"consumer:{consumer.consumer_id}",
            scope="consumer",
            owner=consumer.consumer_id,
            capacity_scaled=consumer_capacity,
            balance_scaled=consumer_capacity,
            last_refill_ms=clock.initial_now_ms,
            rate_scaled=consumer.rate_limit_scaled,
        )
    conn.executemany(
        "INSERT INTO messages("
        "message_id, topic, partition, partition_key, seq_in_partition, payload, generation, "
        "available_at_ms, state"
        ") VALUES(?, ?, ?, ?, ?, ?, 0, ?, 'ready')",
        [
            (
                message.message_id,
                message.topic,
                message.partition,
                message.partition_key,
                message.seq_in_partition,
                message.payload,
                clock.initial_now_ms,
            )
            for message in messages
        ],
    )
    conn.executemany(
        "INSERT INTO fault_events("
        "message_id, consumer_id, generation, action, duration_ms, note"
        ") VALUES(?, ?, ?, ?, ?, ?)",
        [
            (
                fault.message_id,
                fault.consumer_id,
                fault.generation,
                fault.action,
                fault.duration_ms,
                fault.note,
            )
            for fault in faults.faults
        ],
    )


def _insert_bucket(
    conn: sqlite3.Connection,
    *,
    bucket_id: str,
    scope: str,
    owner: str,
    capacity_scaled: int,
    balance_scaled: int,
    last_refill_ms: int,
    rate_scaled: int,
) -> None:
    conn.execute(
        "INSERT INTO bucket_baselines("
        "bucket_id, scope, owner, capacity_scaled, initial_balance_scaled, initial_last_refill_ms, "
        "refill_rate_scaled"
        ") VALUES(?, ?, ?, ?, ?, ?, ?)",
        (bucket_id, scope, owner, capacity_scaled, balance_scaled, last_refill_ms, rate_scaled),
    )
    conn.execute(
        "INSERT INTO buckets("
        "bucket_id, scope, owner, capacity_scaled, balance_scaled, last_refill_ms, refill_rate_scaled"
        ") VALUES(?, ?, ?, ?, ?, ?, ?)",
        (bucket_id, scope, owner, capacity_scaled, balance_scaled, last_refill_ms, rate_scaled),
    )


def load_runtime_config(conn: sqlite3.Connection) -> RuntimeConfig:
    try:
        quota_row = conn.execute("SELECT * FROM quota_snapshot WHERE singleton=1").fetchone()
        clock_row = conn.execute("SELECT * FROM clock_config WHERE singleton=1").fetchone()
        if quota_row is None or clock_row is None:
            raise GatewayError(ErrorCode.DATABASE, "database is not initialized", {})
        topic_rows = conn.execute("SELECT * FROM topics ORDER BY name").fetchall()
        consumer_rows = conn.execute("SELECT * FROM consumers ORDER BY consumer_id").fetchall()
    except sqlite3.Error as exc:
        raise GatewayError(ErrorCode.DATABASE, "cannot read configuration state", {"error": str(exc)}) from exc
    topics = tuple(
        TopicConfig(
            name=str(row["name"]),
            partitions=int(row["partitions"]),
            partition_key_rule=str(row["partition_key_rule"]),
            backlog_limit=int(row["backlog_limit"]),
        )
        for row in topic_rows
    )
    consumers = tuple(
        ConsumerConfig(
            consumer_id=str(row["consumer_id"]),
            topic=str(row["topic"]),
            request_size=int(row["request_size"]),
            rate_limit_per_sec=_decimal_from_scaled(int(row["rate_limit_scaled"])),
            rate_limit_scaled=int(row["rate_limit_scaled"]),
            delivery_timeout_ms=int(row["delivery_timeout_ms"]),
        )
        for row in consumer_rows
    )
    quota = QuotaConfig(
        config_version=int(quota_row["config_version"]),
        global_capacity=int(quota_row["global_capacity"]),
        global_refill_rate_per_sec=_decimal_from_scaled(int(quota_row["global_refill_scaled"])),
        global_refill_scaled=int(quota_row["global_refill_scaled"]),
        per_consumer_capacity=int(quota_row["per_consumer_capacity"]),
        round_budget_ms=int(quota_row["round_budget_ms"]),
        delivery_timeout_ms=int(quota_row["delivery_timeout_ms"]),
        max_delivery_generation=int(quota_row["max_delivery_generation"]),
        retry_backoff_base_ms=int(quota_row["retry_backoff_base_ms"]),
        retry_backoff_max_ms=int(quota_row["retry_backoff_max_ms"]),
        attempt_duration_ms=int(quota_row["attempt_duration_ms"]),
    )
    clock = ClockConfig(
        initial_now_ms=int(clock_row["initial_now_ms"]),
        regression_tolerance_ms=int(clock_row["regression_tolerance_ms"]),
    )
    return RuntimeConfig(
        topics=topics,
        consumers=consumers,
        quota=quota,
        clock=clock,
        consumer_by_id={consumer.consumer_id: consumer for consumer in consumers},
        topic_by_name={topic.name: topic for topic in topics},
    )


def _decimal_from_scaled(value: int):
    from decimal import Decimal

    return Decimal(value) / Decimal(TOKEN_SCALE)


def _effective_clock(conn: sqlite3.Connection, config: RuntimeConfig, supplied_ms: int | None) -> int:
    watermark = clock_watermark(conn)
    if supplied_ms is None:
        return watermark
    if supplied_ms < watermark - config.clock.regression_tolerance_ms:
        raise GatewayError(
            ErrorCode.CLOCK_REGRESSION,
            "logical clock regressed beyond tolerance",
            {
                "supplied_now_ms": supplied_ms,
                "watermark_ms": watermark,
                "tolerance_ms": config.clock.regression_tolerance_ms,
                "regression_ms": watermark - supplied_ms,
            },
        )
    return max(supplied_ms, watermark)


def _maybe_crash(point: str) -> None:
    if os.environ.get("GATEWAY_CRASH_POINT") == point:
        os.kill(os.getpid(), signal.SIGKILL)


def _request_payload_hash(
    *,
    consumer_id: str,
    now_ms: int,
    limit: int,
    message_id: str | None,
    command: str,
) -> str:
    payload = {
        "command": command,
        "consumer_id": consumer_id,
        "now_ms": now_ms,
        "limit": limit,
        "message_id": message_id,
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def default_request_id(consumer_id: str, now_ms: int, limit: int, message_id: str | None) -> str:
    suffix = hashlib.sha256(
        canonical_json(
            {"consumer_id": consumer_id, "now_ms": now_ms, "limit": limit, "message_id": message_id}
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"auto-{consumer_id}-{now_ms}-{suffix}"


class DeliveryEngine:
    def __init__(self, conn: sqlite3.Connection, config: RuntimeConfig):
        self.conn = conn
        self.config = config

    def deliver(
        self,
        *,
        consumer_id: str,
        request_id: str,
        now_ms: int | None,
        limit: int | None = None,
        message_id: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        if not request_id or request_id != request_id.strip():
            raise GatewayError(ErrorCode.REQUEST_ID_INVALID, "request_id must be a non-empty trimmed string", {})
        consumer = self.config.consumer_by_id.get(consumer_id)
        if consumer is None:
            raise GatewayError(
                ErrorCode.CONSUMER_NOT_FOUND,
                "consumer lease is missing",
                {"consumer_id": consumer_id},
            )
        if topic is not None and topic not in self.config.topic_by_name:
            raise GatewayError(ErrorCode.TOPIC_NOT_FOUND, "topic does not exist", {"topic": topic})
        if topic is not None and topic != consumer.topic:
            raise GatewayError(
                ErrorCode.CONSUMER_TOPIC_MISMATCH,
                "consumer is not subscribed to the requested topic",
                {"consumer_id": consumer_id, "topic": topic, "subscribed_topic": consumer.topic},
            )
        effective_limit = consumer.request_size if limit is None else limit
        if (
            isinstance(effective_limit, bool)
            or not isinstance(effective_limit, int)
            or effective_limit <= 0
            or effective_limit > consumer.request_size
        ):
            raise GatewayError(
                ErrorCode.REQUEST_LIMIT_INVALID,
                "request limit must be a positive integer no greater than the lease request_size",
                {
                    "limit": effective_limit,
                    "request_size": consumer.request_size,
                },
            )
        if message_id is not None:
            if not isinstance(message_id, str) or not message_id:
                raise GatewayError(ErrorCode.REQUEST_ID_INVALID, "message_id must be non-empty", {})
            message_row = self.conn.execute(
                "SELECT topic FROM messages WHERE message_id=?", (message_id,)
            ).fetchone()
            if message_row is None:
                raise GatewayError(
                    ErrorCode.MESSAGE_NOT_FOUND,
                    "delivery references a missing message",
                    {"message_id": message_id},
                )
            if str(message_row["topic"]) != consumer.topic:
                raise GatewayError(
                    ErrorCode.CONSUMER_TOPIC_MISMATCH,
                    "message does not belong to the consumer subscription",
                    {"consumer_id": consumer_id, "message_id": message_id, "topic": str(message_row["topic"])},
                )
        payload_hash = _request_payload_hash(
            consumer_id=consumer_id,
            now_ms=0 if now_ms is None else now_ms,
            limit=effective_limit,
            message_id=message_id,
            command="deliver",
        )
        with transaction(self.conn):
            existing = self._get_request(request_id)
            if existing is not None:
                if str(existing["payload_hash"]) != payload_hash:
                    raise GatewayError(
                        ErrorCode.REQUEST_CONFLICT,
                        "request_id was already used with a different payload",
                        {
                            "request_id": request_id,
                            "stored_payload_hash": str(existing["payload_hash"]),
                            "provided_payload_hash": payload_hash,
                        },
                    )
                if str(existing["status"]) == "completed":
                    return json.loads(str(existing["result_json"]))
                request_row = existing
            else:
                effective_now = _effective_clock(self.conn, self.config, now_ms)
                start_cursor = effective_now
                deadline = start_cursor + self.config.quota.round_budget_ms
                self.conn.execute(
                    "INSERT INTO delivery_requests("
                    "request_id, command, consumer_id, topic, now_ms, start_cursor_ms, deadline_ms, "
                    "limit_count, message_id, payload_hash, status, cursor_ms, outcome_count, "
                    "created_ms, updated_ms"
                    ") VALUES(?, 'deliver', ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, 0, ?, ?)",
                    (
                        request_id,
                        consumer_id,
                        consumer.topic,
                        0 if now_ms is None else now_ms,
                        start_cursor,
                        deadline,
                        effective_limit,
                        message_id,
                        payload_hash,
                        start_cursor,
                        effective_now,
                        effective_now,
                    ),
                )
                advance_clock(self.conn, effective_now)
                bump_queue_version(self.conn)
                request_row = self._get_request(request_id)
        assert request_row is not None
        return self.process_request(request_id)

    def process_request(self, request_id: str) -> dict[str, Any]:
        request = self._get_request(request_id)
        if request is None:
            raise GatewayError(ErrorCode.INTERNAL, "request journal is missing", {"request_id": request_id})
        if str(request["status"]) == "completed":
            return json.loads(str(request["result_json"]))

        recovered = self._recover_started_for_request(request_id)
        request = self._get_request(request_id)
        assert request is not None
        if str(request["status"]) == "completed":
            return json.loads(str(request["result_json"]))

        consumer = self.config.consumer_by_id[str(request["consumer_id"])]
        cursor = int(request["cursor_ms"])
        deadline = int(request["deadline_ms"])
        limit_count = int(request["limit_count"])
        finish_reason = request["finish_reason"]
        while True:
            request = self._get_request(request_id)
            assert request is not None
            if str(request["status"]) == "completed":
                return json.loads(str(request["result_json"]))
            cursor = int(request["cursor_ms"])
            deadline = int(request["deadline_ms"])
            outcome_count = int(request["outcome_count"])
            finish_reason = request["finish_reason"]
            if finish_reason:
                return self._finish_request(request_id, str(finish_reason))
            if outcome_count >= limit_count:
                return self._finish_request(request_id, "request_limit")
            if cursor > deadline:
                return self._finish_request(request_id, "round_budget")

            message = self._select_candidate(request, cursor)
            if message is None:
                return self._finish_request(request_id, "idle")

            parsed = self._specific_message_status(request, message, cursor)
            if parsed is not None:
                kind, status, reason, detail = parsed
                self._record_not_started(
                    request_id=request_id,
                    message=message,
                    at_ms=cursor,
                    kind=kind,
                    status=status,
                    reason=reason,
                    detail=detail,
                    finish_reason=reason,
                )
                return self._finish_request(request_id, reason)

            if int(message["generation"]) >= self.config.quota.max_delivery_generation:
                self._dead_letter_from_limit(request, message, cursor)
                return self._finish_request(request_id, "generation_limit")

            plan = self._plan_fault(message, consumer, cursor)
            if plan.duration_ms > consumer.delivery_timeout_ms:
                self._abandon_before_start(
                    request=request,
                    message=message,
                    at_ms=cursor,
                    reason="delivery_timeout_budget_exceeded",
                    duration_ms=plan.duration_ms,
                )
                continue

            projected = self._project_refill(message, consumer, cursor)
            global_wait = wait_ms_for_token(
                int(projected["global_balance"]), int(projected["global_rate"])
            )
            consumer_wait = wait_ms_for_token(
                int(projected["consumer_balance"]), int(projected["consumer_rate"])
            )
            needed_wait = max(global_wait, consumer_wait)
            if needed_wait > 0:
                if cursor + needed_wait > deadline:
                    self._record_wait_and_not_started(
                        request=request,
                        message=message,
                        cursor=cursor,
                        projected=projected,
                        needed_wait=needed_wait,
                        reason="token_wait_exceeds_round_budget",
                        finish_reason="round_budget",
                    )
                    return self._finish_request(request_id, "round_budget")
                if needed_wait + plan.duration_ms > consumer.delivery_timeout_ms:
                    self._record_wait_and_not_started(
                        request=request,
                        message=message,
                        cursor=cursor,
                        projected=projected,
                        needed_wait=needed_wait,
                        reason="token_wait_exceeds_delivery_timeout",
                        finish_reason="delivery_timeout",
                    )
                    return self._finish_request(request_id, "delivery_timeout")
                self._advance_over_token_wait(
                    request=request,
                    message=message,
                    cursor=cursor,
                    projected=projected,
                    needed_wait=needed_wait,
                )
                continue

            if cursor + plan.duration_ms > deadline:
                self._record_not_started(
                    request_id=request_id,
                    message=message,
                    at_ms=cursor,
                    kind="not_started",
                    status="not_started",
                    reason="round_budget_exhausted",
                    detail={"needed_duration_ms": plan.duration_ms, "remaining_budget_ms": deadline - cursor},
                    finish_reason="round_budget",
                )
                return self._finish_request(request_id, "round_budget")

            delivery_id = self._claim_attempt(
                request=request,
                message=message,
                consumer=consumer,
                cursor=cursor,
                plan=plan,
            )
            if delivery_id is None:
                continue
            if plan.action == "crash_after_claim":
                os.kill(os.getpid(), signal.SIGKILL)
            self._complete_delivery(delivery_id)

    def resume(self, *, now_ms: int | None) -> dict[str, Any]:
        effective_now = _effective_clock(self.conn, self.config, now_ms)
        with transaction(self.conn):
            advance_clock(self.conn, effective_now)
        started_rows = self.conn.execute(
            "SELECT delivery_id FROM deliveries WHERE status='started' "
            "ORDER BY started_at_ms, delivery_id"
        ).fetchall()
        resumed_deliveries = 0
        for row in started_rows:
            self._complete_delivery(str(row["delivery_id"]))
            resumed_deliveries += 1
        running_rows = self.conn.execute(
            "SELECT request_id FROM delivery_requests WHERE status='running' ORDER BY request_id"
        ).fetchall()
        completed_requests = 0
        for row in running_rows:
            request_id = str(row["request_id"])
            self.process_request(request_id)
            completed_requests += 1
        return {
            "ok": True,
            "command": "resume",
            "resumed_deliveries": resumed_deliveries,
            "completed_requests": completed_requests,
            "clock_watermark_ms": clock_watermark(self.conn),
            "queue_version": int(get_meta(self.conn, "queue_version") or "0"),
        }

    def confirm(
        self,
        *,
        delivery_id: str,
        consumer_id: str | None,
        now_ms: int | None,
    ) -> dict[str, Any]:
        effective_now = _effective_clock(self.conn, self.config, now_ms)
        delivery = self.conn.execute(
            "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if delivery is None:
            raise GatewayError(
                ErrorCode.DELIVERY_NOT_FOUND,
                "confirmation references a missing delivery",
                {"delivery_id": delivery_id},
            )
        actual_consumer = str(delivery["consumer_id"])
        if consumer_id is not None and consumer_id != actual_consumer:
            return {
                "ok": True,
                "command": "confirm",
                "delivery_id": delivery_id,
                "confirmed": False,
                "idempotent": True,
                "ignored_reason": "cross_consumer_confirmation",
                "consumer_id": consumer_id,
                "delivery_consumer_id": actual_consumer,
                "generation": int(delivery["generation"]),
            }
        message_row = self.conn.execute(
            "SELECT * FROM messages WHERE message_id=?", (str(delivery["message_id"]),)
        ).fetchone()
        if message_row is None:
            raise GatewayError(
                ErrorCode.MESSAGE_NOT_FOUND,
                "delivery references a missing message",
                {"delivery_id": delivery_id, "message_id": str(delivery["message_id"])},
            )
        if int(message_row["generation"]) != int(delivery["generation"]):
            return {
                "ok": True,
                "command": "confirm",
                "delivery_id": delivery_id,
                "confirmed": False,
                "idempotent": True,
                "ignored_reason": "late_confirmation_stale_generation",
                "generation": int(delivery["generation"]),
                "current_generation": int(message_row["generation"]),
            }
        if str(delivery["status"]) != "delivered":
            return {
                "ok": True,
                "command": "confirm",
                "delivery_id": delivery_id,
                "confirmed": False,
                "idempotent": True,
                "ignored_reason": "late_confirmation_not_delivered",
                "delivery_status": str(delivery["status"]),
                "generation": int(delivery["generation"]),
            }
        if str(message_row["state"]) == "dead_letter":
            return {
                "ok": True,
                "command": "confirm",
                "delivery_id": delivery_id,
                "confirmed": False,
                "idempotent": True,
                "ignored_reason": "dead_letter_tombstone_preserved",
                "generation": int(delivery["generation"]),
            }
        if str(message_row["state"]) == "confirmed":
            return {
                "ok": True,
                "command": "confirm",
                "delivery_id": delivery_id,
                "confirmed": True,
                "idempotent": True,
                "ignored_reason": "already_confirmed",
                "confirmed_by": message_row["confirmed_by"],
                "confirmed_at_ms": int(message_row["confirmed_at_ms"]),
                "generation": int(message_row["generation"]),
            }
        with transaction(self.conn):
            delivery = self.conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            message_row = self.conn.execute(
                "SELECT * FROM messages WHERE message_id=?", (str(delivery["message_id"]),)
            ).fetchone()
            if delivery is None or message_row is None:
                raise GatewayError(ErrorCode.INTERNAL, "confirmation state disappeared", {})
            if str(message_row["state"]) == "dead_letter":
                return {
                    "ok": True,
                    "command": "confirm",
                    "delivery_id": delivery_id,
                    "confirmed": False,
                    "idempotent": True,
                    "ignored_reason": "dead_letter_tombstone_preserved",
                    "generation": int(delivery["generation"]),
                }
            if str(message_row["state"]) == "confirmed":
                return {
                    "ok": True,
                    "command": "confirm",
                    "delivery_id": delivery_id,
                    "confirmed": True,
                    "idempotent": True,
                    "ignored_reason": "already_confirmed",
                    "confirmed_by": message_row["confirmed_by"],
                    "confirmed_at_ms": int(message_row["confirmed_at_ms"]),
                    "generation": int(message_row["generation"]),
                }
            if str(delivery["status"]) != "delivered":
                return {
                    "ok": True,
                    "command": "confirm",
                    "delivery_id": delivery_id,
                    "confirmed": False,
                    "idempotent": True,
                    "ignored_reason": "late_confirmation_not_delivered",
                    "delivery_status": str(delivery["status"]),
                    "generation": int(delivery["generation"]),
                }
            self.conn.execute(
                "UPDATE messages SET state='confirmed', confirmed_by=?, confirmed_at_ms=? "
                "WHERE message_id=?",
                (actual_consumer, effective_now, str(delivery["message_id"])),
            )
            self.conn.execute(
                "UPDATE deliveries SET confirmed=1, confirmed_at_ms=?, confirmed_by=? "
                "WHERE delivery_id=?",
                (effective_now, actual_consumer, delivery_id),
            )
            self.conn.execute(
                "UPDATE consumer_usage SET confirmed=confirmed+1 WHERE consumer_id=?",
                (actual_consumer,),
            )
            advance_clock(self.conn, effective_now)
            version = bump_queue_version(self.conn)
        return {
            "ok": True,
            "command": "confirm",
            "delivery_id": delivery_id,
            "message_id": str(delivery["message_id"]),
            "consumer_id": actual_consumer,
            "confirmed": True,
            "idempotent": False,
            "generation": int(delivery["generation"]),
            "confirmed_at_ms": effective_now,
            "queue_version": version,
        }

    def _get_request(self, request_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM delivery_requests WHERE request_id=?", (request_id,)
        ).fetchone()

    def _recover_started_for_request(self, request_id: str) -> int:
        rows = self.conn.execute(
            "SELECT delivery_id FROM deliveries WHERE request_id=? AND status='started' "
            "ORDER BY started_at_ms, delivery_id",
            (request_id,),
        ).fetchall()
        for row in rows:
            self._complete_delivery(str(row["delivery_id"]))
        return len(rows)

    def _select_candidate(self, request: sqlite3.Row, cursor: int) -> sqlite3.Row | None:
        message_id = request["message_id"]
        if message_id is not None:
            return self.conn.execute(
                "SELECT * FROM messages WHERE message_id=?", (str(message_id),)
            ).fetchone()
        return self.conn.execute(
            "SELECT m.* FROM messages m "
            "WHERE m.topic=? AND m.state='ready' AND m.available_at_ms<=? "
            "AND NOT EXISTS (SELECT 1 FROM abandonments a "
            "  WHERE a.request_id=? AND a.message_id=m.message_id AND a.generation=m.generation) "
            "ORDER BY m.partition, m.seq_in_partition LIMIT 1",
            (str(request["topic"]), cursor, str(request["request_id"])),
        ).fetchone()

    def _specific_message_status(
        self, request: sqlite3.Row, message: sqlite3.Row, cursor: int
    ) -> tuple[str, str, str, dict[str, Any]] | None:
        state = str(message["state"])
        if state == "confirmed":
            return (
                "not_started",
                "not_started",
                "already_confirmed",
                {"message_id": str(message["message_id"]), "generation": int(message["generation"])},
            )
        if state == "dead_letter":
            return (
                "not_started",
                "not_started",
                "dead_letter",
                {"message_id": str(message["message_id"]), "generation": int(message["generation"])},
            )
        if state in {"delivered", "inflight"}:
            return (
                "not_started",
                "not_started",
                "already_in_delivery",
                {
                    "message_id": str(message["message_id"]),
                    "generation": int(message["generation"]),
                    "state": state,
                },
            )
        if state != "ready":
            return (
                "not_started",
                "not_started",
                "not_ready",
                {"message_id": str(message["message_id"]), "state": state},
            )
        if int(message["available_at_ms"]) > cursor:
            return (
                "not_started",
                "not_started",
                "not_yet_available",
                {
                    "message_id": str(message["message_id"]),
                    "available_at_ms": int(message["available_at_ms"]),
                    "wait_ms": int(message["available_at_ms"]) - cursor,
                },
            )
        abandoned = self.conn.execute(
            "SELECT 1 FROM abandonments WHERE request_id=? AND message_id=? AND generation=?",
            (str(request["request_id"]), str(message["message_id"]), int(message["generation"])),
        ).fetchone()
        if abandoned is not None and request["message_id"] is not None:
            return (
                "not_started",
                "not_started",
                "abandoned_for_request",
                {"message_id": str(message["message_id"]), "generation": int(message["generation"])},
            )
        return None

    def _plan_fault(
        self, message: sqlite3.Row, consumer: ConsumerConfig, cursor: int
    ) -> FaultPlan:
        fault = self.conn.execute(
            "SELECT * FROM fault_events WHERE message_id=? AND consumer_id=? "
            "AND consumed_delivery_id IS NULL AND (generation IS NULL OR generation=?) "
            "ORDER BY CASE WHEN generation IS NULL THEN 1 ELSE 0 END, fault_id LIMIT 1",
            (str(message["message_id"]), consumer.consumer_id, int(message["generation"]) + 1),
        ).fetchone()
        duration = (
            int(fault["duration_ms"])
            if fault is not None and fault["duration_ms"] is not None
            else self.config.quota.attempt_duration_ms
        )
        if fault is None:
            return FaultPlan(None, "success", duration, "delivered", "success")
        action = str(fault["action"])
        mapping = {
            "success": ("delivered", "success"),
            "slow": ("delivered", "slow_success"),
            "crash_after_claim": ("delivered", "crash_after_claim"),
            "fail": ("failed", "injected_failure"),
            "reject": ("rejected", "consumer_rejected"),
            "expire": ("expired", "message_expired"),
        }
        planned_outcome, planned_reason = mapping[action]
        return FaultPlan(
            int(fault["fault_id"]),
            action,
            duration,
            planned_outcome,
            planned_reason,
        )

    def _project_refill(
        self, message: sqlite3.Row, consumer: ConsumerConfig, cursor: int
    ) -> dict[str, int | str]:
        global_bucket = self.conn.execute(
            "SELECT * FROM buckets WHERE bucket_id='global'"
        ).fetchone()
        consumer_bucket = self.conn.execute(
            "SELECT * FROM buckets WHERE bucket_id=?",
            (f"consumer:{consumer.consumer_id}",),
        ).fetchone()
        if global_bucket is None or consumer_bucket is None:
            raise GatewayError(ErrorCode.INTERNAL, "token bucket is missing", {})
        global_balance, _ = _project_bucket_at(global_bucket, cursor)
        consumer_balance, _ = _project_bucket_at(consumer_bucket, cursor)
        return {
            "message_id": str(message["message_id"]),
            "global_balance": global_balance,
            "consumer_balance": consumer_balance,
            "global_last_refill_ms": int(global_bucket["last_refill_ms"]),
            "consumer_last_refill_ms": int(consumer_bucket["last_refill_ms"]),
            "global_rate": int(global_bucket["refill_rate_scaled"]),
            "consumer_rate": int(consumer_bucket["refill_rate_scaled"]),
        }

    def _record_wait(
        self,
        request: sqlite3.Row,
        message: sqlite3.Row,
        cursor: int,
        projected: dict[str, int | str],
        needed_wait: int,
        reason: str,
    ) -> None:
        self.conn.execute(
            "INSERT INTO token_waits("
            "request_id, consumer_id, message_id, at_ms, global_balance_scaled, "
            "global_last_refill_ms, consumer_balance_scaled, consumer_last_refill_ms, "
            "global_rate_scaled, consumer_rate_scaled, needed_wait_ms, reason"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(request["request_id"]),
                str(request["consumer_id"]),
                str(message["message_id"]),
                cursor,
                int(projected["global_balance"]),
                int(projected["global_last_refill_ms"]),
                int(projected["consumer_balance"]),
                int(projected["consumer_last_refill_ms"]),
                int(projected["global_rate"]),
                int(projected["consumer_rate"]),
                needed_wait,
                reason,
            ),
        )
        advance_clock(self.conn, cursor)

    def _record_wait_and_not_started(
        self,
        *,
        request: sqlite3.Row,
        message: sqlite3.Row,
        cursor: int,
        projected: dict[str, int | str],
        needed_wait: int,
        reason: str,
        finish_reason: str,
    ) -> None:
        with transaction(self.conn):
            self._record_wait(request, message, cursor, projected, needed_wait, reason)
            self._record_not_started_tx(
                current=request,
                message=message,
                at_ms=cursor,
                kind="not_started",
                status="not_started",
                reason=reason,
                detail={
                    "needed_wait_ms": needed_wait,
                    "global_balance_scaled": int(projected["global_balance"]),
                    "consumer_balance_scaled": int(projected["consumer_balance"]),
                },
                finish_reason=finish_reason,
            )
            bump_queue_version(self.conn)

    def _advance_over_token_wait(
        self,
        *,
        request: sqlite3.Row,
        message: sqlite3.Row,
        cursor: int,
        projected: dict[str, int | str],
        needed_wait: int,
    ) -> None:
        new_cursor = cursor + needed_wait
        with transaction(self.conn):
            self._record_wait(request, message, cursor, projected, needed_wait, "within_round")
            self.conn.execute(
                "UPDATE delivery_requests SET cursor_ms=?, updated_ms=? WHERE request_id=?",
                (new_cursor, new_cursor, str(request["request_id"])),
            )
            advance_clock(self.conn, new_cursor)
            bump_queue_version(self.conn)

    def _abandon_before_start(
        self,
        *,
        request: sqlite3.Row,
        message: sqlite3.Row,
        at_ms: int,
        reason: str,
        duration_ms: int,
    ) -> None:
        request_id = str(request["request_id"])
        with transaction(self.conn):
            current = self._get_request(request_id)
            assert current is not None
            sequence = int(current["outcome_count"])
            generation = int(message["generation"])
            self.conn.execute(
                "INSERT OR IGNORE INTO abandonments(request_id, message_id, generation, reason, at_ms) "
                "VALUES(?, ?, ?, ?, ?)",
                (request_id, str(message["message_id"]), generation, reason, at_ms),
            )
            self._insert_outcome(
                request_id=request_id,
                sequence=sequence,
                message_id=str(message["message_id"]),
                delivery_id=None,
                kind="abandoned",
                status="abandoned",
                reason=reason,
                at_ms=at_ms,
                duration_ms=duration_ms,
                generation=generation,
                detail={
                    "planned_duration_ms": duration_ms,
                    "delivery_timeout_ms": self.config.consumer_by_id[
                        str(request["consumer_id"])
                    ].delivery_timeout_ms,
                },
            )
            next_count = sequence + 1
            limit_count = int(current["limit_count"])
            finish = "request_limit" if next_count >= limit_count else None
            self.conn.execute(
                "UPDATE delivery_requests SET outcome_count=?, finish_reason=?, cursor_ms=?, updated_ms=? "
                "WHERE request_id=?",
                (next_count, finish, int(current["cursor_ms"]), int(current["cursor_ms"]), request_id),
            )
            self.conn.execute(
                "UPDATE consumer_usage SET abandoned=abandoned+1 WHERE consumer_id=?",
                (str(request["consumer_id"]),),
            )
            advance_clock(self.conn, at_ms)
            bump_queue_version(self.conn)
        if finish:
            self._finish_request(request_id, finish)

    def _record_not_started(
        self,
        *,
        request_id: str,
        message: sqlite3.Row,
        at_ms: int,
        kind: str,
        status: str,
        reason: str,
        detail: dict[str, Any],
        finish_reason: str,
    ) -> None:
        with transaction(self.conn):
            current = self._get_request(request_id)
            assert current is not None
            self._record_not_started_tx(
                current=current,
                message=message,
                at_ms=at_ms,
                kind=kind,
                status=status,
                reason=reason,
                detail=detail,
                finish_reason=finish_reason,
            )

    def _record_not_started_tx(
        self,
        *,
        current: sqlite3.Row,
        message: sqlite3.Row,
        at_ms: int,
        kind: str,
        status: str,
        reason: str,
        detail: dict[str, Any],
        finish_reason: str,
    ) -> None:
        sequence = int(current["outcome_count"])
        self._insert_outcome(
            request_id=str(current["request_id"]),
            sequence=sequence,
            message_id=str(message["message_id"]),
            delivery_id=None,
            kind=kind,
            status=status,
            reason=reason,
            at_ms=at_ms,
            duration_ms=0,
            generation=int(message["generation"]),
            detail=detail,
        )
        self.conn.execute(
            "UPDATE delivery_requests SET outcome_count=?, finish_reason=?, cursor_ms=?, updated_ms=? "
            "WHERE request_id=?",
            (
                sequence + 1,
                finish_reason,
                int(current["cursor_ms"]),
                int(current["cursor_ms"]),
                str(current["request_id"]),
            ),
        )
        advance_clock(self.conn, at_ms)
        bump_queue_version(self.conn)

    def _claim_attempt(
        self,
        *,
        request: sqlite3.Row,
        message: sqlite3.Row,
        consumer: ConsumerConfig,
        cursor: int,
        plan: FaultPlan,
    ) -> str | None:
        with transaction(self.conn):
            current_message = self.conn.execute(
                "SELECT * FROM messages WHERE message_id=?",
                (str(message["message_id"]),),
            ).fetchone()
            if (
                current_message is None
                or str(current_message["state"]) != "ready"
                or int(current_message["available_at_ms"]) > cursor
                or int(current_message["generation"]) != int(message["generation"])
            ):
                return None
            global_bucket = self.conn.execute(
                "SELECT * FROM buckets WHERE bucket_id='global'"
            ).fetchone()
            consumer_bucket = self.conn.execute(
                "SELECT * FROM buckets WHERE bucket_id=?",
                (f"consumer:{consumer.consumer_id}",),
            ).fetchone()
            assert global_bucket is not None and consumer_bucket is not None
            global_after, global_refill = _project_bucket_at(global_bucket, cursor)
            consumer_after, consumer_refill = _project_bucket_at(consumer_bucket, cursor)
            if global_after < TOKEN_SCALE or consumer_after < TOKEN_SCALE:
                raise GatewayError(
                    ErrorCode.INTERNAL,
                    "bucket balance changed between planning and claim",
                    {
                        "global_balance_scaled": global_after,
                        "consumer_balance_scaled": consumer_after,
                    },
                )
            global_after -= TOKEN_SCALE
            consumer_after -= TOKEN_SCALE
            self.conn.execute(
                "UPDATE buckets SET balance_scaled=?, last_refill_ms=? WHERE bucket_id='global'",
                (global_after, cursor),
            )
            self.conn.execute(
                "UPDATE buckets SET balance_scaled=?, last_refill_ms=? WHERE bucket_id=?",
                (consumer_after, cursor, f"consumer:{consumer.consumer_id}"),
            )
            new_generation = int(current_message["generation"]) + 1
            delivery_id = "dlv_" + hashlib.sha256(
                f"{current_message['message_id']}:{new_generation}".encode()
            ).hexdigest()[:24]
            self.conn.execute(
                "UPDATE messages SET generation=?, state='inflight', current_delivery_id=? "
                "WHERE message_id=?",
                (new_generation, delivery_id, str(current_message["message_id"])),
            )
            self.conn.execute(
                "INSERT INTO deliveries("
                "delivery_id, request_id, message_id, consumer_id, generation, started_at_ms, "
                "duration_ms, planned_outcome, planned_reason, status, fault_action"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', ?)",
                (
                    delivery_id,
                    str(request["request_id"]),
                    str(current_message["message_id"]),
                    consumer.consumer_id,
                    new_generation,
                    cursor,
                    plan.duration_ms,
                    plan.planned_outcome,
                    plan.planned_reason,
                    None if plan.fault_id is None else plan.action,
                ),
            )
            self.conn.execute(
                "INSERT INTO ledger_events("
                "delivery_id, request_id, message_id, consumer_id, at_ms, charge_scaled, "
                "global_before_scaled, global_refill_scaled, global_after_scaled, "
                "global_last_before_ms, global_last_after_ms, "
                "consumer_before_scaled, consumer_refill_scaled, consumer_after_scaled, "
                "consumer_last_before_ms, consumer_last_after_ms, global_rate_scaled, consumer_rate_scaled"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    delivery_id,
                    str(request["request_id"]),
                    str(current_message["message_id"]),
                    consumer.consumer_id,
                    cursor,
                    TOKEN_SCALE,
                    int(global_bucket["balance_scaled"]),
                    global_refill,
                    global_after,
                    int(global_bucket["last_refill_ms"]),
                    cursor,
                    int(consumer_bucket["balance_scaled"]),
                    consumer_refill,
                    consumer_after,
                    int(consumer_bucket["last_refill_ms"]),
                    cursor,
                    int(global_bucket["refill_rate_scaled"]),
                    int(consumer_bucket["refill_rate_scaled"]),
                ),
            )
            if plan.fault_id is not None:
                self.conn.execute(
                    "UPDATE fault_events SET consumed_delivery_id=?, consumed_at_ms=? WHERE fault_id=?",
                    (delivery_id, cursor, plan.fault_id),
                )
            self.conn.execute(
                "UPDATE delivery_requests SET cursor_ms=?, updated_ms=? WHERE request_id=?",
                (cursor, cursor, str(request["request_id"])),
            )
            advance_clock(self.conn, cursor)
            bump_queue_version(self.conn)
            _maybe_crash("claim_after_bucket_update")
            _maybe_crash("claim_after_generation_increment")
            _maybe_crash("claim_before_commit")
        _maybe_crash("claim_after_commit")
        return delivery_id

    def _complete_delivery(self, delivery_id: str) -> dict[str, Any] | None:
        with transaction(self.conn):
            delivery = self.conn.execute(
                "SELECT * FROM deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if delivery is None:
                raise GatewayError(
                    ErrorCode.DELIVERY_NOT_FOUND,
                    "delivery is missing during completion",
                    {"delivery_id": delivery_id},
                )
            if str(delivery["status"]) != "started":
                existing = self.conn.execute(
                    "SELECT * FROM request_outcomes WHERE delivery_id=?", (delivery_id,)
                ).fetchone()
                return None if existing is None else _outcome_row_to_dict(existing)
            request = self._get_request(str(delivery["request_id"]))
            if request is None:
                raise GatewayError(
                    ErrorCode.INTERNAL,
                    "delivery request journal is missing",
                    {"delivery_id": delivery_id, "request_id": str(delivery["request_id"])},
                )
            message = self.conn.execute(
                "SELECT * FROM messages WHERE message_id=?", (str(delivery["message_id"]),)
            ).fetchone()
            if message is None:
                raise GatewayError(
                    ErrorCode.MESSAGE_NOT_FOUND,
                    "delivery message is missing",
                    {"delivery_id": delivery_id, "message_id": str(delivery["message_id"])},
                )
            completed_at = int(delivery["started_at_ms"]) + int(delivery["duration_ms"])
            outcome = str(delivery["planned_outcome"])
            reason = str(delivery["planned_reason"] or outcome)
            generation = int(delivery["generation"])
            retry_detail: dict[str, Any] | None = None
            if outcome == "delivered":
                self.conn.execute(
                    "UPDATE messages SET state='delivered', current_delivery_id=?, "
                    "available_at_ms=? WHERE message_id=?",
                    (delivery_id, completed_at, str(message["message_id"])),
                )
                self.conn.execute(
                    "UPDATE deliveries SET status='delivered', completed_at_ms=? WHERE delivery_id=?",
                    (completed_at, delivery_id),
                )
                kind = "delivered"
                status = "delivered"
                self.conn.execute(
                    "UPDATE consumer_usage SET delivered=delivered+1 WHERE consumer_id=?",
                    (str(delivery["consumer_id"]),),
                )
            elif outcome == "failed":
                self.conn.execute(
                    "UPDATE deliveries SET status='failed', completed_at_ms=? WHERE delivery_id=?",
                    (completed_at, delivery_id),
                )
                kind = "failed_in_flight"
                status = "failed"
                self.conn.execute(
                    "UPDATE consumer_usage SET failed=failed+1 WHERE consumer_id=?",
                    (str(delivery["consumer_id"]),),
                )
                if generation >= self.config.quota.max_delivery_generation:
                    self._dead_letter(
                        message=message,
                        delivery=delivery,
                        reason="failed",
                        at_ms=completed_at,
                    )
                    reason = "generation_limit_after_failure"
                else:
                    capped, jitter, delay = backoff_delay_ms(
                        str(message["message_id"]),
                        str(delivery["consumer_id"]),
                        generation,
                        self.config.quota.retry_backoff_base_ms,
                        self.config.quota.retry_backoff_max_ms,
                    )
                    next_at = completed_at + delay
                    self.conn.execute(
                        "UPDATE messages SET state='ready', current_delivery_id=NULL, available_at_ms=? "
                        "WHERE message_id=?",
                        (next_at, str(message["message_id"])),
                    )
                    self.conn.execute(
                        "INSERT INTO retry_events("
                        "delivery_id, message_id, consumer_id, generation, at_ms, capped_base_ms, "
                        "jitter_ms, delay_ms, next_at_ms"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            delivery_id,
                            str(message["message_id"]),
                            str(delivery["consumer_id"]),
                            generation,
                            completed_at,
                            capped,
                            jitter,
                            delay,
                            next_at,
                        ),
                    )
                    retry_detail = {
                        "capped_base_ms": capped,
                        "jitter_ms": jitter,
                        "delay_ms": delay,
                        "next_at_ms": next_at,
                    }
            elif outcome == "rejected":
                self.conn.execute(
                    "UPDATE deliveries SET status='rejected', completed_at_ms=? WHERE delivery_id=?",
                    (completed_at, delivery_id),
                )
                kind = "failed_in_flight"
                status = "rejected"
                self.conn.execute(
                    "UPDATE consumer_usage SET rejected=rejected+1 WHERE consumer_id=?",
                    (str(delivery["consumer_id"]),),
                )
                self._dead_letter(
                    message=message,
                    delivery=delivery,
                    reason="rejected",
                    at_ms=completed_at,
                )
            elif outcome == "expired":
                self.conn.execute(
                    "UPDATE deliveries SET status='expired', completed_at_ms=? WHERE delivery_id=?",
                    (completed_at, delivery_id),
                )
                kind = "failed_in_flight"
                status = "expired"
                self.conn.execute(
                    "UPDATE consumer_usage SET expired=expired+1 WHERE consumer_id=?",
                    (str(delivery["consumer_id"]),),
                )
                self._dead_letter(
                    message=message,
                    delivery=delivery,
                    reason="expired",
                    at_ms=completed_at,
                )
            else:
                raise GatewayError(
                    ErrorCode.INTERNAL,
                    "unknown planned delivery outcome",
                    {"delivery_id": delivery_id, "outcome": outcome},
                )
            sequence = int(request["outcome_count"])
            detail: dict[str, Any] = {
                "planned_outcome": outcome,
                "token_charge_scaled": TOKEN_SCALE,
            }
            if retry_detail is not None:
                detail["retry"] = retry_detail
            if status in {"failed", "rejected", "expired"} and generation >= self.config.quota.max_delivery_generation:
                detail["generation_limit"] = self.config.quota.max_delivery_generation
            self._insert_outcome(
                request_id=str(request["request_id"]),
                sequence=sequence,
                message_id=str(message["message_id"]),
                delivery_id=delivery_id,
                kind=kind,
                status=status,
                reason=reason,
                at_ms=completed_at,
                duration_ms=int(delivery["duration_ms"]),
                generation=generation,
                detail=detail,
            )
            next_count = sequence + 1
            limit_count = int(request["limit_count"])
            finish = "request_limit" if next_count >= limit_count else None
            self.conn.execute(
                "UPDATE delivery_requests SET outcome_count=?, finish_reason=?, cursor_ms=?, updated_ms=? "
                "WHERE request_id=?",
                (
                    next_count,
                    finish,
                    completed_at,
                    completed_at,
                    str(request["request_id"]),
                ),
            )
            self.conn.execute(
                "UPDATE consumer_usage SET attempts=attempts+1, tokens_spent_scaled=tokens_spent_scaled+? "
                "WHERE consumer_id=?",
                (TOKEN_SCALE, str(delivery["consumer_id"])),
            )
            advance_clock(self.conn, completed_at)
            bump_queue_version(self.conn)
            _maybe_crash("complete_after_tombstone")
            _maybe_crash("complete_before_commit")
        _maybe_crash("complete_after_commit")
        self._maybe_finish_after_completion(str(request["request_id"]), finish)
        return {
            "message_id": str(message["message_id"]),
            "delivery_id": delivery_id,
            "kind": kind,
            "status": status,
            "reason": reason,
            "at_ms": completed_at,
            "duration_ms": int(delivery["duration_ms"]),
            "generation": generation,
        }

    def _maybe_finish_after_completion(self, request_id: str, finish: str | None) -> None:
        if finish:
            self._finish_request(request_id, finish)

    def _dead_letter(
        self,
        *,
        message: sqlite3.Row,
        delivery: sqlite3.Row,
        reason: str,
        at_ms: int,
    ) -> None:
        tombstone = "tomb_" + hashlib.sha256(
            f"tombstone:{message['message_id']}".encode()
        ).hexdigest()[:32]
        existing = self.conn.execute(
            "SELECT * FROM dead_letters WHERE message_id=?", (str(message["message_id"]),)
        ).fetchone()
        if existing is None:
            self.conn.execute(
                "INSERT INTO dead_letters(message_id, reason, generation, delivery_id, at_ms, tombstone) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (
                    str(message["message_id"]),
                    reason,
                    int(delivery["generation"]),
                    str(delivery["delivery_id"]),
                    at_ms,
                    tombstone,
                ),
            )
        self.conn.execute(
            "UPDATE messages SET state='dead_letter', current_delivery_id=NULL, "
            "dead_letter_reason=?, dead_letter_at_ms=? WHERE message_id=?",
            (reason, at_ms, str(message["message_id"])),
        )

    def _dead_letter_from_limit(
        self,
        request: sqlite3.Row,
        message: sqlite3.Row,
        at_ms: int,
    ) -> None:
        with transaction(self.conn):
            tombstone = "tomb_" + hashlib.sha256(
                f"tombstone:{message['message_id']}".encode()
            ).hexdigest()[:32]
            self.conn.execute(
                "INSERT OR IGNORE INTO dead_letters("
                "message_id, reason, generation, delivery_id, at_ms, tombstone"
                ") VALUES(?, 'expired', ?, NULL, ?, ?)",
                (str(message["message_id"]), int(message["generation"]), at_ms, tombstone),
            )
            self.conn.execute(
                "UPDATE messages SET state='dead_letter', current_delivery_id=NULL, "
                "dead_letter_reason='expired', dead_letter_at_ms=? WHERE message_id=?",
                (at_ms, str(message["message_id"])),
            )
            current = self._get_request(str(request["request_id"]))
            assert current is not None
            sequence = int(current["outcome_count"])
            self._insert_outcome(
                request_id=str(request["request_id"]),
                sequence=sequence,
                message_id=str(message["message_id"]),
                delivery_id=None,
                kind="not_started",
                status="not_started",
                reason="generation_limit_exhausted",
                at_ms=at_ms,
                duration_ms=0,
                generation=int(message["generation"]),
                detail={"max_delivery_generation": self.config.quota.max_delivery_generation},
            )
            self.conn.execute(
                "UPDATE delivery_requests SET outcome_count=?, finish_reason='generation_limit', "
                "cursor_ms=?, updated_ms=? WHERE request_id=?",
                (sequence + 1, at_ms, at_ms, str(request["request_id"])),
            )
            advance_clock(self.conn, at_ms)
            bump_queue_version(self.conn)

    def _insert_outcome(
        self,
        *,
        request_id: str,
        sequence: int,
        message_id: str | None,
        delivery_id: str | None,
        kind: str,
        status: str,
        reason: str,
        at_ms: int,
        duration_ms: int,
        generation: int | None,
        detail: dict[str, Any],
    ) -> None:
        self.conn.execute(
            "INSERT INTO request_outcomes("
            "request_id, sequence, message_id, delivery_id, kind, status, reason, at_ms, "
            "duration_ms, generation, detail_json"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                request_id,
                sequence,
                message_id,
                delivery_id,
                kind,
                status,
                reason,
                at_ms,
                duration_ms,
                generation,
                canonical_json(detail),
            ),
        )

    def _finish_request(self, request_id: str, stop_reason: str) -> dict[str, Any]:
        with transaction(self.conn):
            request = self._get_request(request_id)
            if request is None:
                raise GatewayError(ErrorCode.INTERNAL, "request journal is missing", {"request_id": request_id})
            if str(request["status"]) == "completed":
                return json.loads(str(request["result_json"]))
            outcome_rows = self.conn.execute(
                "SELECT * FROM request_outcomes WHERE request_id=? ORDER BY sequence", (request_id,)
            ).fetchall()
            outcomes = [_outcome_row_to_dict(row) for row in outcome_rows]
            summary = {
                "delivered": sum(1 for outcome in outcomes if outcome["kind"] == "delivered"),
                "failed_in_flight": sum(1 for outcome in outcomes if outcome["kind"] == "failed_in_flight"),
                "abandoned": sum(1 for outcome in outcomes if outcome["kind"] == "abandoned"),
                "not_started": sum(1 for outcome in outcomes if outcome["kind"] == "not_started"),
            }
            cursor = int(request["cursor_ms"])
            start_cursor = int(request["start_cursor_ms"])
            advance_clock(self.conn, cursor)
            queue_version = bump_queue_version(self.conn)
            result = {
                "ok": True,
                "command": "deliver",
                "request_id": request_id,
                "consumer_id": str(request["consumer_id"]),
                "topic": str(request["topic"]),
                "status": "completed",
                "started_at_ms": start_cursor,
                "finished_at_ms": cursor,
                "deadline_ms": int(request["deadline_ms"]),
                "budget_used_ms": max(0, cursor - start_cursor),
                "limit": int(request["limit_count"]),
                "processed": len(outcomes),
                "stop_reason": stop_reason,
                "summary": summary,
                "outcomes": outcomes,
                "queue_version": queue_version,
                "clock_watermark_ms": clock_watermark(self.conn),
            }
            result_json = canonical_json(result)
            self.conn.execute(
                "UPDATE delivery_requests SET status='completed', cursor_ms=?, finish_reason=?, "
                "result_json=?, updated_ms=? WHERE request_id=?",
                (cursor, stop_reason, result_json, cursor, request_id),
            )
            self.conn.execute(
                "UPDATE consumer_usage SET rounds=rounds+1, logical_time_used_ms=logical_time_used_ms+? "
                "WHERE consumer_id=?",
                (max(0, cursor - start_cursor), str(request["consumer_id"])),
            )
        return json.loads(result_json)


def _project_bucket_at(bucket: sqlite3.Row, at_ms: int) -> tuple[int, int]:
    last = int(bucket["last_refill_ms"])
    if at_ms < last:
        return int(bucket["balance_scaled"]), 0
    elapsed = at_ms - last
    refill = (elapsed * int(bucket["refill_rate_scaled"])) // 1000
    balance = min(int(bucket["capacity_scaled"]), int(bucket["balance_scaled"]) + refill)
    return balance, balance - int(bucket["balance_scaled"])


def _outcome_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sequence": int(row["sequence"]),
        "message_id": row["message_id"],
        "delivery_id": row["delivery_id"],
        "kind": str(row["kind"]),
        "status": str(row["status"]),
        "reason": str(row["reason"]),
        "at_ms": int(row["at_ms"]),
        "duration_ms": int(row["duration_ms"]),
        "generation": None if row["generation"] is None else int(row["generation"]),
        "detail": json.loads(str(row["detail_json"])),
    }


def deliver_command(
    db_path: str | Path,
    *,
    consumer_id: str,
    request_id: str | None,
    now_ms: int | None,
    limit: int | None,
    message_id: str | None,
    topic: str | None,
) -> dict[str, Any]:
    require_existing(db_path)
    with write_lock(db_path):
        conn = connect(db_path, readonly=False)
        try:
            initialize_schema(conn)
            config = load_runtime_config(conn)
            req_id = request_id or default_request_id(
                consumer_id,
                0 if now_ms is None else now_ms,
                config.consumer_by_id[consumer_id].request_size
                if consumer_id in config.consumer_by_id
                else 0,
                message_id,
            )
            engine = DeliveryEngine(conn, config)
            return engine.deliver(
                consumer_id=consumer_id,
                request_id=req_id,
                now_ms=now_ms,
                limit=limit,
                message_id=message_id,
                topic=topic,
            )
        finally:
            conn.close()


def resume_command(db_path: str | Path, *, now_ms: int | None) -> dict[str, Any]:
    require_existing(db_path)
    with write_lock(db_path):
        conn = connect(db_path, readonly=False)
        try:
            initialize_schema(conn)
            config = load_runtime_config(conn)
            engine = DeliveryEngine(conn, config)
            return engine.resume(now_ms=now_ms)
        finally:
            conn.close()


def confirm_command(
    db_path: str | Path,
    *,
    delivery_id: str,
    consumer_id: str | None,
    now_ms: int | None,
) -> dict[str, Any]:
    require_existing(db_path)
    with write_lock(db_path):
        conn = connect(db_path, readonly=False)
        try:
            initialize_schema(conn)
            config = load_runtime_config(conn)
            engine = DeliveryEngine(conn, config)
            return engine.confirm(delivery_id=delivery_id, consumer_id=consumer_id, now_ms=now_ms)
        finally:
            conn.close()


def status_command(
    db_path: str | Path,
    *,
    topic_filter: str | None = None,
) -> dict[str, Any]:
    p = require_existing(db_path)
    conn = connect(p, readonly=True)
    try:
        with read_snapshot(conn):
            config = load_runtime_config(conn)
            if topic_filter is not None and topic_filter not in config.topic_by_name:
                raise GatewayError(
                    ErrorCode.TOPIC_NOT_FOUND,
                    "status requested for an unknown topic",
                    {"topic": topic_filter},
                )
            quota_row = conn.execute("SELECT * FROM quota_snapshot WHERE singleton=1").fetchone()
            bucket_rows = conn.execute("SELECT * FROM buckets ORDER BY bucket_id").fetchall()
            usage_rows = conn.execute(
                "SELECT * FROM consumer_usage ORDER BY consumer_id"
            ).fetchall()
            state_rows = conn.execute(
                "SELECT topic, partition, state, COUNT(*) AS n, MAX(generation) AS max_generation "
                "FROM messages GROUP BY topic, partition, state"
            ).fetchall()
            dead_rows = conn.execute(
                "SELECT topic, reason, COUNT(*) AS n FROM messages m "
                "JOIN dead_letters d ON d.message_id=m.message_id "
                "GROUP BY topic, reason ORDER BY topic, reason"
            ).fetchall()
            request_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM delivery_requests GROUP BY status"
            ).fetchall()
            request_counts = {str(row["status"]): int(row["n"]) for row in request_rows}
            bucket_by_id = {str(row["bucket_id"]): row for row in bucket_rows}
            usage_by_id = {str(row["consumer_id"]): row for row in usage_rows}
            message_stats: dict[str, dict[str, Any]] = {}
            for topic in config.topics:
                message_stats[topic.name] = {
                    "backlog": 0,
                    "ready": 0,
                    "inflight": 0,
                    "delivered_unconfirmed": 0,
                    "confirmed": 0,
                    "dead_letter": 0,
                    "max_generation": 0,
                    "dead_letters": 0,
                    "partitions": [
                        {
                            "partition": index,
                            "backlog": 0,
                            "ready": 0,
                            "inflight": 0,
                            "delivered_unconfirmed": 0,
                            "confirmed": 0,
                            "dead_letter": 0,
                            "max_generation": 0,
                        }
                        for index in range(topic.partitions)
                    ],
                }
            for row in state_rows:
                topic_name = str(row["topic"])
                if topic_name not in message_stats:
                    continue
                state = str(row["state"])
                count = int(row["n"])
                max_generation = int(row["max_generation"] or 0)
                topic_stat = message_stats[topic_name]
                partition_stat = topic_stat["partitions"][int(row["partition"])]
                topic_stat["max_generation"] = max(topic_stat["max_generation"], max_generation)
                partition_stat["max_generation"] = max(partition_stat["max_generation"], max_generation)
                state_key = "delivered_unconfirmed" if state == "delivered" else state
                topic_stat[state_key] = int(topic_stat.get(state_key, 0)) + count
                partition_stat[state_key] = int(partition_stat.get(state_key, 0)) + count
                if state in {"ready", "inflight", "delivered"}:
                    topic_stat["backlog"] += count
                    partition_stat["backlog"] += count
            for row in dead_rows:
                topic_name = str(row["topic"])
                if topic_name in message_stats:
                    message_stats[topic_name]["dead_letters"] += int(row["n"])
            topics_output: list[dict[str, Any]] = []
            for topic in config.topics:
                if topic_filter is not None and topic.name != topic_filter:
                    continue
                stat = message_stats[topic.name]
                topics_output.append(
                    {
                        "name": topic.name,
                        "partitions": topic.partitions,
                        "partition_key_rule": topic.partition_key_rule,
                        "backlog": stat["backlog"],
                        "ready": stat["ready"],
                        "inflight": stat["inflight"],
                        "delivered_unconfirmed": stat["delivered_unconfirmed"],
                        "confirmed": stat["confirmed"],
                        "dead_letter": stat["dead_letter"],
                        "dead_letters": stat["dead_letters"],
                        "max_delivery_generation": stat["max_generation"],
                        "partition_stats": stat["partitions"],
                    }
                )
            consumers_output: list[dict[str, Any]] = []
            for consumer in config.consumers:
                if topic_filter is not None and consumer.topic != topic_filter:
                    continue
                bucket_id = f"consumer:{consumer.consumer_id}"
                bucket = bucket_by_id.get(bucket_id)
                usage = usage_by_id.get(consumer.consumer_id)
                if bucket is None or usage is None:
                    raise GatewayError(
                        ErrorCode.VERIFY_FAILED,
                        "consumer token bucket or usage state is missing",
                        {"consumer_id": consumer.consumer_id},
                    )
                consumers_output.append(
                    {
                        "consumer_id": consumer.consumer_id,
                        "topic": consumer.topic,
                        "request_size": consumer.request_size,
                        "rate_limit_per_sec": rate_text(int(bucket["refill_rate_scaled"])),
                        "delivery_timeout_ms": consumer.delivery_timeout_ms,
                        "token_balance_scaled": int(bucket["balance_scaled"]),
                        "token_balance": rate_text(int(bucket["balance_scaled"])),
                        "token_capacity_scaled": int(bucket["capacity_scaled"]),
                        "last_refill_ms": int(bucket["last_refill_ms"]),
                        "budget_consumption": {
                            "logical_time_used_ms": int(usage["logical_time_used_ms"]),
                            "rounds": int(usage["rounds"]),
                            "round_budget_ms": int(quota_row["round_budget_ms"]),
                        },
                        "counters": {
                            "attempts": int(usage["attempts"]),
                            "delivered": int(usage["delivered"]),
                            "failed": int(usage["failed"]),
                            "rejected": int(usage["rejected"]),
                            "expired": int(usage["expired"]),
                            "abandoned": int(usage["abandoned"]),
                            "confirmed": int(usage["confirmed"]),
                            "tokens_spent_scaled": int(usage["tokens_spent_scaled"]),
                        },
                    }
                )
            global_bucket = bucket_by_id.get("global")
            if global_bucket is None:
                raise GatewayError(ErrorCode.VERIFY_FAILED, "global token bucket is missing", {})
            all_max = max((item["max_delivery_generation"] for item in topics_output), default=0)
            total_dead = sum(item["dead_letters"] for item in topics_output)
            return {
                "ok": True,
                "command": "status",
                "config_version": int(quota_row["config_version"]),
                "queue_version": int(get_meta(conn, "queue_version") or "0"),
                "clock_watermark_ms": clock_watermark(conn),
                "global_bucket": {
                    "token_balance_scaled": int(global_bucket["balance_scaled"]),
                    "token_balance": rate_text(int(global_bucket["balance_scaled"])),
                    "token_capacity_scaled": int(global_bucket["capacity_scaled"]),
                    "refill_rate_per_sec": rate_text(int(global_bucket["refill_rate_scaled"])),
                    "last_refill_ms": int(global_bucket["last_refill_ms"]),
                },
                "round_budget_ms": int(quota_row["round_budget_ms"]),
                "request_counts": {
                    "running": int(request_counts.get("running", 0)),
                    "completed": int(request_counts.get("completed", 0)),
                },
                "totals": {
                    "backlog": sum(item["backlog"] for item in topics_output),
                    "confirmed": sum(item["confirmed"] for item in topics_output),
                    "dead_letters": total_dead,
                    "max_delivery_generation": all_max,
                },
                "topics": topics_output,
                "consumers": consumers_output,
            }
    finally:
        conn.close()


def verify_command(db_path: str | Path) -> dict[str, Any]:
    p = require_existing(db_path)
    conn = connect(p, readonly=True)
    errors: list[dict[str, Any]] = []
    try:
        with read_snapshot(conn):
            config = load_runtime_config(conn)
            baselines = {
                str(row["bucket_id"]): row
                for row in conn.execute("SELECT * FROM bucket_baselines ORDER BY bucket_id").fetchall()
            }
            buckets = {
                str(row["bucket_id"]): row
                for row in conn.execute("SELECT * FROM buckets ORDER BY bucket_id").fetchall()
            }
            events = conn.execute("SELECT * FROM ledger_events ORDER BY event_id").fetchall()
            waits = conn.execute("SELECT * FROM token_waits ORDER BY wait_id").fetchall()
            messages = conn.execute("SELECT * FROM messages ORDER BY message_id").fetchall()
            deliveries = conn.execute(
                "SELECT * FROM deliveries ORDER BY message_id, generation"
            ).fetchall()
            dead_letters = conn.execute(
                "SELECT * FROM dead_letters ORDER BY message_id"
            ).fetchall()
            retry_events = conn.execute(
                "SELECT * FROM retry_events ORDER BY retry_id"
            ).fetchall()
            usage_rows = conn.execute(
                "SELECT * FROM consumer_usage ORDER BY consumer_id"
            ).fetchall()
            request_rows = conn.execute(
                "SELECT * FROM delivery_requests ORDER BY request_id"
            ).fetchall()
            outcomes = conn.execute(
                "SELECT * FROM request_outcomes ORDER BY request_id, sequence"
            ).fetchall()
            started_count = conn.execute(
                "SELECT COUNT(*) AS n FROM deliveries WHERE status='started'"
            ).fetchone()["n"]
            if int(started_count):
                errors.append(
                    {
                        "check": "unfinished_delivery",
                        "message": "started deliveries require resume before verification",
                        "count": int(started_count),
                    }
                )

            expected: dict[str, dict[str, int]] = {}
            for bucket_id, baseline in baselines.items():
                expected[bucket_id] = {
                    "balance": int(baseline["initial_balance_scaled"]),
                    "last": int(baseline["initial_last_refill_ms"]),
                    "capacity": int(baseline["capacity_scaled"]),
                    "rate": int(baseline["refill_rate_scaled"]),
                }
            for event in events:
                event_id = int(event["event_id"])
                delivery_id = str(event["delivery_id"])
                if int(event["charge_scaled"]) != TOKEN_SCALE:
                    errors.append(
                        {
                            "check": "ledger_charge",
                            "delivery_id": delivery_id,
                            "expected": TOKEN_SCALE,
                            "actual": int(event["charge_scaled"]),
                        }
                    )
                _verify_ledger_side(
                    errors,
                    expected=expected,
                    bucket_id="global",
                    event=event,
                    at_key="at_ms",
                    before_key="global_before_scaled",
                    refill_key="global_refill_scaled",
                    after_key="global_after_scaled",
                    last_before_key="global_last_before_ms",
                    last_after_key="global_last_after_ms",
                    rate_key="global_rate_scaled",
                    event_id=event_id,
                )
                consumer_bucket_id = f"consumer:{event['consumer_id']}"
                _verify_ledger_side(
                    errors,
                    expected=expected,
                    bucket_id=consumer_bucket_id,
                    event=event,
                    at_key="at_ms",
                    before_key="consumer_before_scaled",
                    refill_key="consumer_refill_scaled",
                    after_key="consumer_after_scaled",
                    last_before_key="consumer_last_before_ms",
                    last_after_key="consumer_last_after_ms",
                    rate_key="consumer_rate_scaled",
                    event_id=event_id,
                )
            for bucket_id in sorted(set(expected) | set(buckets)):
                if bucket_id not in expected:
                    errors.append(
                        {"check": "bucket_missing_baseline", "bucket_id": bucket_id}
                    )
                    continue
                if bucket_id not in buckets:
                    errors.append({"check": "bucket_missing", "bucket_id": bucket_id})
                    continue
                exp = expected[bucket_id]
                actual = buckets[bucket_id]
                if int(actual["balance_scaled"]) != exp["balance"]:
                    errors.append(
                        {
                            "check": "bucket_final_balance",
                            "bucket_id": bucket_id,
                            "expected": exp["balance"],
                            "actual": int(actual["balance_scaled"]),
                        }
                    )
                if int(actual["last_refill_ms"]) != exp["last"]:
                    errors.append(
                        {
                            "check": "bucket_final_refill",
                            "bucket_id": bucket_id,
                            "expected": exp["last"],
                            "actual": int(actual["last_refill_ms"]),
                        }
                    )
                if int(actual["capacity_scaled"]) != exp["capacity"]:
                    errors.append(
                        {
                            "check": "bucket_capacity",
                            "bucket_id": bucket_id,
                            "expected": exp["capacity"],
                            "actual": int(actual["capacity_scaled"]),
                        }
                    )
                if int(actual["refill_rate_scaled"]) != exp["rate"]:
                    errors.append(
                        {
                            "check": "bucket_rate",
                            "bucket_id": bucket_id,
                            "expected": exp["rate"],
                            "actual": int(actual["refill_rate_scaled"]),
                        }
                    )

            for wait in waits:
                global_wait = wait_ms_for_token(
                    int(wait["global_balance_scaled"]), int(wait["global_rate_scaled"])
                )
                consumer_wait = wait_ms_for_token(
                    int(wait["consumer_balance_scaled"]), int(wait["consumer_rate_scaled"])
                )
                expected_wait = max(global_wait, consumer_wait)
                if expected_wait != int(wait["needed_wait_ms"]):
                    errors.append(
                        {
                            "check": "token_wait_calculation",
                            "wait_id": int(wait["wait_id"]),
                            "expected": expected_wait,
                            "actual": int(wait["needed_wait_ms"]),
                        }
                    )
                if int(wait["global_rate_scaled"]) <= 0 or int(wait["consumer_rate_scaled"]) <= 0:
                    errors.append(
                        {"check": "token_wait_rate", "wait_id": int(wait["wait_id"])}
                    )

            deliveries_by_message: dict[str, list[sqlite3.Row]] = {}
            for delivery in deliveries:
                deliveries_by_message.setdefault(str(delivery["message_id"]), []).append(delivery)
            dead_by_message = {str(row["message_id"]): row for row in dead_letters}
            retry_by_delivery = {str(row["delivery_id"]): row for row in retry_events}
            outcome_by_delivery = {
                str(row["delivery_id"]): row
                for row in outcomes
                if row["delivery_id"] is not None
            }
            for message in messages:
                message_id = str(message["message_id"])
                message_deliveries = deliveries_by_message.get(message_id, [])
                for index, delivery in enumerate(message_deliveries, start=1):
                    if int(delivery["generation"]) != index:
                        errors.append(
                            {
                                "check": "generation_contiguous",
                                "message_id": message_id,
                                "expected": index,
                                "actual": int(delivery["generation"]),
                            }
                        )
                expected_generation = len(message_deliveries)
                if int(message["generation"]) != expected_generation:
                    errors.append(
                        {
                            "check": "message_generation",
                            "message_id": message_id,
                            "expected": expected_generation,
                            "actual": int(message["generation"]),
                        }
                    )
                state = str(message["state"])
                if state == "dead_letter" and message_id not in dead_by_message:
                    errors.append(
                        {"check": "dead_letter_tombstone_missing", "message_id": message_id}
                    )
                if state != "dead_letter" and message_id in dead_by_message:
                    errors.append(
                        {"check": "dead_letter_state_mismatch", "message_id": message_id, "state": state}
                    )
                if state == "confirmed":
                    delivered = [
                        delivery for delivery in message_deliveries if str(delivery["status"]) == "delivered"
                    ]
                    if not delivered or message["confirmed_by"] is None:
                        errors.append(
                            {"check": "confirmed_without_delivery", "message_id": message_id}
                        )
                if state in {"delivered", "inflight", "ready"} and message_id in dead_by_message:
                    errors.append(
                        {"check": "tombstone_for_live_message", "message_id": message_id}
                    )
                if state == "inflight":
                    started = [
                        delivery for delivery in message_deliveries if str(delivery["status"]) == "started"
                    ]
                    if len(started) != 1:
                        errors.append(
                            {
                                "check": "inflight_delivery_count",
                                "message_id": message_id,
                                "expected": 1,
                                "actual": len(started),
                            }
                        )
                if not message_deliveries:
                    continue
                last = message_deliveries[-1]
                last_status = str(last["status"])
                planned_outcome = str(last["planned_outcome"])
                expected_status = {
                    "delivered": "delivered",
                    "failed": "failed",
                    "rejected": "rejected",
                    "expired": "expired",
                }.get(planned_outcome)
                if last_status != "started" and expected_status != last_status:
                    errors.append(
                        {
                            "check": "delivery_status_plan",
                            "delivery_id": str(last["delivery_id"]),
                            "expected": expected_status,
                            "actual": last_status,
                        }
                    )
                outcome = outcome_by_delivery.get(str(last["delivery_id"]))
                if last_status != "started" and outcome is None:
                    errors.append(
                        {
                            "check": "delivery_outcome_plan_missing",
                            "delivery_id": str(last["delivery_id"]),
                        }
                    )
                if outcome is not None:
                    if int(outcome["generation"]) != int(last["generation"]):
                        errors.append(
                            {
                                "check": "delivery_outcome_generation",
                                "delivery_id": str(last["delivery_id"]),
                                "expected": int(last["generation"]),
                                "actual": int(outcome["generation"]),
                            }
                        )
                    if str(outcome["status"]) != last_status:
                        errors.append(
                            {
                                "check": "delivery_outcome_status",
                                "delivery_id": str(last["delivery_id"]),
                                "expected": last_status,
                                "actual": str(outcome["status"]),
                            }
                        )
                if last_status == "delivered":
                    if state not in {"delivered", "confirmed"}:
                        errors.append(
                            {
                                "check": "delivered_message_state",
                                "message_id": message_id,
                                "actual_state": state,
                            }
                        )
                    if state == "delivered" and int(last["confirmed"]) != 0:
                        errors.append(
                            {"check": "unconfirmed_delivery_flag", "delivery_id": str(last["delivery_id"])}
                        )
                    if state == "confirmed" and int(last["confirmed"]) != 1:
                        errors.append(
                            {"check": "confirmed_delivery_flag", "delivery_id": str(last["delivery_id"])}
                        )
                elif last_status == "failed":
                    retry = retry_by_delivery.get(str(last["delivery_id"]))
                    if int(last["generation"]) >= config.quota.max_delivery_generation:
                        if state != "dead_letter" or message_id not in dead_by_message:
                            errors.append(
                                {"check": "failed_limit_not_dead", "message_id": message_id}
                            )
                    else:
                        if retry is None:
                            errors.append(
                                {
                                    "check": "failed_retry_missing",
                                    "delivery_id": str(last["delivery_id"]),
                                }
                            )
                        elif state != "ready":
                            errors.append(
                                {
                                    "check": "failed_message_not_ready",
                                    "message_id": message_id,
                                    "actual_state": state,
                                }
                            )
                        elif int(message["available_at_ms"]) != int(retry["next_at_ms"]):
                            errors.append(
                                {
                                    "check": "failed_available_at",
                                    "message_id": message_id,
                                    "expected": int(retry["next_at_ms"]),
                                    "actual": int(message["available_at_ms"]),
                                }
                            )
                elif last_status in {"rejected", "expired"}:
                    if state != "dead_letter" or message_id not in dead_by_message:
                        errors.append(
                            {
                                "check": "terminal_delivery_not_dead",
                                "message_id": message_id,
                                "delivery_status": last_status,
                            }
                        )

            for tombstone in dead_letters:
                message_id = str(tombstone["message_id"])
                message = next((item for item in messages if str(item["message_id"]) == message_id), None)
                if message is None:
                    errors.append(
                        {"check": "tombstone_message_missing", "message_id": message_id}
                    )
                    continue
                if str(message["state"]) != "dead_letter":
                    errors.append(
                        {"check": "tombstone_message_not_dead", "message_id": message_id}
                    )
                if str(message["dead_letter_reason"] or "") != str(tombstone["reason"]):
                    errors.append(
                        {
                            "check": "tombstone_reason",
                            "message_id": message_id,
                            "expected": str(tombstone["reason"]),
                            "actual": message["dead_letter_reason"],
                        }
                    )
                if message["dead_letter_at_ms"] is not None and int(message["dead_letter_at_ms"]) != int(
                    tombstone["at_ms"]
                ):
                    errors.append(
                        {
                            "check": "tombstone_time",
                            "message_id": message_id,
                            "expected": int(tombstone["at_ms"]),
                            "actual": int(message["dead_letter_at_ms"]),
                        }
                    )
                if tombstone["delivery_id"] is not None:
                    delivery = next(
                        (
                            item
                            for item in deliveries
                            if str(item["delivery_id"]) == str(tombstone["delivery_id"])
                        ),
                        None,
                    )
                    if delivery is None:
                        errors.append(
                            {
                                "check": "tombstone_delivery_missing",
                                "message_id": message_id,
                                "delivery_id": str(tombstone["delivery_id"]),
                            }
                        )

            for retry in retry_events:
                message_id = str(retry["message_id"])
                message_deliveries = deliveries_by_message.get(message_id, [])
                generation = int(retry["generation"])
                later = [
                    delivery
                    for delivery in message_deliveries
                    if int(delivery["generation"]) > generation
                ]
                if later and int(later[0]["started_at_ms"]) < int(retry["next_at_ms"]):
                    errors.append(
                        {
                            "check": "retry_started_before_backoff",
                            "message_id": message_id,
                            "generation": generation,
                            "next_at_ms": int(retry["next_at_ms"]),
                            "started_at_ms": int(later[0]["started_at_ms"]),
                        }
                    )
                if int(retry["delay_ms"]) != min(
                    config.quota.retry_backoff_max_ms,
                    int(retry["capped_base_ms"]) + int(retry["jitter_ms"]),
                ):
                    errors.append(
                        {
                            "check": "retry_delay_formula",
                            "retry_id": int(retry["retry_id"]),
                        }
                    )
                if int(retry["next_at_ms"]) != int(retry["at_ms"]) + int(retry["delay_ms"]):
                    errors.append(
                        {
                            "check": "retry_next_at",
                            "retry_id": int(retry["retry_id"]),
                            "expected": int(retry["at_ms"]) + int(retry["delay_ms"]),
                            "actual": int(retry["next_at_ms"]),
                        }
                    )

            token_spend_by_consumer: dict[str, int] = {}
            for event in events:
                consumer_id = str(event["consumer_id"])
                token_spend_by_consumer[consumer_id] = (
                    token_spend_by_consumer.get(consumer_id, 0) + int(event["charge_scaled"])
                )
            for usage in usage_rows:
                consumer_id = str(usage["consumer_id"])
                if int(usage["tokens_spent_scaled"]) != token_spend_by_consumer.get(consumer_id, 0):
                    errors.append(
                        {
                            "check": "consumer_token_spend",
                            "consumer_id": consumer_id,
                            "expected": token_spend_by_consumer.get(consumer_id, 0),
                            "actual": int(usage["tokens_spent_scaled"]),
                        }
                    )
                for status_name, column_name in (
                    ("delivered", "delivered"),
                    ("failed", "failed"),
                    ("rejected", "rejected"),
                    ("expired", "expired"),
                ):
                    expected_count = sum(
                        1
                        for delivery in deliveries
                        if str(delivery["consumer_id"]) == consumer_id
                        and str(delivery["status"]) == status_name
                    )
                    if int(usage[column_name]) != expected_count:
                        errors.append(
                            {
                                "check": f"consumer_{column_name}",
                                "consumer_id": consumer_id,
                                "expected": expected_count,
                                "actual": int(usage[column_name]),
                            }
                        )
                expected_confirmed = sum(
                    1
                    for message in messages
                    if str(message["confirmed_by"] or "") == consumer_id
                )
                if int(usage["confirmed"]) != expected_confirmed:
                    errors.append(
                        {
                            "check": "consumer_confirmed",
                            "consumer_id": consumer_id,
                            "expected": expected_confirmed,
                            "actual": int(usage["confirmed"]),
                        }
                    )

            delivery_statuses: dict[str, int] = {}
            for delivery in deliveries:
                status = str(delivery["status"])
                delivery_statuses[status] = delivery_statuses.get(status, 0) + 1
            for usage in usage_rows:
                consumer_id = str(usage["consumer_id"])
                expected_attempts = sum(
                    1 for delivery in deliveries if str(delivery["consumer_id"]) == consumer_id
                )
                if int(usage["attempts"]) != expected_attempts:
                    errors.append(
                        {
                            "check": "consumer_attempts",
                            "consumer_id": consumer_id,
                            "expected": expected_attempts,
                            "actual": int(usage["attempts"]),
                        }
                    )

            outcome_delivery_ids = {
                str(row["delivery_id"]) for row in outcomes if row["delivery_id"] is not None
            }
            for delivery in deliveries:
                delivery_id = str(delivery["delivery_id"])
                if str(delivery["status"]) != "started" and delivery_id not in outcome_delivery_ids:
                    errors.append(
                        {"check": "delivery_outcome_missing", "delivery_id": delivery_id}
                    )
            for request in request_rows:
                if str(request["status"]) != "completed":
                    errors.append(
                        {
                            "check": "request_not_completed",
                            "request_id": str(request["request_id"]),
                            "status": str(request["status"]),
                        }
                    )
                request_outcomes = [
                    outcome
                    for outcome in outcomes
                    if str(outcome["request_id"]) == str(request["request_id"])
                ]
                if int(request["outcome_count"]) != len(request_outcomes):
                    errors.append(
                        {
                            "check": "request_outcome_count",
                            "request_id": str(request["request_id"]),
                            "expected": len(request_outcomes),
                            "actual": int(request["outcome_count"]),
                        }
                    )
                if len(request_outcomes) > int(request["limit_count"]):
                    errors.append(
                        {
                            "check": "request_outcome_overflow",
                            "request_id": str(request["request_id"]),
                            "limit": int(request["limit_count"]),
                            "actual": len(request_outcomes),
                        }
                    )
                for expected_sequence, outcome in enumerate(request_outcomes):
                    if int(outcome["sequence"]) != expected_sequence:
                        errors.append(
                            {
                                "check": "request_outcome_sequence",
                                "request_id": str(request["request_id"]),
                                "expected": expected_sequence,
                                "actual": int(outcome["sequence"]),
                            }
                        )

            return {
                "ok": not errors,
                "command": "verify",
                "config_version": config.quota.config_version,
                "queue_version": int(get_meta(conn, "queue_version") or "0"),
                "clock_watermark_ms": clock_watermark(conn),
                "checks": {
                    "ledger_events": len(events),
                    "token_waits": len(waits),
                    "retry_events": len(retry_events),
                    "messages": len(messages),
                    "deliveries": len(deliveries),
                    "delivery_statuses": dict(sorted(delivery_statuses.items())),
                    "dead_letters": len(dead_letters),
                    "requests": len(request_rows),
                    "outcomes": len(outcomes),
                    "buckets_replayed": len(expected),
                },
                "errors": errors,
            }
    finally:
        conn.close()


def _verify_ledger_side(
    errors: list[dict[str, Any]],
    *,
    expected: dict[str, dict[str, int]],
    bucket_id: str,
    event: sqlite3.Row,
    at_key: str,
    before_key: str,
    refill_key: str,
    after_key: str,
    last_before_key: str,
    last_after_key: str,
    rate_key: str,
    event_id: int,
) -> None:
    if bucket_id not in expected:
        errors.append(
            {"check": "ledger_bucket_missing", "bucket_id": bucket_id, "event_id": event_id}
        )
        return
    exp = expected[bucket_id]
    at_ms = int(event[at_key])
    before = int(event[before_key])
    after = int(event[after_key])
    last_before = int(event[last_before_key])
    last_after = int(event[last_after_key])
    rate = int(event[rate_key])
    if rate != exp["rate"]:
        errors.append(
            {
                "check": "ledger_rate",
                "bucket_id": bucket_id,
                "event_id": event_id,
                "expected": exp["rate"],
                "actual": rate,
            }
        )
    if last_before != exp["last"]:
        errors.append(
            {
                "check": "ledger_last_before",
                "bucket_id": bucket_id,
                "event_id": event_id,
                "expected": exp["last"],
                "actual": last_before,
            }
        )
    if before != exp["balance"]:
        errors.append(
            {
                "check": "ledger_before",
                "bucket_id": bucket_id,
                "event_id": event_id,
                "expected": exp["balance"],
                "actual": before,
            }
        )
    raw_refill = ((at_ms - exp["last"]) * exp["rate"]) // 1000
    expected_refill = min(exp["capacity"], exp["balance"] + raw_refill) - exp["balance"]
    if int(event[refill_key]) != expected_refill:
        errors.append(
            {
                "check": "ledger_refill",
                "bucket_id": bucket_id,
                "event_id": event_id,
                "expected": expected_refill,
                "actual": int(event[refill_key]),
            }
        )
    if after != before + expected_refill - TOKEN_SCALE:
        errors.append(
            {
                "check": "ledger_after",
                "bucket_id": bucket_id,
                "event_id": event_id,
                "expected": before + expected_refill - TOKEN_SCALE,
                "actual": after,
            }
        )
    if last_after != at_ms:
        errors.append(
            {
                "check": "ledger_last_after",
                "bucket_id": bucket_id,
                "event_id": event_id,
                "expected": at_ms,
                "actual": last_after,
            }
        )
    exp["balance"] = after
    exp["last"] = at_ms

