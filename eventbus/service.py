from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Callable

from .errors import EventBusError, fail
from .expr import compile_expression, evaluate_expression
from .models import (
    BridgeRule,
    Channel,
    Event,
    SampleBundle,
    Subscription,
    bridge_topological_order,
    check_bridge_cycles,
    config_digest,
    parse_event_input,
    parse_rule_input,
    validate_rule_set,
)
from .store import (
    advance_logical_clock,
    bump_commit_seq,
    compute_state_digest,
    connect,
    ensure_schema,
    get_meta,
    get_meta_int,
    get_operation,
    insert_operation,
    list_pending_operations,
    load_config,
    logical_clock_preflight,
    mark_operation_committed,
    mark_operation_failed,
    operation_input,
    operation_result,
    set_meta,
    transaction,
)
from .util import (
    FileLock,
    canonical_json,
    crash_point,
    digest_json,
    normalize_partition_key,
    parse_json_text,
    partition_for,
    sha256_text,
)


def _sha(value: Any) -> str:
    return "sha256:" + digest_json(value)


def _require_int(value: Any, code: str, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        fail(code, f"{field} must be an integer", field=field)
    return value


def _initial_chain(rule_id: str) -> str:
    return _sha({"initial": True, "rule_id": rule_id})


def _advance_chain(previous: str, action_digest: str, source_offset: int) -> str:
    return _sha(
        {
            "action": action_digest,
            "previous": previous,
            "source_offset": source_offset,
        }
    )


def _action_digest(
    rule_id: str,
    source_offset: int,
    source_digest: str,
    status: str,
    transformed_digest: str | None,
    downstream_offset: int | None,
    downstream_config_digest: str | None,
) -> str:
    return _sha(
        {
            "downstream_config_digest": downstream_config_digest,
            "downstream_offset": downstream_offset,
            "rule_id": rule_id,
            "source_digest": source_digest,
            "source_offset": source_offset,
            "status": status,
            "transformed_digest": transformed_digest,
        }
    )


def _schema_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    return row is not None


def _row_event(row: sqlite3.Row | dict[str, Any]) -> Event:
    payload = parse_json_text(str(row["payload_json"]), "source_events.payload")
    partition_key = parse_json_text(
        str(row["partition_key_json"]), "source_events.partition_key"
    )
    return Event(
        seq=int(row["seq"]),
        topic=str(row["topic"]),
        partition_key=partition_key,
        payload=payload,
        clock_ms=int(row["clock_ms"]),
    )


def _record_payload(row: sqlite3.Row) -> dict[str, Any]:
    if row["payload_json"] is None:
        fail("E_VERIFY", "live record has no payload", offset=int(row["offset"]))
    value = parse_json_text(str(row["payload_json"]), "records.payload")
    if not isinstance(value, dict):
        fail("E_VERIFY", "record payload is not an object")
    return value


class EventBusService:
    def __init__(
        self,
        db_path: str | Path,
        *,
        lock_timeout: float = 20.0,
    ) -> None:
        self.db_path = Path(db_path)
        self.lock_timeout = lock_timeout

    @property
    def lock_path(self) -> Path:
        return self.db_path.with_name(self.db_path.name + ".lock")

    def load(
        self,
        bundle: SampleBundle,
        clock_ms: int,
        *,
        expected_digest: str | None = None,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        if expected_digest is not None and bundle.digest != expected_digest:
            fail(
                "E_SAMPLE_DIGEST",
                "sample digest does not match the expected digest",
                expected=expected_digest,
                actual=bundle.digest,
            )
        input_value = {
            "clock_ms": clock_ms,
            "bundle": bundle.to_dict(),
            "expected_digest": expected_digest,
        }
        return self._run_operation(
            "load",
            input_value,
            self._do_load,
            prepare=self._prepare_load,
            op_key_override=operation_key or f"load:{bundle.digest}",
            expected_digest=None,
        )

    def publish(
        self,
        clock_ms: int,
        *,
        expected_digest: str | None = None,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        input_value = {"clock_ms": clock_ms}
        return self._run_operation(
            "publish",
            input_value,
            self._do_publish,
            prepare=self._prepare_publish,
            op_key_override=operation_key,
            expected_digest=expected_digest,
        )

    def bridge(
        self,
        clock_ms: int,
        *,
        rule_ids: list[str] | None = None,
        expected_digest: str | None = None,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        input_value = {
            "clock_ms": clock_ms,
            "rule_ids": None if rule_ids is None else sorted(set(rule_ids)),
        }
        return self._run_operation(
            "bridge",
            input_value,
            self._do_bridge,
            prepare=self._prepare_bridge,
            op_key_override=operation_key,
            expected_digest=expected_digest,
        )

    def consume(
        self,
        consumer_id: str,
        clock_ms: int,
        *,
        budget: int | None = None,
        report_offset: int | None = None,
        expected_digest: str | None = None,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        input_value = {
            "clock_ms": clock_ms,
            "consumer_id": consumer_id,
            "budget": budget,
            "report_offset": report_offset,
        }
        return self._run_operation(
            "consume",
            input_value,
            self._do_consume,
            prepare=self._prepare_consume,
            op_key_override=operation_key,
            expected_digest=expected_digest,
        )

    def cleanup(
        self,
        clock_ms: int,
        *,
        expected_digest: str | None = None,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        input_value = {"clock_ms": clock_ms}
        return self._run_operation(
            "cleanup",
            input_value,
            self._do_cleanup,
            prepare=self._prepare_cleanup,
            op_key_override=operation_key,
            expected_digest=expected_digest,
        )

    def edit(
        self,
        kind: str,
        action: str,
        object_id: Any,
        data: dict[str, Any] | None,
        clock_ms: int,
        *,
        expected_digest: str | None = None,
        operation_key: str | None = None,
    ) -> dict[str, Any]:
        if kind not in {"event", "rule"}:
            fail("E_EDIT", "edit kind must be 'event' or 'rule'", kind=kind)
        if action not in {"add", "update", "delete"}:
            fail("E_EDIT", "edit action must be add, update, or delete", action=action)
        input_value = {
            "action": action,
            "clock_ms": clock_ms,
            "data": data,
            "kind": kind,
            "object_id": object_id,
        }
        return self._run_operation(
            f"edit_{kind}",
            input_value,
            self._do_edit,
            prepare=self._prepare_edit,
            op_key_override=operation_key,
            expected_digest=expected_digest,
        )

    def resume(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "ok": True,
                "command": "resume",
                "replayed": [],
                "failed": [],
                "pending_remaining": 0,
            }
        with FileLock(self.lock_path, self.lock_timeout):
            conn = connect(self.db_path)
            try:
                if not _schema_exists(conn):
                    return {
                        "ok": True,
                        "command": "resume",
                        "replayed": [],
                        "failed": [],
                        "pending_remaining": 0,
                    }
                ensure_schema(conn)
                pending = list_pending_operations(conn)
                replayed: list[str] = []
                failed_ops: list[dict[str, Any]] = []
                for row in pending:
                    op_key = str(row["op_key"])
                    kind = str(row["kind"])
                    input_value = operation_input(row)
                    try:
                        result = self._execute_pending(
                            conn, op_key, kind, input_value
                        )
                        replayed.append(str(result.get("operation_key", op_key)))
                    except EventBusError as exc:
                        failed_ops.append(
                            {
                                "operation_key": op_key,
                                "error": {
                                    "code": exc.code,
                                    "message": exc.message,
                                    "details": exc.details,
                                },
                            }
                        )
                remaining = list_pending_operations(conn)
                return {
                    "ok": not failed_ops,
                    "command": "resume",
                    "failed": sorted(failed_ops, key=lambda item: item["operation_key"]),
                    "pending_remaining": len(remaining),
                    "replayed": sorted(replayed),
                }
            finally:
                conn.close()

    def status(self) -> dict[str, Any]:
        if not self.db_path.exists():
            fail("E_CONFIG", "event bus is not initialized", db=str(self.db_path))
        conn = connect(self.db_path, readonly=True)
        try:
            if not _schema_exists(conn):
                fail("E_CONFIG", "event bus is not initialized")
            with transaction(conn, immediate=False):
                return self._status_in_tx(conn)
        finally:
            conn.close()

    def verify(
        self,
        *,
        sample_bundle: SampleBundle | None = None,
        expect_digest: str | None = None,
        status_file: str | Path | None = None,
    ) -> dict[str, Any]:
        if not self.db_path.exists():
            fail("E_CONFIG", "event bus is not initialized", db=str(self.db_path))
        conn = connect(self.db_path, readonly=True)
        try:
            if not _schema_exists(conn):
                fail("E_CONFIG", "event bus is not initialized")
            with transaction(conn, immediate=False):
                result = self._verify_in_tx(conn)
                try:
                    actual_status = self._status_in_tx(conn)
                except EventBusError:
                    actual_status = None
        finally:
            conn.close()

        errors: list[dict[str, Any]] = list(result["errors"])
        actual_status_json = (
            None if actual_status is None else canonical_json(actual_status)
        )
        actual_status_digest = (
            None
            if actual_status_json is None
            else "sha256:" + sha256_text(actual_status_json)
        )
        if sample_bundle is not None:
            actual = getattr(self, "_last_sample_digest", None)
            if actual != sample_bundle.digest:
                errors.append(
                    {
                        "code": "E_SAMPLE_DIGEST",
                        "message": "sample digest differs from persisted sample",
                        "expected": sample_bundle.digest,
                        "actual": actual,
                    }
                )
        if expect_digest is not None:
            actual = getattr(self, "_last_sample_digest", None)
            if actual != expect_digest:
                errors.append(
                    {
                        "code": "E_SAMPLE_DIGEST",
                        "message": "expected sample digest does not match",
                        "expected": expect_digest,
                        "actual": actual,
                    }
                )
        if status_file is not None:
            path = Path(status_file)
            try:
                supplied = parse_json_text(
                    path.read_text(encoding="utf-8"), str(path)
                )
            except OSError as exc:
                errors.append(
                    {
                        "code": "E_VERIFY",
                        "message": "cannot read status file",
                        "path": str(path),
                        "reason": str(exc),
                    }
                )
            else:
                if actual_status_json is None:
                    errors.append(
                        {
                            "code": "E_VERIFY",
                            "message": "cannot reconstruct status for output comparison",
                        }
                    )
                else:
                    supplied_digest = (
                        supplied.get("state_digest")
                        if isinstance(supplied, dict)
                        else None
                    )
                    supplied_json = canonical_json(supplied)
                    if (
                        supplied_digest != result["state_digest"]
                        or supplied_json != actual_status_json
                    ):
                        errors.append(
                            {
                                "code": "E_VERIFY",
                                "message": "status output is inconsistent with persisted state",
                                "expected": result["state_digest"],
                                "actual": supplied_digest,
                                "actual_status_digest": actual_status_digest,
                                "supplied_status_digest": "sha256:"
                                + sha256_text(supplied_json),
                            }
                        )
        result["errors"] = sorted(
            errors,
            key=lambda item: (
                str(item.get("code", "")),
                str(item.get("message", "")),
                canonical_json(item),
            ),
        )
        result["checks"]["output_consistency"] = not any(
            "status output" in str(item.get("message", ""))
            for item in result["errors"]
        )
        if sample_bundle is not None or expect_digest is not None:
            result["checks"]["sample_digest_match"] = not any(
                item.get("code") == "E_SAMPLE_DIGEST"
                for item in result["errors"]
            )
        result["ok"] = not result["errors"]
        result["command"] = "verify"
        return result

    def _run_operation(
        self,
        kind: str,
        input_value: dict[str, Any],
        executor: Callable[
            [sqlite3.Connection, dict[str, Any], str], dict[str, Any]
        ],
        *,
        prepare: (
            Callable[[sqlite3.Connection, dict[str, Any]], dict[str, Any]] | None
        ) = None,
        op_key_override: str | None = None,
        expected_digest: str | None = None,
    ) -> dict[str, Any]:
        clock_ms = _require_int(input_value.get("clock_ms"), "E_BAD_CLOCK", "clock_ms")
        if clock_ms < 0:
            fail("E_BAD_CLOCK", "logical clock must be non-negative")
        with FileLock(self.lock_path, self.lock_timeout):
            conn = connect(self.db_path)
            try:
                if kind != "load" and not _schema_exists(conn):
                    fail("E_CONFIG", "event bus is not initialized")
                ensure_schema(conn)
                if prepare is not None:
                    input_value = prepare(conn, input_value)
                clock_ms = _require_int(
                    input_value.get("clock_ms"), "E_BAD_CLOCK", "clock_ms"
                )
                logical_clock_preflight(conn, clock_ms, expected_digest)
                op_key = op_key_override or (
                    f"{kind}:{digest_json({'input': input_value})[:32]}"
                )
                row = get_operation(conn, op_key)
                if row is not None:
                    if str(row["kind"]) != kind:
                        fail(
                            "E_OP_MISMATCH",
                            "operation key is already bound to another command kind",
                            operation_key=op_key,
                            existing_kind=str(row["kind"]),
                            requested_kind=kind,
                        )
                    if row["state"] == "committed":
                        result = operation_result(row)
                        if result is None:
                            fail(
                                "E_OP_MISMATCH",
                                "committed operation has no stored result",
                                operation_key=op_key,
                            )
                        return result
                    if row["state"] == "pending":
                        kind = str(row["kind"])
                        input_value = operation_input(row)
                    else:
                        conn.execute(
                            "UPDATE operations SET state='pending', result_json=NULL, "
                            "error_json=NULL, input_json=?, input_digest=?, clock_ms=?, "
                            "updated_clock=? WHERE op_key=?",
                            (
                                canonical_json(input_value),
                                digest_json(input_value),
                                clock_ms,
                                clock_ms,
                                op_key,
                            ),
                        )
                else:
                    pending = list_pending_operations(conn)
                    if pending:
                        fail(
                            "E_OPERATION_PENDING",
                            "an unfinished operation must be resumed first",
                            pending=[str(item["op_key"]) for item in pending],
                        )
                    insert_operation(conn, op_key, kind, input_value, clock_ms)
                crash_point("after_intent")
                return self._execute_pending(conn, op_key, kind, input_value)
            finally:
                conn.close()

    def _execute_pending(
        self,
        conn: sqlite3.Connection,
        op_key: str,
        kind: str,
        input_value: dict[str, Any],
    ) -> dict[str, Any]:
        clock_ms = _require_int(input_value.get("clock_ms"), "E_BAD_CLOCK", "clock_ms")
        dispatch: dict[
            str, Callable[[sqlite3.Connection, dict[str, Any], str], dict[str, Any]]
        ] = {
            "bridge": self._do_bridge,
            "cleanup": self._do_cleanup,
            "consume": self._do_consume,
            "edit_event": self._do_edit,
            "edit_rule": self._do_edit,
            "load": self._do_load,
            "publish": self._do_publish,
        }
        executor = dispatch.get(kind)
        if executor is None:
            fail("E_OP_MISMATCH", "unknown operation kind", kind=kind, op_key=op_key)
        try:
            with transaction(conn):
                current = get_operation(conn, op_key)
                if current is None:
                    fail(
                        "E_OP_MISMATCH",
                        "operation intent disappeared",
                        operation_key=op_key,
                    )
                if current["state"] != "pending":
                    result = operation_result(current)
                    if result is not None:
                        return result
                    fail(
                        "E_OP_MISMATCH",
                        "operation is not pending",
                        operation_key=op_key,
                        state=str(current["state"]),
                    )
                logical_clock_preflight(conn, clock_ms, None)
                result = executor(conn, input_value, op_key)
                result = {
                    "ok": True,
                    **result,
                    "operation_key": op_key,
                }
                advance_logical_clock(conn, clock_ms)
                bump_commit_seq(conn)
                crash_point("before_commit")
                mark_operation_committed(conn, op_key, result, clock_ms)
                return result
        except EventBusError as exc:
            try:
                mark_operation_failed(
                    conn,
                    op_key,
                    {"code": exc.code, "message": exc.message, "details": exc.details},
                    clock_ms,
                )
            except sqlite3.Error:
                pass
            raise
        except sqlite3.Error as exc:
            error = EventBusError(
                "E_DB", "SQLite operation failed", {"reason": str(exc)}
            )
            try:
                mark_operation_failed(
                    conn,
                    op_key,
                    {"code": error.code, "message": error.message, "details": error.details},
                    clock_ms,
                )
            except sqlite3.Error:
                pass
            raise error
        except Exception as exc:
            error = EventBusError(
                "E_INTERNAL", "unexpected internal error", {"reason": repr(exc)}
            )
            try:
                mark_operation_failed(
                    conn,
                    op_key,
                    {"code": error.code, "message": error.message, "details": error.details},
                    clock_ms,
                )
            except sqlite3.Error:
                pass
            raise error

    # ------------------------------------------------------------------ load

    def _prepare_load(
        self, conn: sqlite3.Connection, input_value: dict[str, Any]
    ) -> dict[str, Any]:
        bundle_raw = input_value.get("bundle")
        if not isinstance(bundle_raw, dict) or "digest" not in bundle_raw:
            fail("E_OP_MISMATCH", "load input has no sample bundle")
        digest = str(bundle_raw["digest"])
        pointer = conn.execute(
            "SELECT version FROM config_pointer WHERE id=1"
        ).fetchone()
        if pointer is not None:
            stored_digest = get_meta(conn, "sample_digest")
            if stored_digest != digest:
                fail(
                    "E_SAMPLE_DIGEST",
                    "database already contains a different sample",
                    expected=digest,
                    actual=stored_digest,
                )
        return input_value

    def _do_load(
        self,
        conn: sqlite3.Connection,
        input_value: dict[str, Any],
        op_key: str,
    ) -> dict[str, Any]:
        clock_ms = int(input_value["clock_ms"])
        bundle_raw = input_value.get("bundle")
        if not isinstance(bundle_raw, dict):
            fail("E_OP_MISMATCH", "load intent has no bundle")
        channels = [
            Channel(
                name=item["name"],
                topic=item["topic"],
                partitions=int(item["partitions"]),
                retention_bytes=int(item["retention_bytes"]),
                max_batch_records=int(item["max_batch_records"]),
            )
            for item in bundle_raw["channels"]
        ]
        rules = [
            BridgeRule(
                id=item["id"],
                upstream=item["upstream"],
                downstream=item["downstream"],
                filter_expr=item["filter"],
                transform_expr=item["transform"],
                depends_upstream_offset=int(item["depends_upstream_offset"]),
            )
            for item in bundle_raw["rules"]
        ]
        events = [
            Event(
                seq=int(item["seq"]),
                topic=item["topic"],
                partition_key=item["partition_key"],
                payload=item["payload"],
                clock_ms=int(item["clock_ms"]),
            )
            for item in bundle_raw["events"]
        ]
        subscriptions = [
            Subscription(
                consumer_id=item["consumer_id"],
                channel=item["channel"],
                start_offset=int(item["start_offset"]),
                budget=int(item["budget"]),
            )
            for item in bundle_raw["subscriptions"]
        ]
        digest = str(bundle_raw["digest"])

        pointer = conn.execute("SELECT version FROM config_pointer WHERE id=1").fetchone()
        if pointer is not None:
            stored_digest = get_meta(conn, "sample_digest")
            if stored_digest != digest:
                fail(
                    "E_SAMPLE_DIGEST",
                    "database already contains a different sample",
                    expected=digest,
                    actual=stored_digest,
                )
            return self._load_state_summary(conn, digest, already_loaded=True)

        version = 1
        config_digest_value = config_digest(channels, rules, subscriptions)
        conn.execute(
            "INSERT INTO config_versions("
            "version, digest, catalog_json, rules_json, subscriptions_json, "
            "created_clock, edit_version"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                version,
                config_digest_value,
                canonical_json([channel.to_dict() for channel in channels]),
                canonical_json([rule.to_dict() for rule in rules]),
                canonical_json(
                    [subscription.to_dict() for subscription in subscriptions]
                ),
                clock_ms,
                get_meta_int(conn, "edit_version", 0),
            ),
        )
        for channel in channels:
            conn.execute(
                "INSERT INTO channel_versions("
                "config_version, name, topic, partitions, retention_bytes, "
                "max_batch_records, digest"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    version,
                    channel.name,
                    channel.topic,
                    channel.partitions,
                    channel.retention_bytes,
                    channel.max_batch_records,
                    channel.digest,
                ),
            )
            conn.execute(
                "INSERT INTO channels(name, next_offset, cleaned_through, updated_clock) "
                "VALUES(?, 0, 0, ?)",
                (channel.name, clock_ms),
            )
        for rule in rules:
            conn.execute(
                "INSERT INTO rule_versions("
                "config_version, rule_id, upstream, downstream, filter_expr, "
                "transform_expr, depends_upstream_offset, digest"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    version,
                    rule.id,
                    rule.upstream,
                    rule.downstream,
                    rule.filter_expr,
                    rule.transform_expr,
                    rule.depends_upstream_offset,
                    rule.digest,
                ),
            )
        conn.execute(
            "INSERT INTO config_pointer(id, version) VALUES(1, ?)", (version,)
        )
        for event in events:
            conn.execute(
                "INSERT INTO source_events("
                "seq, topic, partition_key_json, payload_json, clock_ms, state, "
                "revision_version, created_clock, updated_clock"
                ") VALUES(?, ?, ?, ?, ?, 'active', 0, ?, ?)",
                (
                    event.seq,
                    event.topic,
                    canonical_json(event.partition_key),
                    canonical_json(event.payload),
                    event.clock_ms,
                    clock_ms,
                    clock_ms,
                ),
            )
        for subscription in subscriptions:
            conn.execute(
                "INSERT INTO subscriptions("
                "consumer_id, channel, start_offset, budget, current_offset, "
                "updated_clock, config_version"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    subscription.consumer_id,
                    subscription.channel,
                    subscription.start_offset,
                    subscription.budget,
                    subscription.start_offset,
                    clock_ms,
                    version,
                ),
            )
        set_meta(conn, "sample_digest", digest)
        crash_point("after_load_state")
        append_result = self._append_pending_events(conn, op_key, clock_ms)
        max_event_clock = max((event.clock_ms for event in events), default=0)
        advance_logical_clock(conn, max(clock_ms, max_event_clock))
        retention = self._apply_retention(
            conn, clock_ms, op_key, version, channels, record_within_limit=False
        )
        result = {
            "command": "load",
            "already_loaded": False,
            "appended": append_result["appended"],
            "batches": append_result["batches"],
            "channels": self._channel_summaries(conn, channels),
            "config_version": version,
            "sample_digest": digest,
        }
        if retention is not None:
            result["cleanup"] = retention["summary"]
        return result

    def _load_state_summary(
        self, conn: sqlite3.Connection, digest: str, *, already_loaded: bool
    ) -> dict[str, Any]:
        version, _, channels, _, _, _ = load_config(conn)
        return {
            "command": "load",
            "already_loaded": already_loaded,
            "appended": 0,
            "batches": [],
            "channels": self._channel_summaries(conn, channels),
            "config_version": version,
            "sample_digest": digest,
        }

    # --------------------------------------------------------------- publish

    def _prepare_publish(
        self, conn: sqlite3.Connection, input_value: dict[str, Any]
    ) -> dict[str, Any]:
        version, digest, channels, _, _, _ = load_config(conn)
        rows = conn.execute(
            "SELECT seq, topic, payload_json, clock_ms FROM source_events "
            "WHERE state='active' AND seq NOT IN (SELECT seq FROM published_events) "
            "ORDER BY seq"
        ).fetchall()
        input_value = dict(input_value)
        input_value["bound_config_version"] = version
        input_value["bound_config_digest"] = digest
        input_value["pending_events"] = [
            {
                "clock_ms": int(row["clock_ms"]),
                "seq": int(row["seq"]),
                "topic": str(row["topic"]),
            }
            for row in rows
        ]
        input_value["channels"] = [channel.to_dict() for channel in channels]
        return input_value

    def _do_publish(
        self,
        conn: sqlite3.Connection,
        input_value: dict[str, Any],
        op_key: str,
    ) -> dict[str, Any]:
        clock_ms = int(input_value["clock_ms"])
        version, digest, channels, _, _, _ = load_config(conn)
        if version != int(input_value["bound_config_version"]):
            fail(
                "E_RULE_VERSION_CHANGED",
                "configuration changed after the publish intent was bound",
                bound=int(input_value["bound_config_version"]),
                current=version,
            )
        if digest != input_value["bound_config_digest"]:
            fail("E_RULE_VERSION_CHANGED", "configuration digest changed")
        result = self._append_pending_events(conn, op_key, clock_ms)
        max_event_clock = max(
            (
                int(item["clock_ms"])
                for item in input_value.get("pending_events", [])
            ),
            default=0,
        )
        advance_logical_clock(conn, max(clock_ms, max_event_clock))
        retention = self._apply_retention(
            conn, clock_ms, op_key, version, channels, record_within_limit=False
        )
        result = {
            "command": "publish",
            **result,
            "channels": self._channel_summaries(conn, channels),
            "config_version": version,
        }
        if retention is not None:
            result["cleanup"] = retention["summary"]
        return result

    def _append_pending_events(
        self,
        conn: sqlite3.Connection,
        op_key: str,
        clock_ms: int,
    ) -> dict[str, Any]:
        channels = {
            channel.name: channel for channel in load_config(conn)[2]
        }
        channel_order = sorted(channels)
        rows = conn.execute(
            "SELECT * FROM source_events "
            "WHERE state='active' AND seq NOT IN (SELECT seq FROM published_events) "
            "ORDER BY seq"
        ).fetchall()
        by_channel: dict[str, list[sqlite3.Row]] = {name: [] for name in channel_order}
        topic_to_channel = {
            channel.topic: channel.name for channel in channels.values()
        }
        for row in rows:
            channel_name = topic_to_channel.get(str(row["topic"]))
            if channel_name is None:
                fail("E_BAD_CHANNEL", "event topic has no current channel")
            by_channel[channel_name].append(row)

        appended = 0
        skipped = 0
        batches: list[dict[str, Any]] = []
        for channel_name in channel_order:
            channel = channels[channel_name]
            channel_rows = by_channel[channel_name]
            for index in range(0, len(channel_rows), channel.max_batch_records):
                chunk = channel_rows[index : index + channel.max_batch_records]
                event_descriptors: list[dict[str, Any]] = []
                for row in chunk:
                    event = _row_event(row)
                    event_descriptors.append(
                        {
                            "seq": event.seq,
                            "source_digest": event.source_digest,
                        }
                    )
                batch_key = _sha(
                    {
                        "channel": channel.name,
                        "channel_config_digest": channel.digest,
                        "events": event_descriptors,
                    }
                )
                existing = conn.execute(
                    "SELECT * FROM append_batches WHERE batch_key=?", (batch_key,)
                ).fetchone()
                if existing is not None:
                    skipped += len(chunk)
                    continue
                state_row = conn.execute(
                    "SELECT next_offset FROM channels WHERE name=?", (channel.name,)
                ).fetchone()
                if state_row is None:
                    fail("E_BAD_CHANNEL", "channel runtime state is missing")
                start_offset = int(state_row["next_offset"])
                offset = start_offset
                for row in chunk:
                    event = _row_event(row)
                    already = conn.execute(
                        "SELECT 1 FROM published_events WHERE seq=?", (event.seq,)
                    ).fetchone()
                    if already is not None:
                        skipped += 1
                        continue
                    normalized_key = event.normalized_key
                    partition = partition_for(normalized_key, channel.partitions)
                    payload_json = canonical_json(event.payload)
                    origin_ref = f"{event.topic}:{event.seq}"
                    record_size = len(
                        canonical_json(
                            {
                                "channel": channel.name,
                                "clock_ms": event.clock_ms,
                                "offset": offset,
                                "origin_ref": origin_ref,
                                "origin_type": "producer",
                                "partition": partition,
                                "partition_key": event.partition_key,
                                "payload": event.payload,
                            }
                        ).encode("utf-8")
                    )
                    conn.execute(
                        "INSERT INTO records("
                        "channel, offset, partition, partition_key_json, payload_json, "
                        "payload_digest, source_digest, clock_ms, record_bytes, state, "
                        "origin_type, origin_ref, config_digest, created_clock"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', 'producer', ?, ?, ?)",
                        (
                            channel.name,
                            offset,
                            partition,
                            canonical_json(event.partition_key),
                            payload_json,
                            _sha(event.payload),
                            event.source_digest,
                            event.clock_ms,
                            record_size,
                            origin_ref,
                            channel.digest,
                            clock_ms,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO published_events("
                        "seq, topic, channel, record_offset, payload_digest, "
                        "source_digest, published_clock"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                        (
                            event.seq,
                            event.topic,
                            channel.name,
                            offset,
                            _sha(event.payload),
                            event.source_digest,
                            clock_ms,
                        ),
                    )
                    offset += 1
                    appended += 1
                if offset > start_offset:
                    end_offset = offset
                    batch_digest = _sha(
                        {
                            "channel": channel.name,
                            "event_digests": [
                                item["source_digest"] for item in event_descriptors
                            ],
                        }
                    )
                    conn.execute(
                        "INSERT INTO append_batches("
                        "batch_key, channel, start_offset, end_offset, event_count, "
                        "batch_digest, config_version, clock_ms, operation_key"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            batch_key,
                            channel.name,
                            start_offset,
                            end_offset,
                            end_offset - start_offset,
                            batch_digest,
                            load_config(conn)[0],
                            clock_ms,
                            op_key,
                        ),
                    )
                    conn.execute(
                        "UPDATE channels SET next_offset=?, updated_clock=? "
                        "WHERE name=?",
                        (end_offset, clock_ms, channel.name),
                    )
                    batches.append(
                        {
                            "batch_digest": batch_digest,
                            "batch_key": batch_key,
                            "channel": channel.name,
                            "end_offset": end_offset,
                            "event_count": end_offset - start_offset,
                            "event_seqs": [
                                item["seq"] for item in event_descriptors
                            ],
                            "start_offset": start_offset,
                        }
                    )
                    crash_point("after_append_batch")
        return {
            "appended": appended,
            "batches": sorted(
                batches, key=lambda item: (item["channel"], item["start_offset"])
            ),
            "skipped": skipped,
        }

    # ---------------------------------------------------------------- bridge

    def _prepare_bridge(
        self, conn: sqlite3.Connection, input_value: dict[str, Any]
    ) -> dict[str, Any]:
        version, digest, channels, rules, _, _ = load_config(conn)
        requested = input_value.get("rule_ids")
        by_id = {rule.id: rule for rule in rules}
        if requested:
            unknown = sorted(set(requested) - set(by_id))
            if unknown:
                fail("E_NOT_FOUND", "bridge rule does not exist", rules=unknown)
            selected = [by_id[rule_id] for rule_id in sorted(set(requested))]
        else:
            selected = rules
        input_value = dict(input_value)
        input_value["bound_config_version"] = version
        input_value["bound_config_digest"] = digest
        input_value["channels"] = [channel.to_dict() for channel in channels]
        input_value["rules"] = [rule.to_dict() for rule in selected]
        input_value["bound_ends"] = {
            str(row["name"]): int(row["next_offset"])
            for row in conn.execute(
                "SELECT name, next_offset FROM channels ORDER BY name"
            ).fetchall()
        }
        input_value["bridge_positions"] = {
            str(row["rule_id"]): int(row["next_offset"])
            for row in conn.execute(
                "SELECT rule_id, next_offset FROM bridge_state ORDER BY rule_id"
            ).fetchall()
        }
        return input_value

    def _do_bridge(
        self,
        conn: sqlite3.Connection,
        input_value: dict[str, Any],
        op_key: str,
    ) -> dict[str, Any]:
        clock_ms = int(input_value["clock_ms"])
        version, digest, current_channels, _, _, _ = load_config(conn)
        bound_version = int(input_value["bound_config_version"])
        if version != bound_version or digest != input_value["bound_config_digest"]:
            fail(
                "E_RULE_VERSION_CHANGED",
                "configuration changed after the bridge intent was bound",
                bound_version=bound_version,
                current_version=version,
            )
        channels = {
            item["name"]: Channel(
                name=item["name"],
                topic=item["topic"],
                partitions=int(item["partitions"]),
                retention_bytes=int(item["retention_bytes"]),
                max_batch_records=int(item["max_batch_records"]),
            )
            for item in input_value["channels"]
        }
        rules = [
            BridgeRule(
                id=item["id"],
                upstream=item["upstream"],
                downstream=item["downstream"],
                filter_expr=item["filter"],
                transform_expr=item["transform"],
                depends_upstream_offset=int(item["depends_upstream_offset"]),
            )
            for item in input_value["rules"]
        ]
        check_bridge_cycles(rules)
        ordered_rules = bridge_topological_order(rules)
        results: list[dict[str, Any]] = []
        for rule in ordered_rules:
            if rule.upstream not in channels or rule.downstream not in channels:
                fail(
                    "E_BAD_CHANNEL",
                    "bridge rule references a missing channel",
                    rule=rule.id,
                )
            upstream = channels[rule.upstream]
            downstream = channels[rule.downstream]
            source_state = conn.execute(
                "SELECT next_offset FROM channels WHERE name=?", (upstream.name,)
            ).fetchone()
            target_state = conn.execute(
                "SELECT next_offset FROM channels WHERE name=?", (downstream.name,)
            ).fetchone()
            if source_state is None or target_state is None:
                fail("E_BAD_CHANNEL", "channel runtime state is missing")
            source_end = int(source_state["next_offset"])
            if source_end < rule.depends_upstream_offset:
                fail(
                    "E_UPSTREAM_WATERMARK_NOT_READY",
                    "upstream committed watermark is below the rule dependency",
                    rule=rule.id,
                    source_end=source_end,
                    depends_upstream_offset=rule.depends_upstream_offset,
                )
            state = conn.execute(
                "SELECT * FROM bridge_state WHERE rule_id=?", (rule.id,)
            ).fetchone()
            if state is None:
                chain_digest = _initial_chain(rule.id)
                next_offset = 0
                conn.execute(
                    "INSERT INTO bridge_state("
                    "rule_id, upstream_channel, downstream_channel, next_offset, "
                    "last_source_offset, chain_digest, rules_version, rules_digest, "
                    "upstream_config_digest, downstream_config_digest, "
                    "last_upstream_end, updated_clock"
                    ") VALUES(?, ?, ?, 0, NULL, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rule.id,
                        upstream.name,
                        downstream.name,
                        chain_digest,
                        version,
                        rule.digest,
                        upstream.digest,
                        downstream.digest,
                        source_end,
                        clock_ms,
                    ),
                )
            else:
                chain_digest = str(state["chain_digest"])
                next_offset = int(state["next_offset"])
                if next_offset > 0 and (
                    str(state["upstream_channel"]) != upstream.name
                    or str(state["downstream_channel"]) != downstream.name
                ):
                    fail(
                        "E_CHANNEL_VERSION_MISMATCH",
                        "bridge rule changed an endpoint after forwarding began",
                        rule=rule.id,
                        bridge_offset=next_offset,
                        old_upstream=str(state["upstream_channel"]),
                        new_upstream=upstream.name,
                        old_downstream=str(state["downstream_channel"]),
                        new_downstream=downstream.name,
                    )
            if next_offset > source_end:
                fail(
                    "E_UPSTREAM_WATERMARK_REGRESSION",
                    "bridge position is beyond the committed upstream watermark",
                    rule=rule.id,
                    bridge_offset=next_offset,
                    source_end=source_end,
                )
            if next_offset < source_end:
                first = conn.execute(
                    "SELECT config_digest FROM records WHERE channel=? AND offset=?",
                    (upstream.name, next_offset),
                ).fetchone()
                if first is None:
                    fail(
                        "E_UPSTREAM_WATERMARK_REGRESSION",
                        "upstream event is no longer available",
                        rule=rule.id,
                        offset=next_offset,
                    )
                if str(first["config_digest"]) != upstream.digest:
                    fail(
                        "E_CHANNEL_VERSION_MISMATCH",
                        "upstream channel configuration differs from the record version",
                        rule=rule.id,
                        offset=next_offset,
                    )
            conn.execute(
                "UPDATE bridge_state SET upstream_channel=?, downstream_channel=?, "
                "rules_version=?, rules_digest=?, upstream_config_digest=?, "
                "downstream_config_digest=?, last_upstream_end=?, updated_clock=? "
                "WHERE rule_id=?",
                (
                    upstream.name,
                    downstream.name,
                    version,
                    rule.digest,
                    upstream.digest,
                    downstream.digest,
                    source_end,
                    clock_ms,
                    rule.id,
                ),
            )
            filter_code = compile_expression(
                rule.filter_expr, f"rules.{rule.id}.filter"
            )
            transform_code = compile_expression(
                rule.transform_expr, f"rules.{rule.id}.transform"
            )
            processed = 0
            forwarded = 0
            skipped = 0
            for source_offset in range(next_offset, source_end):
                record = conn.execute(
                    "SELECT * FROM records WHERE channel=? AND offset=?",
                    (upstream.name, source_offset),
                ).fetchone()
                if record is None or record["state"] != "live":
                    fail(
                        "E_UPSTREAM_WATERMARK_REGRESSION",
                        "bridge source event is unavailable or tombstoned",
                        rule=rule.id,
                        offset=source_offset,
                    )
                if str(record["config_digest"]) != upstream.digest:
                    fail(
                        "E_CHANNEL_VERSION_MISMATCH",
                        "upstream channel configuration differs from the record version",
                        rule=rule.id,
                        offset=source_offset,
                    )
                payload = _record_payload(record)
                partition_key = parse_json_text(
                    str(record["partition_key_json"]), "records.partition_key"
                )
                event_context = {
                    "channel": upstream.name,
                    "clock_ms": int(record["clock_ms"]),
                    "offset": source_offset,
                    "partition": int(record["partition"]),
                    "partition_key": partition_key,
                    "payload": payload,
                    "topic": upstream.topic,
                }
                accepted = evaluate_expression(
                    filter_code, payload, event_context, {}
                )
                if not isinstance(accepted, bool):
                    fail(
                        "E_TRANSFORM",
                        "bridge filter did not return a boolean",
                        rule=rule.id,
                        offset=source_offset,
                    )
                source_digest = str(record["source_digest"])
                if not accepted:
                    action_digest = _action_digest(
                        rule.id,
                        source_offset,
                        source_digest,
                        "skipped",
                        None,
                        None,
                        None,
                    )
                    conn.execute(
                        "INSERT INTO bridge_actions("
                        "rule_id, source_offset, upstream_channel, source_digest, "
                        "rules_digest, status, transformed_json, transformed_digest, "
                        "downstream_channel, downstream_offset, action_digest, clock_ms"
                        ") VALUES(?, ?, ?, ?, ?, 'skipped', NULL, NULL, NULL, NULL, ?, ?)",
                        (
                            rule.id,
                            source_offset,
                            upstream.name,
                            source_digest,
                            rule.digest,
                            action_digest,
                            clock_ms,
                        ),
                    )
                    skipped += 1
                else:
                    transformed = evaluate_expression(
                        transform_code, payload, event_context, {}
                    )
                    if not isinstance(transformed, dict):
                        fail(
                            "E_TRANSFORM",
                            "bridge transform did not return a JSON object",
                            rule=rule.id,
                            offset=source_offset,
                        )
                    try:
                        transformed_json = canonical_json(transformed)
                    except EventBusError as exc:
                        fail(
                            "E_TRANSFORM",
                            "bridge transform produced a non-JSON value",
                            rule=rule.id,
                            offset=source_offset,
                            reason=exc.message,
                        )
                    transformed_digest = "sha256:" + sha256_text(transformed_json)
                    transformed_again = evaluate_expression(
                        transform_code, payload, event_context, {}
                    )
                    if (
                        not isinstance(transformed_again, dict)
                        or canonical_json(transformed_again) != transformed_json
                        or "sha256:" + sha256_text(canonical_json(transformed_again))
                        != transformed_digest
                    ):
                        fail(
                            "E_TRANSFORM_DIGEST",
                            "transformation result digest verification failed",
                            rule=rule.id,
                            offset=source_offset,
                        )
                    target_row = conn.execute(
                        "SELECT next_offset FROM channels WHERE name=?",
                        (downstream.name,),
                    ).fetchone()
                    if target_row is None:
                        fail("E_BAD_CHANNEL", "downstream channel state is missing")
                    downstream_offset = int(target_row["next_offset"])
                    normalized_key = normalize_partition_key(partition_key)
                    partition = partition_for(normalized_key, downstream.partitions)
                    action_digest = _action_digest(
                        rule.id,
                        source_offset,
                        source_digest,
                        "forwarded",
                        transformed_digest,
                        downstream_offset,
                        downstream.digest,
                    )
                    hit = conn.execute(
                        "SELECT 1 FROM records WHERE channel=? AND offset=?",
                        (downstream.name, downstream_offset),
                    ).fetchone()
                    if hit is not None:
                        fail(
                            "E_CONFLICT",
                            "downstream offset is already occupied",
                            channel=downstream.name,
                            offset=downstream_offset,
                        )
                    record_size = len(
                        canonical_json(
                            {
                                "channel": downstream.name,
                                "clock_ms": int(record["clock_ms"]),
                                "offset": downstream_offset,
                                "origin_ref": f"{rule.id}:{source_offset}",
                                "origin_type": "bridge",
                                "partition": partition,
                                "partition_key": partition_key,
                                "payload": transformed,
                            }
                        ).encode("utf-8")
                    )
                    conn.execute(
                        "INSERT INTO records("
                        "channel, offset, partition, partition_key_json, payload_json, "
                        "payload_digest, source_digest, clock_ms, record_bytes, state, "
                        "origin_type, origin_ref, config_digest, created_clock"
                        ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'live', 'bridge', ?, ?, ?)",
                        (
                            downstream.name,
                            downstream_offset,
                            partition,
                            canonical_json(partition_key),
                            transformed_json,
                            transformed_digest,
                            action_digest,
                            int(record["clock_ms"]),
                            record_size,
                            f"{rule.id}:{source_offset}",
                            downstream.digest,
                            clock_ms,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO bridge_actions("
                        "rule_id, source_offset, upstream_channel, source_digest, "
                        "rules_digest, status, transformed_json, transformed_digest, "
                        "downstream_channel, downstream_offset, action_digest, clock_ms"
                        ") VALUES(?, ?, ?, ?, ?, 'forwarded', ?, ?, ?, ?, ?, ?)",
                        (
                            rule.id,
                            source_offset,
                            upstream.name,
                            source_digest,
                            rule.digest,
                            transformed_json,
                            transformed_digest,
                            downstream.name,
                            downstream_offset,
                            action_digest,
                            clock_ms,
                        ),
                    )
                    conn.execute(
                        "UPDATE channels SET next_offset=?, updated_clock=? "
                        "WHERE name=?",
                        (downstream_offset + 1, clock_ms, downstream.name),
                    )
                    forwarded += 1
                processed += 1
                chain_digest = _advance_chain(
                    chain_digest, action_digest, source_offset
                )
                crash_point("after_bridge_stage")
            final_end_row = conn.execute(
                "SELECT next_offset FROM channels WHERE name=?", (upstream.name,)
            ).fetchone()
            final_upstream_end = (
                int(final_end_row["next_offset"])
                if final_end_row is not None
                else source_end
            )
            conn.execute(
                "UPDATE bridge_state SET next_offset=?, last_source_offset=?, "
                "chain_digest=?, last_upstream_end=?, updated_clock=? WHERE rule_id=?",
                (
                    source_end,
                    source_end - 1 if source_end > 0 else None,
                    chain_digest,
                    final_upstream_end,
                    clock_ms,
                    rule.id,
                ),
            )
            results.append(
                {
                    "forwarded": forwarded,
                    "from_offset": next_offset,
                    "next_upstream_offset": source_end,
                    "processed": processed,
                    "rule_id": rule.id,
                    "skipped": skipped,
                    "upstream_channel": rule.upstream,
                }
            )
            crash_point("after_bridge_rule")
        result = {
            "command": "bridge",
            "rules": sorted(results, key=lambda item: item["rule_id"]),
            "rules_version": version,
        }
        retention = self._apply_retention(
            conn, clock_ms, op_key, version, channels, record_within_limit=False
        )
        if retention is not None:
            result["cleanup"] = retention["summary"]
        return result

    # --------------------------------------------------------------- consume

    def _prepare_consume(
        self, conn: sqlite3.Connection, input_value: dict[str, Any]
    ) -> dict[str, Any]:
        version, digest, _, _, _, _ = load_config(conn)
        consumer_id = input_value.get("consumer_id")
        if not isinstance(consumer_id, str) or not consumer_id:
            fail("E_SUBSCRIPTION", "consumer_id must be non-empty")
        subscription = conn.execute(
            "SELECT * FROM subscriptions WHERE consumer_id=?", (consumer_id,)
        ).fetchone()
        if subscription is None:
            fail("E_SUBSCRIPTION", "subscription does not exist", consumer_id=consumer_id)
        budget = input_value.get("budget")
        if budget is None:
            budget = int(subscription["budget"])
        budget = _require_int(budget, "E_BAD_SUBSCRIPTION", "budget")
        if budget <= 0:
            fail("E_BAD_SUBSCRIPTION", "budget must be positive")
        report_offset = input_value.get("report_offset")
        if report_offset is not None:
            report_offset = _require_int(
                report_offset, "E_BAD_CHECKPOINT", "report_offset"
            )
            if report_offset < 0:
                fail("E_BAD_CHECKPOINT", "report_offset must be non-negative")
        input_value = dict(input_value)
        input_value.update(
            {
                "bound_config_digest": digest,
                "bound_config_version": version,
                "budget": budget,
                "previous_offset": int(subscription["current_offset"]),
                "report_offset": report_offset,
                "subscription": {
                    "budget": int(subscription["budget"]),
                    "channel": str(subscription["channel"]),
                    "consumer_id": str(subscription["consumer_id"]),
                    "current_offset": int(subscription["current_offset"]),
                    "start_offset": int(subscription["start_offset"]),
                },
            }
        )
        return input_value

    def _do_consume(
        self,
        conn: sqlite3.Connection,
        input_value: dict[str, Any],
        op_key: str,
    ) -> dict[str, Any]:
        clock_ms = int(input_value["clock_ms"])
        version, digest, _, _, _, _ = load_config(conn)
        if (
            version != int(input_value["bound_config_version"])
            or digest != input_value["bound_config_digest"]
        ):
            fail(
                "E_CONFIG_VERSION_MISMATCH",
                "configuration changed after the consume intent was bound",
            )
        consumer_id = str(input_value["consumer_id"])
        subscription = conn.execute(
            "SELECT * FROM subscriptions WHERE consumer_id=?", (consumer_id,)
        ).fetchone()
        if subscription is None:
            fail("E_SUBSCRIPTION", "subscription does not exist", consumer_id=consumer_id)
        previous = int(subscription["current_offset"])
        if previous != int(input_value["previous_offset"]):
            fail(
                "E_OPERATION_CONFLICT",
                "subscription offset changed after the consume intent was bound",
                bound=int(input_value["previous_offset"]),
                current=previous,
            )
        channel_name = str(subscription["channel"])
        channel_state = conn.execute(
            "SELECT next_offset, cleaned_through FROM channels WHERE name=?",
            (channel_name,),
        ).fetchone()
        if channel_state is None:
            fail("E_BAD_CHANNEL", "subscription channel is missing")
        channel_end = int(channel_state["next_offset"])
        budget = int(input_value["budget"])
        rows = conn.execute(
            "SELECT * FROM records WHERE channel=? AND offset>=? AND state='live' "
            "ORDER BY offset LIMIT ?",
            (channel_name, previous, budget),
        ).fetchall()
        records: list[dict[str, Any]] = []
        for row in rows:
            payload = _record_payload(row)
            records.append(
                {
                    "clock_ms": int(row["clock_ms"]),
                    "offset": int(row["offset"]),
                    "partition": int(row["partition"]),
                    "payload": payload,
                    "payload_digest": str(row["payload_digest"]),
                    "source_digest": str(row["source_digest"]),
                }
            )
        pulled_end = int(rows[-1]["offset"]) + 1 if rows else previous
        requested = input_value.get("report_offset")
        applied = False
        outcome: str
        new_offset = previous
        if requested is not None:
            requested = int(requested)
            if requested > channel_end:
                fail(
                    "E_OFFSET_AHEAD",
                    "checkpoint is beyond the channel end",
                    requested=requested,
                    channel_end=channel_end,
                )
            if requested < previous:
                outcome = "stale_report"
            elif requested == previous:
                outcome = "duplicate_report"
            elif requested > pulled_end:
                fail(
                    "E_REPORT_AHEAD",
                    "checkpoint is beyond the pulled batch",
                    requested=requested,
                    pulled_end=pulled_end,
                )
            else:
                outcome = "checkpoint_applied"
                new_offset = requested
                applied = True
        else:
            if pulled_end > previous:
                new_offset = pulled_end
                applied = True
                outcome = "checkpoint_applied"
            else:
                outcome = "no_live_records"

        remaining = conn.execute(
            "SELECT COUNT(*) AS count FROM records "
            "WHERE channel=? AND offset>=? AND state='live'",
            (channel_name, new_offset),
        ).fetchone()
        remaining_count = int(remaining["count"]) if remaining is not None else 0
        if remaining_count > 0:
            reason = "budget_exhausted"
        elif new_offset >= channel_end:
            reason = "channel_caught_up"
        else:
            reason = "tombstone_gap"
        if applied:
            conn.execute(
                "UPDATE subscriptions SET current_offset=?, updated_clock=? "
                "WHERE consumer_id=?",
                (new_offset, clock_ms, consumer_id),
            )
        result_payload = {
            "channel": channel_name,
            "command": "consume",
            "consumer_id": consumer_id,
            "offset_after": new_offset,
            "offset_before": previous,
            "outcome": outcome,
            "reason": reason,
            "records": records,
            "remaining_count": remaining_count,
        }
        report_digest = _sha(result_payload)
        conn.execute(
            "INSERT INTO checkpoint_reports("
            "consumer_id, requested_offset, previous_offset, current_offset, "
            "outcome, reason, record_count, clock_ms, digest, operation_key"
            ") VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                consumer_id,
                new_offset if requested is None else int(requested),
                previous,
                new_offset,
                outcome,
                reason,
                len(records),
                clock_ms,
                report_digest,
                op_key,
            ),
        )
        retention = self._apply_retention(
            conn,
            clock_ms,
            op_key,
            version,
            load_config(conn)[2],
            record_within_limit=False,
        )
        if retention is not None:
            result_payload["cleanup"] = retention["summary"]
        crash_point("after_consume_stage")
        return {
            **result_payload,
            "config_version": version,
        }

    # --------------------------------------------------------------- cleanup

    def _prepare_cleanup(
        self, conn: sqlite3.Connection, input_value: dict[str, Any]
    ) -> dict[str, Any]:
        version, digest, channels, _, _, _ = load_config(conn)
        input_value = dict(input_value)
        input_value["bound_config_version"] = version
        input_value["bound_config_digest"] = digest
        input_value["channels"] = [channel.to_dict() for channel in channels]
        input_value["cleanup_positions"] = [
            {
                "cleaned_through": int(row["cleaned_through"]),
                "channel": str(row["name"]),
                "next_offset": int(row["next_offset"]),
            }
            for row in conn.execute(
                "SELECT name, next_offset, cleaned_through FROM channels ORDER BY name"
            ).fetchall()
        ]
        input_value["cursor_positions"] = [
            {
                "consumer_id": str(row["consumer_id"]),
                "current_offset": int(row["current_offset"]),
            }
            for row in conn.execute(
                "SELECT consumer_id, current_offset FROM subscriptions ORDER BY consumer_id"
            ).fetchall()
        ]
        input_value["bridge_positions"] = [
            {
                "next_offset": int(row["next_offset"]),
                "rule_id": str(row["rule_id"]),
            }
            for row in conn.execute(
                "SELECT rule_id, next_offset FROM bridge_state ORDER BY rule_id"
            ).fetchall()
        ]
        return input_value

    def _do_cleanup(
        self,
        conn: sqlite3.Connection,
        input_value: dict[str, Any],
        op_key: str,
    ) -> dict[str, Any]:
        clock_ms = int(input_value["clock_ms"])
        version, digest, channels, _, _, _ = load_config(conn)
        if (
            version != int(input_value["bound_config_version"])
            or digest != input_value["bound_config_digest"]
        ):
            fail(
                "E_CONFIG_VERSION_MISMATCH",
                "configuration changed after the cleanup intent was bound",
            )
        result = self._apply_retention(
            conn,
            clock_ms,
            op_key,
            version,
            channels,
            record_within_limit=True,
        )
        if result is None:
            fail("E_INTERNAL", "explicit cleanup produced no retention decision")
        return {"command": "cleanup", **result}

    def _apply_retention(
        self,
        conn: sqlite3.Connection,
        clock_ms: int,
        op_key: str,
        version: int,
        channels: list[Channel],
        *,
        record_within_limit: bool,
    ) -> dict[str, Any] | None:
        summaries: list[dict[str, Any]] = []
        pressure_seen = False
        channel_list = (
            list(channels.values()) if isinstance(channels, dict) else list(channels)
        )
        for channel in channel_list:
            state = conn.execute(
                "SELECT next_offset, cleaned_through FROM channels WHERE name=?",
                (channel.name,),
            ).fetchone()
            if state is None:
                fail("E_BAD_CHANNEL", "channel runtime state is missing")
            end_offset = int(state["next_offset"])
            cleaned_from = int(state["cleaned_through"])
            safe_candidates = [end_offset]
            for row in conn.execute(
                "SELECT current_offset FROM subscriptions WHERE channel=? ORDER BY consumer_id",
                (channel.name,),
            ):
                safe_candidates.append(int(row["current_offset"]))
            for row in conn.execute(
                "SELECT next_offset FROM bridge_state WHERE upstream_channel=? "
                "ORDER BY rule_id",
                (channel.name,),
            ):
                safe_candidates.append(int(row["next_offset"]))
            safe_offset = min(safe_candidates)
            if safe_offset < end_offset and safe_offset == end_offset:
                safe_offset = end_offset
            before_row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN state='live' THEN record_bytes ELSE 0 END), 0) AS bytes, "
                "COALESCE(SUM(CASE WHEN state='live' THEN 1 ELSE 0 END), 0) AS live "
                "FROM records WHERE channel=?",
                (channel.name,),
            ).fetchone()
            before_bytes = int(before_row["bytes"]) if before_row else 0
            live_before = int(before_row["live"]) if before_row else 0
            physical = 0
            tombstoned = 0
            reclaimed = 0
            cursor = cleaned_from
            if before_bytes > channel.retention_bytes:
                pressure_seen = True
                candidates = conn.execute(
                    "SELECT * FROM records WHERE channel=? AND offset>=? "
                    "AND offset<? ORDER BY offset",
                    (channel.name, cleaned_from, safe_offset),
                ).fetchall()
                projected = before_bytes
                for record in candidates:
                    if projected <= channel.retention_bytes:
                        break
                    offset = int(record["offset"])
                    if offset != cursor:
                        fail(
                            "E_VERIFY",
                            "retention candidate range is not contiguous",
                            channel=channel.name,
                            expected=cursor,
                            actual=offset,
                        )
                    referenced = conn.execute(
                        "SELECT 1 FROM bridge_actions WHERE "
                        "(upstream_channel=? AND source_offset=?) OR "
                        "(downstream_channel=? AND downstream_offset=?) LIMIT 1",
                        (channel.name, offset, channel.name, offset),
                    ).fetchone()
                    size = int(record["record_bytes"])
                    if referenced is not None:
                        conn.execute(
                            "UPDATE records SET state='tombstone', payload_json=NULL "
                            "WHERE channel=? AND offset=?",
                            (channel.name, offset),
                        )
                        tombstoned += 1
                    else:
                        conn.execute(
                            "DELETE FROM records WHERE channel=? AND offset=?",
                            (channel.name, offset),
                        )
                        physical += 1
                    reclaimed += size
                    projected -= size
                    cursor = offset + 1
                after_bytes = projected
                if after_bytes > channel.retention_bytes:
                    reason = "pinned_by_min_subscription_or_bridge"
                else:
                    reason = "retention_pressure"
            else:
                after_bytes = before_bytes
                reason = "within_limit"
            if cursor > cleaned_from:
                conn.execute(
                    "UPDATE channels SET cleaned_through=?, updated_clock=? "
                    "WHERE name=?",
                    (cursor, clock_ms, channel.name),
                )
            summaries.append(
                {
                    "before_bytes": before_bytes,
                    "bytes_reclaimed": reclaimed,
                    "channel": channel.name,
                    "cleaned_from": cleaned_from,
                    "cleaned_through": cursor,
                    "limit_bytes": channel.retention_bytes,
                    "live_records_before": live_before,
                    "physical_cleared": physical,
                    "reason": reason,
                    "retained_bytes": after_bytes,
                    "safe_offset": safe_offset,
                    "tombstoned": tombstoned,
                }
            )
            crash_point("after_cleanup_stage")
        if not pressure_seen and not record_within_limit:
            return None
        summary = {
            "channels": sorted(summaries, key=lambda item: item["channel"]),
            "clock_ms": clock_ms,
            "config_version": version,
        }
        summary_json = canonical_json(summary)
        digest_value = _sha(summary)
        conn.execute(
            "INSERT INTO cleanup_runs("
            "operation_key, clock_ms, summary_json, digest"
            ") VALUES(?, ?, ?, ?)",
            (op_key, clock_ms, summary_json, digest_value),
        )
        return {
            "summary": summary,
            "summary_digest": digest_value,
        }

    # ------------------------------------------------------------------ edit

    def _prepare_edit(
        self, conn: sqlite3.Connection, input_value: dict[str, Any]
    ) -> dict[str, Any]:
        version, digest, channels, rules, subscriptions, _ = load_config(conn)
        input_value = dict(input_value)
        input_value["bound_config_version"] = version
        input_value["bound_config_digest"] = digest
        kind = input_value["kind"]
        action = input_value["action"]
        object_id = input_value.get("object_id")
        data = input_value.get("data")
        if kind == "event":
            max_seq_row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM source_events"
            ).fetchone()
            input_value["max_seq"] = int(max_seq_row["max_seq"])
            topic_to_channel = {channel.topic: channel.name for channel in channels}
            if action == "add":
                event = parse_event_input(data, topic_to_channel)
                if event.seq <= int(max_seq_row["max_seq"]):
                    fail(
                        "E_EVENT_SEQ",
                        "new event seq must be greater than every existing seq",
                        highest=int(max_seq_row["max_seq"]),
                        actual=event.seq,
                    )
            else:
                seq = _require_int(object_id, "E_NOT_FOUND", "event id")
                row = conn.execute(
                    "SELECT * FROM source_events WHERE seq=?", (seq,)
                ).fetchone()
                if row is None or (action == "update" and row["state"] != "active"):
                    fail("E_NOT_FOUND", "event does not exist", seq=seq)
                if action == "update":
                    if not isinstance(data, dict):
                        fail("E_EDIT", "event update data must be an object")
                    merged = dict(_row_event(row).to_dict())
                    merged.update(data)
                    if merged.get("seq") != seq:
                        fail("E_EVENT_SEQ", "event seq cannot change during update")
                    if merged.get("topic") != row["topic"]:
                        fail("E_BAD_TOPIC", "event topic cannot change during update")
                    parse_event_input(merged, topic_to_channel)
        else:
            channel_names = {channel.name for channel in channels}
            by_id = {rule.id: rule for rule in rules}
            if action == "add":
                if not isinstance(data, dict):
                    fail("E_EDIT", "rule add data must be an object")
                rule = parse_rule_input(data, channel_names)
                if rule.id in by_id:
                    fail("E_DUPLICATE_RULE", "bridge rule already exists", rule=rule.id)
                validate_rule_set(rules + [rule])
            else:
                rule_id = str(object_id)
                if rule_id not in by_id:
                    fail("E_NOT_FOUND", "bridge rule does not exist", rule=rule_id)
                if action == "update":
                    if not isinstance(data, dict):
                        fail("E_EDIT", "rule update data must be an object")
                    merged = dict(by_id[rule_id].to_dict())
                    merged.update(data)
                    if merged.get("id") != rule_id:
                        fail("E_BAD_RULE", "bridge rule id cannot change during update")
                    rule = parse_rule_input(merged, channel_names)
                    validate_rule_set(
                        [rule if item.id == rule_id else item for item in rules]
                    )
                elif action == "delete":
                    validate_rule_set(
                        [rule for rule in rules if rule.id != rule_id]
                    )
            input_value["channel_names"] = sorted(channel_names)
            input_value["rule_ids"] = sorted(rule.id for rule in rules)
            input_value["rules"] = [rule.to_dict() for rule in rules]
        return input_value

    def _do_edit(
        self,
        conn: sqlite3.Connection,
        input_value: dict[str, Any],
        op_key: str,
    ) -> dict[str, Any]:
        clock_ms = int(input_value["clock_ms"])
        version, digest, channels, rules, subscriptions, _ = load_config(conn)
        if (
            version != int(input_value["bound_config_version"])
            or digest != input_value["bound_config_digest"]
        ):
            fail(
                "E_RULE_VERSION_CHANGED",
                "configuration changed after the edit intent was bound",
            )
        kind = str(input_value["kind"])
        action = str(input_value["action"])
        object_id = input_value.get("object_id")
        data = input_value.get("data")
        if kind == "event":
            return self._edit_event(
                conn,
                action,
                object_id,
                data,
                clock_ms,
                op_key,
                channels,
                rules,
                subscriptions,
                version,
            )
        return self._edit_rule(
            conn,
            action,
            object_id,
            data,
            clock_ms,
            op_key,
            channels,
            rules,
            subscriptions,
            version,
            digest,
        )

    def _next_edit_version(self, conn: sqlite3.Connection) -> int:
        version = get_meta_int(conn, "edit_version", 0) + 1
        set_meta(conn, "edit_version", str(version))
        return version

    def _edit_event(
        self,
        conn: sqlite3.Connection,
        action: str,
        object_id: Any,
        data: Any,
        clock_ms: int,
        op_key: str,
        channels: list[Channel],
        rules: list[BridgeRule],
        subscriptions: list[Subscription],
        config_version: int,
    ) -> dict[str, Any]:
        topic_to_channel = {channel.topic: channel.name for channel in channels}
        before: dict[str, Any] | None = None
        after: dict[str, Any] | None = None
        published = False
        if action == "add":
            event = parse_event_input(data, topic_to_channel)
            max_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM source_events"
            ).fetchone()
            highest = int(max_seq["max_seq"])
            if event.seq <= highest:
                fail(
                    "E_EVENT_SEQ",
                    "new event seq must be greater than every existing seq",
                    highest=highest,
                    actual=event.seq,
                )
            existing = conn.execute(
                "SELECT 1 FROM source_events WHERE seq=?", (event.seq,)
            ).fetchone()
            if existing is not None:
                fail("E_EVENT_SEQ", "event seq already exists", seq=event.seq)
            edit_version = self._next_edit_version(conn)
            conn.execute(
                "INSERT INTO source_events("
                "seq, topic, partition_key_json, payload_json, clock_ms, state, "
                "revision_version, created_clock, updated_clock"
                ") VALUES(?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (
                    event.seq,
                    event.topic,
                    canonical_json(event.partition_key),
                    canonical_json(event.payload),
                    event.clock_ms,
                    edit_version,
                    clock_ms,
                    clock_ms,
                ),
            )
            after = event.to_dict()
            object_id = event.seq
            impact = self._impact_closure(
                {topic_to_channel[event.topic]}, rules, subscriptions
            )
        elif action == "update":
            seq = _require_int(object_id, "E_NOT_FOUND", "event id")
            row = conn.execute(
                "SELECT * FROM source_events WHERE seq=?", (seq,)
            ).fetchone()
            if row is None or row["state"] != "active":
                fail("E_NOT_FOUND", "active event does not exist", seq=seq)
            if not isinstance(data, dict):
                fail("E_EDIT", "event update data must be an object")
            current = _row_event(row)
            before = current.to_dict()
            merged = dict(before)
            merged.update(data)
            if merged.get("seq") != seq:
                fail("E_EVENT_SEQ", "event seq cannot change during update")
            if merged.get("topic") != current.topic:
                fail("E_BAD_TOPIC", "event topic cannot change during update")
            event = parse_event_input(merged, topic_to_channel)
            published_row = conn.execute(
                "SELECT 1 FROM published_events WHERE seq=?", (seq,)
            ).fetchone()
            published = published_row is not None
            edit_version = self._next_edit_version(conn)
            conn.execute(
                "UPDATE source_events SET partition_key_json=?, payload_json=?, "
                "clock_ms=?, revision_version=?, updated_clock=? WHERE seq=?",
                (
                    canonical_json(event.partition_key),
                    canonical_json(event.payload),
                    event.clock_ms,
                    edit_version,
                    clock_ms,
                    seq,
                ),
            )
            after = event.to_dict()
            object_id = seq
            impact = self._impact_closure(
                {topic_to_channel[event.topic]}, rules, subscriptions
            )
        elif action == "delete":
            seq = _require_int(object_id, "E_NOT_FOUND", "event id")
            row = conn.execute(
                "SELECT * FROM source_events WHERE seq=?", (seq,)
            ).fetchone()
            if row is None:
                fail("E_NOT_FOUND", "event does not exist", seq=seq)
            current = _row_event(row)
            before = current.to_dict()
            if row["state"] == "deleted":
                return {
                    "action": action,
                    "already_deleted": True,
                    "command": "edit",
                    "config_version": config_version,
                    "edit_version": int(row["revision_version"]),
                    "impact_closure": self._impact_closure(
                        {topic_to_channel[current.topic]}, rules, subscriptions
                    ),
                    "kind": "event",
                    "object_id": seq,
                    "published_log_immutable": False,
                }
            published_row = conn.execute(
                "SELECT 1 FROM published_events WHERE seq=?", (seq,)
            ).fetchone()
            published = published_row is not None
            edit_version = self._next_edit_version(conn)
            conn.execute(
                "UPDATE source_events SET state='deleted', revision_version=?, "
                "updated_clock=? WHERE seq=?",
                (edit_version, clock_ms, seq),
            )
            after = None
            object_id = seq
            impact = self._impact_closure(
                {topic_to_channel[current.topic]}, rules, subscriptions
            )
        else:
            fail("E_EDIT", "unsupported event edit action", action=action)

        after_json = None if after is None else canonical_json(after)
        before_json = None if before is None else canonical_json(before)
        impact_json = canonical_json(impact)
        digest_value = _sha(
            {
                "action": action,
                "after": after,
                "before": before,
                "kind": "event",
                "object_id": object_id,
                "version": edit_version,
            }
        )
        conn.execute(
            "INSERT INTO edit_versions("
            "version, kind, action, object_id, before_json, after_json, impact_json, "
            "config_version, clock_ms, operation_key, digest"
            ") VALUES(?, 'event', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edit_version,
                action,
                str(object_id),
                before_json,
                after_json,
                impact_json,
                config_version,
                clock_ms,
                op_key,
                digest_value,
            ),
        )
        if after is not None:
            advance_logical_clock(conn, max(clock_ms, int(after["clock_ms"])))
        return {
            "action": action,
            "command": "edit",
            "config_version": config_version,
            "edit_version": edit_version,
            "impact_closure": impact,
            "kind": "event",
            "object_id": object_id,
            "published": published,
            "published_log_immutable": published,
        }

    def _edit_rule(
        self,
        conn: sqlite3.Connection,
        action: str,
        object_id: Any,
        data: Any,
        clock_ms: int,
        op_key: str,
        channels: list[Channel],
        rules: list[BridgeRule],
        subscriptions: list[Subscription],
        config_version: int,
        config_digest_value: str,
    ) -> dict[str, Any]:
        channel_names = {channel.name for channel in channels}
        by_id = {rule.id: rule for rule in rules}
        before: dict[str, Any] | None = None
        after: dict[str, Any] | None = None
        if action == "add":
            if not isinstance(data, dict):
                fail("E_EDIT", "rule add data must be an object")
            rule = parse_rule_input(data, channel_names)
            if rule.id in by_id:
                fail("E_DUPLICATE_RULE", "bridge rule already exists", rule=rule.id)
            new_rules = sorted(rules + [rule], key=lambda item: item.id)
            validate_rule_set(new_rules)
            object_id = rule.id
            after = rule.to_dict()
            impact = self._impact_closure(
                {rule.upstream, rule.downstream}, new_rules, subscriptions
            )
        elif action == "update":
            rule_id = str(object_id)
            if rule_id not in by_id:
                fail("E_NOT_FOUND", "bridge rule does not exist", rule=rule_id)
            if not isinstance(data, dict):
                fail("E_EDIT", "rule update data must be an object")
            current = by_id[rule_id]
            before = current.to_dict()
            merged = dict(before)
            merged.update(data)
            if merged.get("id") != rule_id:
                fail("E_BAD_RULE", "bridge rule id cannot change during update")
            rule = parse_rule_input(merged, channel_names)
            if rule.to_dict() == current.to_dict():
                return {
                    "action": action,
                    "command": "edit",
                    "config_version": config_version,
                    "edit_version": get_meta_int(conn, "edit_version", 0),
                    "impact_closure": self._impact_closure(
                        {rule.upstream, rule.downstream}, rules, subscriptions
                    ),
                    "kind": "rule",
                    "noop": True,
                    "object_id": rule_id,
                }
            new_rules = sorted(
                [rule if item.id == rule_id else item for item in rules],
                key=lambda item: item.id,
            )
            validate_rule_set(new_rules)
            after = rule.to_dict()
            impact = self._impact_closure(
                {rule.upstream, rule.downstream}, new_rules, subscriptions
            )
        elif action == "delete":
            rule_id = str(object_id)
            if rule_id not in by_id:
                fail("E_NOT_FOUND", "bridge rule does not exist", rule=rule_id)
            before = by_id[rule_id].to_dict()
            new_rules = [rule for rule in rules if rule.id != rule_id]
            validate_rule_set(new_rules)
            after = None
            impact = self._impact_closure(
                {by_id[rule_id].upstream, by_id[rule_id].downstream},
                rules,
                subscriptions,
            )
        else:
            fail("E_EDIT", "unsupported rule edit action", action=action)

        edit_version = self._next_edit_version(conn)
        new_config_version = config_version + 1
        new_config_digest = config_digest(channels, new_rules, subscriptions)
        if new_config_digest == config_digest_value and action == "update":
            fail("E_CONFIG", "rule update produced no configuration change")
        conn.execute(
            "INSERT INTO config_versions("
            "version, digest, catalog_json, rules_json, subscriptions_json, "
            "created_clock, edit_version"
            ") VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                new_config_version,
                new_config_digest,
                canonical_json([channel.to_dict() for channel in channels]),
                canonical_json([rule.to_dict() for rule in new_rules]),
                canonical_json(
                    [subscription.to_dict() for subscription in subscriptions]
                ),
                clock_ms,
                edit_version,
            ),
        )
        for channel in channels:
            conn.execute(
                "INSERT INTO channel_versions("
                "config_version, name, topic, partitions, retention_bytes, "
                "max_batch_records, digest"
                ") VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    new_config_version,
                    channel.name,
                    channel.topic,
                    channel.partitions,
                    channel.retention_bytes,
                    channel.max_batch_records,
                    channel.digest,
                ),
            )
        for rule in new_rules:
            conn.execute(
                "INSERT INTO rule_versions("
                "config_version, rule_id, upstream, downstream, filter_expr, "
                "transform_expr, depends_upstream_offset, digest"
                ") VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_config_version,
                    rule.id,
                    rule.upstream,
                    rule.downstream,
                    rule.filter_expr,
                    rule.transform_expr,
                    rule.depends_upstream_offset,
                    rule.digest,
                ),
            )
        conn.execute(
            "UPDATE config_pointer SET version=? WHERE id=1", (new_config_version,)
        )
        impact_json = canonical_json(impact)
        before_json = None if before is None else canonical_json(before)
        after_json = None if after is None else canonical_json(after)
        digest_value = _sha(
            {
                "action": action,
                "after": after,
                "before": before,
                "kind": "rule",
                "object_id": object_id,
                "version": edit_version,
            }
        )
        conn.execute(
            "INSERT INTO edit_versions("
            "version, kind, action, object_id, before_json, after_json, impact_json, "
            "config_version, clock_ms, operation_key, digest"
            ") VALUES(?, 'rule', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edit_version,
                action,
                str(object_id),
                before_json,
                after_json,
                impact_json,
                new_config_version,
                clock_ms,
                op_key,
                digest_value,
            ),
        )
        return {
            "action": action,
            "command": "edit",
            "config_version": new_config_version,
            "config_digest": new_config_digest,
            "edit_version": edit_version,
            "impact_closure": impact,
            "kind": "rule",
            "object_id": object_id,
        }

    def _impact_closure(
        self,
        starting_channels: set[str],
        rules: list[BridgeRule],
        subscriptions: list[Subscription],
    ) -> dict[str, Any]:
        rules_by_upstream: dict[str, list[BridgeRule]] = {}
        for rule in sorted(rules, key=lambda item: item.id):
            rules_by_upstream.setdefault(rule.upstream, []).append(rule)
        reachable_channels = set(starting_channels)
        reachable_rules: set[str] = set()
        paths: list[dict[str, Any]] = []
        stack: list[dict[str, Any]] = [
            {"channels": [channel], "rules": []} for channel in sorted(starting_channels)
        ]
        seen_paths: set[str] = set()
        while stack:
            path = stack.pop()
            channel = path["channels"][-1]
            expanded = False
            for rule in rules_by_upstream.get(channel, []):
                reachable_rules.add(rule.id)
                reachable_channels.add(rule.downstream)
                new_path = {
                    "channels": path["channels"] + [rule.downstream],
                    "rules": path["rules"] + [rule.id],
                }
                key = canonical_json(new_path)
                if key not in seen_paths and len(paths) < 200:
                    seen_paths.add(key)
                    paths.append(new_path)
                    stack.append(new_path)
                    expanded = True
            if not expanded:
                simple = {
                    "channels": list(path["channels"]),
                    "rules": list(path["rules"]),
                }
                key = canonical_json(simple)
                if key not in seen_paths and len(paths) < 200:
                    seen_paths.add(key)
                    paths.append(simple)
        impacted_subscriptions = sorted(
            subscription.consumer_id
            for subscription in subscriptions
            if subscription.channel in reachable_channels
        )
        return {
            "bridge_rules": sorted(reachable_rules),
            "channels": sorted(reachable_channels),
            "paths": sorted(paths, key=canonical_json),
            "subscriptions": impacted_subscriptions,
        }

    # --------------------------------------------------------------- status

    def _channel_summaries(
        self, conn: sqlite3.Connection, channels: list[Channel]
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for channel in sorted(channels, key=lambda item: item.name):
            state = conn.execute(
                "SELECT next_offset, cleaned_through FROM channels WHERE name=?",
                (channel.name,),
            ).fetchone()
            if state is None:
                fail("E_BAD_CHANNEL", "channel runtime state is missing", channel=channel.name)
            stats = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN state='live' THEN record_bytes ELSE 0 END), 0) "
                "AS live_bytes, COALESCE(SUM(CASE WHEN state='live' THEN 1 ELSE 0 END), 0) "
                "AS live_records, COALESCE(SUM(CASE WHEN state='tombstone' THEN 1 ELSE 0 END), 0) "
                "AS tombstones FROM records WHERE channel=?",
                (channel.name,),
            ).fetchone()
            log_rows = [
                {
                    "clock_ms": int(row["clock_ms"]),
                    "config_digest": str(row["config_digest"]),
                    "offset": int(row["offset"]),
                    "origin_ref": str(row["origin_ref"]),
                    "origin_type": str(row["origin_type"]),
                    "partition": int(row["partition"]),
                    "payload_digest": str(row["payload_digest"]),
                    "source_digest": str(row["source_digest"]),
                    "state": str(row["state"]),
                }
                for row in conn.execute(
                    "SELECT offset, partition, payload_digest, source_digest, "
                    "clock_ms, state, origin_type, origin_ref, config_digest "
                    "FROM records WHERE channel=? ORDER BY offset",
                    (channel.name,),
                )
            ]
            end_offset = int(state["next_offset"])
            result.append(
                {
                    "channel": channel.name,
                    "cleaned_through": int(state["cleaned_through"]),
                    "end_offset": end_offset,
                    "live_bytes": int(stats["live_bytes"]),
                    "live_records": int(stats["live_records"]),
                    "log_digest": _sha(log_rows),
                    "max_batch_records": channel.max_batch_records,
                    "partitions": channel.partitions,
                    "retention_bytes": channel.retention_bytes,
                    "tail_offset": end_offset - 1 if end_offset > 0 else None,
                    "tombstones": int(stats["tombstones"]),
                    "topic": channel.topic,
                }
            )
        return result

    def _status_in_tx(self, conn: sqlite3.Connection) -> dict[str, Any]:
        version, digest, channels, rules, subscriptions, _ = load_config(conn)
        channel_rows = self._channel_summaries(conn, channels)
        bridge_by_id = {rule.id: rule for rule in rules}
        bridges: list[dict[str, Any]] = []
        for rule in sorted(rules, key=lambda item: item.id):
            state = conn.execute(
                "SELECT * FROM bridge_state WHERE rule_id=?", (rule.id,)
            ).fetchone()
            upstream_end = conn.execute(
                "SELECT next_offset FROM channels WHERE name=?", (rule.upstream,)
            ).fetchone()
            if state is None:
                next_offset = 0
                last_source_offset = None
                chain_digest = _initial_chain(rule.id)
                rules_version = None
                rules_digest = None
                last_upstream_end = 0
            else:
                next_offset = int(state["next_offset"])
                last_source_offset = (
                    None
                    if state["last_source_offset"] is None
                    else int(state["last_source_offset"])
                )
                chain_digest = str(state["chain_digest"])
                rules_version = int(state["rules_version"])
                rules_digest = str(state["rules_digest"])
                last_upstream_end = int(state["last_upstream_end"])
            upstream_end_value = (
                int(upstream_end["next_offset"]) if upstream_end is not None else 0
            )
            counts = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='forwarded' THEN 1 ELSE 0 END), 0) "
                "AS forwarded, COALESCE(SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END), 0) "
                "AS skipped FROM bridge_actions WHERE rule_id=? AND source_offset<?",
                (rule.id, next_offset),
            ).fetchone()
            bridges.append(
                {
                    "active": True,
                    "chain_digest": chain_digest,
                    "downstream": rule.downstream,
                    "forwarded": int(counts["forwarded"]),
                    "last_source_offset": last_source_offset,
                    "last_upstream_end": last_upstream_end,
                    "next_upstream_offset": next_offset,
                    "pending_backlog": max(0, upstream_end_value - next_offset),
                    "rule_id": rule.id,
                    "rule_digest": rule.digest,
                    "rules_digest": rules_digest,
                    "rules_version": rules_version,
                    "skipped": int(counts["skipped"]),
                    "upstream": rule.upstream,
                }
            )
        active_rule_ids = {rule.id for rule in rules}
        for state in conn.execute(
            "SELECT * FROM bridge_state ORDER BY rule_id"
        ):
            rule_id = str(state["rule_id"])
            if rule_id in active_rule_ids:
                continue
            upstream_end = conn.execute(
                "SELECT next_offset FROM channels WHERE name=?",
                (state["upstream_channel"],),
            ).fetchone()
            upstream_end_value = (
                int(upstream_end["next_offset"]) if upstream_end is not None else 0
            )
            next_offset = int(state["next_offset"])
            counts = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN status='forwarded' THEN 1 ELSE 0 END), 0) "
                "AS forwarded, COALESCE(SUM(CASE WHEN status='skipped' THEN 1 ELSE 0 END), 0) "
                "AS skipped FROM bridge_actions WHERE rule_id=? AND source_offset<?",
                (rule_id, next_offset),
            ).fetchone()
            bridges.append(
                {
                    "active": False,
                    "chain_digest": str(state["chain_digest"]),
                    "downstream": str(state["downstream_channel"]),
                    "forwarded": int(counts["forwarded"]),
                    "last_source_offset": (
                        None
                        if state["last_source_offset"] is None
                        else int(state["last_source_offset"])
                    ),
                    "last_upstream_end": int(state["last_upstream_end"]),
                    "next_upstream_offset": next_offset,
                    "pending_backlog": max(0, upstream_end_value - next_offset),
                    "rule_digest": None,
                    "rule_id": rule_id,
                    "rules_digest": str(state["rules_digest"]),
                    "rules_version": int(state["rules_version"]),
                    "skipped": int(counts["skipped"]),
                    "upstream": str(state["upstream_channel"]),
                }
            )
        subscription_rows: list[dict[str, Any]] = []
        for row in conn.execute(
            "SELECT * FROM subscriptions ORDER BY consumer_id"
        ):
            channel_state = conn.execute(
                "SELECT next_offset FROM channels WHERE name=?", (row["channel"],)
            ).fetchone()
            last_report = conn.execute(
                "SELECT * FROM checkpoint_reports WHERE consumer_id=? "
                "ORDER BY report_id DESC LIMIT 1",
                (row["consumer_id"],),
            ).fetchone()
            subscription_rows.append(
                {
                    "budget": int(row["budget"]),
                    "channel": str(row["channel"]),
                    "consumer_id": str(row["consumer_id"]),
                    "current_offset": int(row["current_offset"]),
                    "lag": max(
                        0,
                        (
                            int(channel_state["next_offset"])
                            if channel_state is not None
                            else 0
                        )
                        - int(row["current_offset"]),
                    ),
                    "last_report": (
                        None
                        if last_report is None
                        else {
                            "clock_ms": int(last_report["clock_ms"]),
                            "current_offset": int(last_report["current_offset"]),
                            "outcome": str(last_report["outcome"]),
                            "reason": str(last_report["reason"]),
                            "record_count": int(last_report["record_count"]),
                            "requested_offset": int(
                                last_report["requested_offset"]
                            ),
                        }
                    ),
                    "start_offset": int(row["start_offset"]),
                }
            )
        cleanup_row = conn.execute(
            "SELECT * FROM cleanup_runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        cleanup_summary: dict[str, Any] | None = None
        if cleanup_row is not None:
            value = parse_json_text(
                str(cleanup_row["summary_json"]), "cleanup_runs.summary"
            )
            cleanup_summary = value if isinstance(value, dict) else None
        pending = [
            {
                "clock_ms": int(row["clock_ms"]),
                "kind": str(row["kind"]),
                "operation_key": str(row["op_key"]),
                "state": str(row["state"]),
            }
            for row in conn.execute(
                "SELECT * FROM operations WHERE state='pending' ORDER BY op_key"
            )
        ]
        result = {
            "bridges": sorted(bridges, key=lambda item: item["rule_id"]),
            "channels": channel_rows,
            "cleanup": {
                "last_run": cleanup_summary,
                "watermarks": {
                    item["channel"]: item["cleaned_through"]
                    for item in sorted(channel_rows, key=lambda item: item["channel"])
                },
            },
            "command": "status",
            "commit_seq": get_meta_int(conn, "commit_seq", 0),
            "config": {
                "digest": digest,
                "version": version,
            },
            "edit_version": get_meta_int(conn, "edit_version", 0),
            "logical_clock": get_meta_int(conn, "logical_clock", 0),
            "pending_operations": pending,
            "sample_digest": get_meta(conn, "sample_digest"),
            "subscriptions": sorted(
                subscription_rows, key=lambda item: item["consumer_id"]
            ),
        }
        result["state_digest"] = compute_state_digest(conn)
        return result

    # --------------------------------------------------------------- verify

    def _verify_in_tx(self, conn: sqlite3.Connection) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []

        def err(code: str, message: str, **details: Any) -> None:
            item: dict[str, Any] = {"code": code, "message": message}
            if details:
                item["details"] = details
            errors.append(item)

        try:
            version, digest, channels, rules, subscriptions, _ = load_config(conn)
        except EventBusError as exc:
            return {
                "checks": {"config": False},
                "counts": {},
                "errors": [
                    {
                        "code": exc.code,
                        "message": exc.message,
                        "details": exc.details,
                    }
                ],
                "ok": False,
                "sample_digest": get_meta(conn, "sample_digest"),
                "state_digest": compute_state_digest(conn),
            }
        self._last_sample_digest = get_meta(conn, "sample_digest")
        try:
            check_bridge_cycles(rules)
        except EventBusError as exc:
            err(exc.code, exc.message, **exc.details)
        calculated_config_digest = config_digest(channels, rules, subscriptions)
        if calculated_config_digest != digest:
            err(
                "E_VERIFY",
                "current configuration digest is inconsistent",
                actual=calculated_config_digest,
                stored=digest,
            )
        for config_row in conn.execute(
            "SELECT * FROM config_versions ORDER BY version"
        ):
            config_version = int(config_row["version"])
            try:
                catalog_value = parse_json_text(
                    str(config_row["catalog_json"]), "config.catalog"
                )
                rules_value = parse_json_text(
                    str(config_row["rules_json"]), "config.rules"
                )
                subscriptions_value = parse_json_text(
                    str(config_row["subscriptions_json"]), "config.subscriptions"
                )
                if not isinstance(catalog_value, list) or not isinstance(
                    rules_value, list
                ) or not isinstance(subscriptions_value, list):
                    raise ValueError("snapshot arrays are malformed")
                snapshot_channels = [
                    Channel(
                        name=item["name"],
                        topic=item["topic"],
                        partitions=int(item["partitions"]),
                        retention_bytes=int(item["retention_bytes"]),
                        max_batch_records=int(item["max_batch_records"]),
                    )
                    for item in catalog_value
                ]
                snapshot_rules = [
                    BridgeRule(
                        id=item["id"],
                        upstream=item["upstream"],
                        downstream=item["downstream"],
                        filter_expr=item["filter"],
                        transform_expr=item["transform"],
                        depends_upstream_offset=int(item["depends_upstream_offset"]),
                    )
                    for item in rules_value
                ]
                snapshot_subscriptions = [
                    Subscription(
                        consumer_id=item["consumer_id"],
                        channel=item["channel"],
                        start_offset=int(item["start_offset"]),
                        budget=int(item["budget"]),
                    )
                    for item in subscriptions_value
                ]
                snapshot_digest = config_digest(
                    snapshot_channels, snapshot_rules, snapshot_subscriptions
                )
                if snapshot_digest != str(config_row["digest"]):
                    err(
                        "E_VERIFY",
                        "historical configuration digest mismatch",
                        config_version=config_version,
                        calculated=snapshot_digest,
                        stored=str(config_row["digest"]),
                    )
                stored_channel_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM channel_versions WHERE config_version=?",
                        (config_version,),
                    ).fetchone()[0]
                )
                stored_rule_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM rule_versions WHERE config_version=?",
                        (config_version,),
                    ).fetchone()[0]
                )
                if (
                    stored_channel_count != len(snapshot_channels)
                    or stored_rule_count != len(snapshot_rules)
                ):
                    err(
                        "E_VERIFY",
                        "configuration snapshot row counts are inconsistent",
                        config_version=config_version,
                        channel_rows=stored_channel_count,
                        channel_count=len(snapshot_channels),
                        rule_rows=stored_rule_count,
                        rule_count=len(snapshot_rules),
                    )
            except (KeyError, TypeError, ValueError, EventBusError) as exc:
                err(
                    "E_VERIFY",
                    "historical configuration snapshot is malformed",
                    config_version=config_version,
                    reason=str(exc),
                )
        channel_map = {channel.name: channel for channel in channels}
        if len(channel_map) != len(channels):
            err("E_VERIFY", "channel names are not unique")
        if len({rule.id for rule in rules}) != len(rules):
            err("E_VERIFY", "bridge rule ids are not unique")
        if len({sub.consumer_id for sub in subscriptions}) != len(subscriptions):
            err("E_VERIFY", "consumer ids are not unique")
        stored_channel_versions = {
            str(row["name"]): row
            for row in conn.execute(
                "SELECT * FROM channel_versions WHERE config_version=?",
                (version,),
            )
        }
        for channel in channels:
            row = stored_channel_versions.get(channel.name)
            if row is None:
                err(
                    "E_VERIFY",
                    "current configuration is missing a channel version row",
                    channel=channel.name,
                )
                continue
            if (
                str(row["topic"]) != channel.topic
                or int(row["partitions"]) != channel.partitions
                or int(row["retention_bytes"]) != channel.retention_bytes
                or int(row["max_batch_records"]) != channel.max_batch_records
                or str(row["digest"]) != channel.digest
            ):
                err(
                    "E_VERIFY",
                    "channel version row does not match the current catalog",
                    channel=channel.name,
                )
        stored_rule_versions = {
            str(row["rule_id"]): row
            for row in conn.execute(
                "SELECT * FROM rule_versions WHERE config_version=?",
                (version,),
            )
        }
        for rule in rules:
            row = stored_rule_versions.get(rule.id)
            if row is None:
                err(
                    "E_VERIFY",
                    "current configuration is missing a bridge rule version row",
                    rule_id=rule.id,
                )
                continue
            if (
                str(row["upstream"]) != rule.upstream
                or str(row["downstream"]) != rule.downstream
                or str(row["filter_expr"]) != rule.filter_expr
                or str(row["transform_expr"]) != rule.transform_expr
                or int(row["depends_upstream_offset"])
                != rule.depends_upstream_offset
                or str(row["digest"]) != rule.digest
            ):
                err(
                    "E_VERIFY",
                    "bridge rule version row does not match the current snapshot",
                    rule_id=rule.id,
                )

        total_records = 0
        total_live = 0
        total_tombstones = 0
        for channel in channels:
            state = conn.execute(
                "SELECT * FROM channels WHERE name=?", (channel.name,)
            ).fetchone()
            if state is None:
                err("E_VERIFY", "channel state is missing", channel=channel.name)
                continue
            next_offset = int(state["next_offset"])
            cleaned = int(state["cleaned_through"])
            if cleaned < 0 or cleaned > next_offset:
                err(
                    "E_VERIFY",
                    "cleanup watermark is outside the channel range",
                    channel=channel.name,
                    cleaned_through=cleaned,
                    end_offset=next_offset,
                )
            records = list(
                conn.execute(
                    "SELECT * FROM records WHERE channel=? ORDER BY offset",
                    (channel.name,),
                )
            )
            by_offset = {int(row["offset"]): row for row in records}
            total_records += len(records)
            for offset, row in by_offset.items():
                if offset < 0 or offset >= next_offset:
                    err(
                        "E_VERIFY",
                        "record offset is outside the committed range",
                        channel=channel.name,
                        offset=offset,
                        end_offset=next_offset,
                    )
                if int(row["partition"]) < 0 or int(row["partition"]) >= channel.partitions:
                    err(
                        "E_VERIFY",
                        "record partition is outside the channel partition count",
                        channel=channel.name,
                        offset=offset,
                    )
                else:
                    try:
                        partition_key = parse_json_text(
                            str(row["partition_key_json"]), "records.partition_key"
                        )
                        expected_partition = partition_for(
                            normalize_partition_key(partition_key), channel.partitions
                        )
                        if expected_partition != int(row["partition"]):
                            err(
                                "E_VERIFY",
                                "record partition does not match deterministic routing",
                                channel=channel.name,
                                offset=offset,
                                expected_partition=expected_partition,
                            )
                    except EventBusError as exc:
                        err(exc.code, exc.message, **exc.details)
                if str(row["config_digest"]) != channel.digest:
                    err(
                        "E_VERIFY",
                        "record channel configuration digest is stale",
                        channel=channel.name,
                        offset=offset,
                    )
                if row["state"] == "live":
                    total_live += 1
                    try:
                        payload = _record_payload(row)
                        if _sha(payload) != row["payload_digest"]:
                            err(
                                "E_VERIFY",
                                "record payload digest mismatch",
                                channel=channel.name,
                                offset=offset,
                            )
                    except EventBusError as exc:
                        err(exc.code, exc.message, **exc.details)
                elif row["state"] == "tombstone":
                    total_tombstones += 1
                    if row["payload_json"] is not None:
                        err(
                            "E_VERIFY",
                            "tombstone still contains a payload",
                            channel=channel.name,
                            offset=offset,
                        )
            if next_offset > cleaned:
                for offset in range(cleaned, next_offset):
                    if offset not in by_offset:
                        err(
                            "E_VERIFY",
                            "committed log has a gap above the cleanup watermark",
                            channel=channel.name,
                            offset=offset,
                        )
                        break

        published = {
            int(row["seq"]): row
            for row in conn.execute("SELECT * FROM published_events ORDER BY seq")
        }
        producer_records = {
            str(row["origin_ref"]): row
            for row in conn.execute(
                "SELECT * FROM records WHERE origin_type='producer' ORDER BY channel, offset"
            )
        }
        for origin_ref, row in producer_records.items():
            try:
                topic, seq_text = origin_ref.rsplit(":", 1)
                seq = int(seq_text)
            except (ValueError, AttributeError):
                err("E_VERIFY", "producer record origin_ref is malformed", ref=origin_ref)
                continue
            published_row = published.get(seq)
            if published_row is None:
                err(
                    "E_VERIFY",
                    "producer record has no published_events mapping",
                    ref=origin_ref,
                )
                continue
            if (
                str(published_row["channel"]) != str(row["channel"])
                or int(published_row["record_offset"]) != int(row["offset"])
                or str(published_row["source_digest"]) != str(row["source_digest"])
                or str(published_row["payload_digest"]) != str(row["payload_digest"])
            ):
                err(
                    "E_VERIFY",
                    "published_events mapping does not match the log",
                    seq=seq,
                )
            if str(published_row["topic"]) != topic:
                err("E_VERIFY", "published event topic does not match origin", seq=seq)

        state_by_rule = {
            str(row["rule_id"]): row
            for row in conn.execute("SELECT * FROM bridge_state ORDER BY rule_id")
        }
        for rule_id, row in state_by_rule.items():
            upstream_channel = str(row["upstream_channel"])
            channel_state = conn.execute(
                "SELECT next_offset FROM channels WHERE name=?", (upstream_channel,)
            ).fetchone()
            if channel_state is None:
                err(
                    "E_VERIFY",
                    "bridge state refers to a missing upstream channel",
                    rule_id=rule_id,
                    channel=upstream_channel,
                )
            elif int(row["next_offset"]) > int(channel_state["next_offset"]):
                err(
                    "E_VERIFY",
                    "bridge state is beyond the committed upstream watermark",
                    rule_id=rule_id,
                    bridge_offset=int(row["next_offset"]),
                    upstream_end=int(channel_state["next_offset"]),
                )
            actions = list(
                conn.execute(
                    "SELECT * FROM bridge_actions WHERE rule_id=? ORDER BY source_offset",
                    (rule_id,),
                )
            )
            next_offset = int(row["next_offset"])
            offsets = [int(action["source_offset"]) for action in actions]
            if offsets != list(range(next_offset)):
                err(
                    "E_VERIFY",
                    "bridge action offsets are not contiguous",
                    rule_id=rule_id,
                    next_offset=next_offset,
                    action_offsets=offsets[:50],
                )
            chain = _initial_chain(rule_id)
            for action in actions:
                if int(action["source_offset"]) >= next_offset:
                    continue
                chain = _advance_chain(
                    chain, str(action["action_digest"]), int(action["source_offset"])
                )
                if action["status"] == "forwarded":
                    record = conn.execute(
                        "SELECT * FROM records WHERE channel=? AND offset=?",
                        (
                            action["downstream_channel"],
                            action["downstream_offset"],
                        ),
                    ).fetchone()
                    if record is None:
                        err(
                            "E_VERIFY",
                            "forwarded bridge action has no downstream record",
                            rule_id=rule_id,
                            source_offset=int(action["source_offset"]),
                        )
                    else:
                        if str(record["source_digest"]) != str(action["action_digest"]):
                            err(
                                "E_VERIFY",
                                "downstream record source digest mismatch",
                                rule_id=rule_id,
                                source_offset=int(action["source_offset"]),
                            )
                        if str(record["payload_digest"]) != str(
                            action["transformed_digest"]
                        ):
                            err(
                                "E_VERIFY",
                                "downstream transformed digest mismatch",
                                rule_id=rule_id,
                                source_offset=int(action["source_offset"]),
                            )
                elif action["status"] == "skipped":
                    if action["downstream_offset"] is not None:
                        err(
                            "E_VERIFY",
                            "skipped bridge action has a downstream offset",
                            rule_id=rule_id,
                        )
            if chain != str(row["chain_digest"]):
                err(
                    "E_VERIFY",
                    "bridge chain digest mismatch",
                    rule_id=rule_id,
                    actual=chain,
                    stored=str(row["chain_digest"]),
                )

        for subscription in subscriptions:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE consumer_id=?",
                (subscription.consumer_id,),
            ).fetchone()
            if row is None:
                err(
                    "E_VERIFY",
                    "subscription declaration is missing runtime state",
                    consumer_id=subscription.consumer_id,
                )
                continue
            channel = channel_map.get(subscription.channel)
            if channel is None:
                err(
                    "E_VERIFY",
                    "subscription channel is absent from catalog",
                    consumer_id=subscription.consumer_id,
                )
                continue
            state = conn.execute(
                "SELECT next_offset, cleaned_through FROM channels WHERE name=?",
                (channel.name,),
            ).fetchone()
            current = int(row["current_offset"])
            start = int(row["start_offset"])
            if state is None:
                err("E_VERIFY", "subscription channel state is missing")
                continue
            if current < start or current > int(state["next_offset"]):
                err(
                    "E_VERIFY",
                    "subscription offset is outside the channel range",
                    consumer_id=subscription.consumer_id,
                    current_offset=current,
                    start_offset=start,
                    end_offset=int(state["next_offset"]),
                )
            if current < int(state["cleaned_through"]):
                err(
                    "E_VERIFY",
                    "subscription offset is behind the cleanup watermark",
                    consumer_id=subscription.consumer_id,
                    current_offset=current,
                    cleaned_through=int(state["cleaned_through"]),
                )

        for channel in channels:
            state = conn.execute(
                "SELECT next_offset, cleaned_through FROM channels WHERE name=?",
                (channel.name,),
            ).fetchone()
            if state is None:
                continue
            cursors = [
                int(row["current_offset"])
                for row in conn.execute(
                    "SELECT current_offset FROM subscriptions WHERE channel=?",
                    (channel.name,),
                )
            ]
            cursors.extend(
                int(row["next_offset"])
                for row in conn.execute(
                    "SELECT next_offset FROM bridge_state WHERE upstream_channel=?",
                    (channel.name,),
                )
            )
            if cursors and int(state["cleaned_through"]) > min(cursors):
                err(
                    "E_VERIFY",
                    "cleanup watermark is beyond an unconsumed cursor",
                    channel=channel.name,
                    cleaned_through=int(state["cleaned_through"]),
                    minimum_cursor=min(cursors),
                )

        watermark = get_meta_int(conn, "logical_clock", 0)
        max_clock_rows = conn.execute(
            "SELECT COALESCE(MAX(value), 0) AS max_value FROM ("
            "SELECT MAX(clock_ms) AS value FROM records "
            "UNION ALL SELECT MAX(created_clock) FROM records "
            "UNION ALL SELECT MAX(clock_ms) FROM source_events "
            "UNION ALL SELECT MAX(updated_clock) FROM channels)"
        ).fetchone()
        max_clock = int(max_clock_rows["max_value"]) if max_clock_rows else 0
        if watermark < max_clock:
            err(
                "E_VERIFY",
                "persisted logical clock watermark is behind stored event clocks",
                watermark=watermark,
                max_stored_clock=max_clock,
            )
        max_edit_version = int(
            conn.execute(
                "SELECT COALESCE(MAX(version), 0) FROM edit_versions"
            ).fetchone()[0]
        )
        if max_edit_version != get_meta_int(conn, "edit_version", 0):
            err(
                "E_VERIFY",
                "edit version watermark does not match the version history",
                watermark=get_meta_int(conn, "edit_version", 0),
                max_edit_version=max_edit_version,
            )

        pending = list_pending_operations(conn)
        if pending:
            err(
                "E_VERIFY",
                "unfinished operations remain and must be resumed",
                operations=[str(row["op_key"]) for row in pending],
            )
        if errors:
            errors.sort(key=lambda item: (item["code"], item["message"], canonical_json(item)))
        return {
            "checks": {
                "bridge_integrity": not any(
                    "bridge" in item["message"].lower() for item in errors
                ),
                "cleanup_integrity": not any(
                    "cleanup" in item["message"].lower() for item in errors
                ),
                "config_integrity": not any(
                    "configuration" in item["message"].lower() for item in errors
                ),
                "log_integrity": not any(
                    "record" in item["message"].lower()
                    or "log" in item["message"].lower()
                    for item in errors
                ),
                "subscription_integrity": not any(
                    "subscription" in item["message"].lower() for item in errors
                ),
            },
            "counts": {
                "bridge_actions": int(
                    conn.execute("SELECT COUNT(*) FROM bridge_actions").fetchone()[0]
                ),
                "bridge_states": len(state_by_rule),
                "channels": len(channels),
                "cleanup_runs": int(
                    conn.execute("SELECT COUNT(*) FROM cleanup_runs").fetchone()[0]
                ),
                "edit_versions": int(
                    conn.execute("SELECT COUNT(*) FROM edit_versions").fetchone()[0]
                ),
                "live_records": total_live,
                "operations": int(
                    conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
                ),
                "published_events": len(published),
                "records": total_records,
                "subscriptions": len(subscriptions),
                "tombstones": total_tombstones,
            },
            "errors": errors,
            "sample_digest": self._last_sample_digest,
            "state_digest": compute_state_digest(conn),
        }

