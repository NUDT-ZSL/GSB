"""Operations for append, bridge, consume, retention, recovery, and verify."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import BusinessError, err
from .expressions import apply_transforms, evaluate_filter, validate_filter, validate_transforms
from .faults import hit, hold_after_bind
from .models import BridgeDef, Config, LoadedDataset, SubscriptionDef
from .store import (
    connect,
    get_config,
    get_config_version,
    initialize_schema,
    latest_config_version,
    stage_config_version,
    transaction,
    update_clock,
)
from .util import (
    canonical_json,
    digest_value,
    ensure_json_value,
    file_lock,
    is_int,
    now_ms,
    payload_digest,
    typed_value,
)
from .validation import _validate_bridge_graph


class EventBus:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def load(
        self,
        dataset: LoadedDataset,
        *,
        replace: bool = False,
        clock_ms: int | None = None,
    ) -> dict[str, Any]:
        with file_lock(self.db_path):
            initialize_schema(self.db_path)
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    pointer = connection.execute(
                        "SELECT version FROM config_pointer WHERE id=1"
                    ).fetchone()
                    if pointer is not None:
                        current_version = int(pointer["version"])
                        row = connection.execute(
                            "SELECT fingerprint FROM config_versions WHERE version=?",
                            (current_version,),
                        ).fetchone()
                        if (
                            row is not None
                            and row["fingerprint"] == dataset.source_fingerprint
                            and not replace
                        ):
                            return {
                                "ok": True,
                                "idempotent": True,
                                "config_version": current_version,
                                "source_fingerprint": dataset.source_fingerprint,
                            }
                        if not replace:
                            raise err(
                                "DATASET_CONFLICT",
                                "数据库已有不同样本，使用 --replace 才会整体替换",
                                existing_version=current_version,
                                existing_fingerprint=row["fingerprint"] if row else None,
                                incoming_fingerprint=dataset.source_fingerprint,
                            )
                        self._clear_all_state(connection)
                    version = 1
                    stage_config_version(
                        connection,
                        dataset.config,
                        version=version,
                        parent_version=None,
                        patch={"kind": "load", "source": dataset.source_fingerprint},
                        created_at_ms=now_ms(),
                        actor="load",
                    )
                    for channel_name in sorted(dataset.config.channels):
                        channel = dataset.config.channels[channel_name]
                        connection.execute(
                            "INSERT INTO channels(name, generation, partitions, field_schema_json, "
                            "config_version, low_watermark, high_watermark, retention_bytes) "
                            "VALUES(?, ?, ?, ?, ?, 0, 0, ?)",
                            (
                                channel_name,
                                channel.generation,
                                channel.partitions,
                                canonical_json(
                                    [item.to_dict() for item in channel.field_schema]
                                ),
                                version,
                                dataset.config.retention_bytes,
                            ),
                        )
                    connection.execute(
                        "INSERT INTO clock_state(id, logical_clock_ms, event_clock_watermark, "
                        "updated_at_ms) VALUES(1, ?, 0, ?)",
                        (clock_ms or 0, now_ms()),
                    )
                    event_clock = 0
                    grouped_events: dict[str, list[Any]] = {}
                    for event in dataset.events:
                        grouped_events.setdefault(event.channel, []).append(event)
                    for channel_name in sorted(grouped_events):
                        for event in sorted(
                            grouped_events[channel_name], key=lambda item: item.sequence
                        ):
                            event_clock = max(event_clock, event.logical_clock)
                            self._insert_event(
                                connection,
                                channel=dataset.config.channels[channel_name],
                                offset=connection.execute(
                                    "SELECT high_watermark FROM channels WHERE name=?",
                                    (channel_name,),
                                ).fetchone()["high_watermark"],
                                sequence=event.sequence,
                                partition_key=event.partition_key,
                                logical_clock=event.logical_clock,
                                produced_at_ms=event.produced_at_ms,
                                fields=event.fields,
                                tombstone=event.tombstone,
                                source_version=event.source_version,
                                source_generation=event.source_generation,
                                raw_repr=event.raw_repr,
                                time_conversion=event.time_conversion,
                                config_version=version,
                                batch_id=f"load:{channel_name}",
                                created_at_ms=now_ms(),
                            )
                            connection.execute(
                                "UPDATE channels SET high_watermark=high_watermark+1 WHERE name=?",
                                (channel_name,),
                            )
                        connection.execute(
                            "INSERT INTO batches(channel, batch_id, start_offset, end_offset, "
                            "event_count, config_version, payload_root, created_at_ms) "
                            "VALUES(?, ?, 0, ?, ?, ?, ?, ?)",
                            (
                                channel_name,
                                f"load:{channel_name}",
                                len(grouped_events[channel_name]),
                                len(grouped_events[channel_name]),
                                version,
                                digest_value(
                                    [
                                        payload_digest(item.fields)
                                        for item in grouped_events[channel_name]
                                    ]
                                ),
                                now_ms(),
                            ),
                        )
                    for decision in dataset.merge_decisions:
                        connection.execute(
                            "INSERT INTO merge_decisions(channel, sequence, selected_source, "
                            "selected_generation, logical_clock, decision_basis, options_json, "
                            "raw_v1_json, raw_v2_json, config_version) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                decision["channel"],
                                decision["sequence"],
                                decision["selected_source"],
                                decision["selected_generation"],
                                decision["logical_clock"],
                                decision["decision_basis"],
                                canonical_json(decision["options"]),
                                canonical_json(decision["raw_v1"])
                                if decision["raw_v1"] is not None
                                else None,
                                canonical_json(decision["raw_v2"])
                                if decision["raw_v2"] is not None
                                else None,
                                version,
                            ),
                        )
                    for item in dataset.degradations:
                        connection.execute(
                            "INSERT INTO degradations(channel, sequence, field, source_version, "
                            "action, default_json, reason, config_version) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                item["channel"],
                                item["sequence"],
                                item["field"],
                                item["source_version"],
                                item["action"],
                                canonical_json(item["default"]),
                                item["reason"],
                                version,
                            ),
                        )
                    for bridge_id in sorted(dataset.config.bridges):
                        bridge = dataset.config.bridges[bridge_id]
                        connection.execute(
                            "INSERT INTO bridge_positions(bridge_id, source_channel, "
                            "target_channel, next_offset, last_digest, source_generation, "
                            "target_generation, config_version, active, updated_at_ms) "
                            "VALUES(?, ?, ?, 0, NULL, ?, ?, ?, 1, ?)",
                            (
                                bridge_id,
                                bridge.source,
                                bridge.target,
                                bridge.source_generation,
                                bridge.target_generation,
                                version,
                                now_ms(),
                            ),
                        )
                    for name in sorted(dataset.config.subscriptions):
                        subscription = dataset.config.subscriptions[name]
                        connection.execute(
                            "INSERT INTO subscriptions(name, channel, offset, config_version, "
                            "active, updated_at_ms) VALUES(?, ?, ?, ?, 1, ?)",
                            (
                                name,
                                subscription.channel,
                                subscription.start_offset,
                                version,
                                now_ms(),
                            ),
                        )
                    update_clock(
                        connection,
                        clock_ms,
                        dataset.config.clock_tolerance_ms,
                        event_clock=event_clock,
                    )
                    hit("load_before_commit")
                hit("load_after_commit")
                return {
                    "ok": True,
                    "idempotent": False,
                    "config_version": 1,
                    "source_fingerprint": dataset.source_fingerprint,
                    "channels": len(dataset.config.channels),
                    "events": len(dataset.events),
                    "bridges": len(dataset.config.bridges),
                    "subscriptions": len(dataset.config.subscriptions),
                    "degradations": len(dataset.degradations),
                }
            finally:
                connection.close()

    def publish_events(
        self,
        channel: str,
        document: Any,
        *,
        max_batch: int | None = None,
        clock_ms: int | None = None,
    ) -> dict[str, Any]:
        specs = document.get("events") if isinstance(document, dict) else document
        if not isinstance(specs, list) or not specs:
            raise err("MALFORMED_EVENT", "publish 事件必须为非空数组")
        payload = {
            "channel": channel,
            "document": document,
            "max_batch": max_batch,
            "clock_ms": clock_ms,
        }
        intent_id = self._start_operation("append", payload)
        try:
            return self._publish_events_intent(intent_id, payload)
        except BusinessError as exc:
            self._fail_operation(intent_id, exc)
            raise

    def _publish_events_intent(
        self, intent_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        channel = payload["channel"]
        document = payload["document"]
        max_batch = payload.get("max_batch")
        clock_ms = payload.get("clock_ms")
        specs = document.get("events") if isinstance(document, dict) else document
        if not isinstance(specs, list) or not specs:
            raise err("MALFORMED_EVENT", "publish 事件必须为非空数组")
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    completed = self._completed_operation(connection, intent_id)
                    if completed is not None:
                        return completed
                    version, config = get_config(connection)
                    if channel not in config.channels:
                        raise err("NOT_FOUND", "通道不存在", channel=channel)
                    channel_def = config.channels[channel]
                    batch_size = max_batch or max(1, min(100, len(specs)))
                    if not is_int(batch_size) or batch_size <= 0:
                        raise err("PATCH_INVALID", "max_batch 必须为正整数")
                    effective_clock = update_clock(
                        connection, clock_ms, config.clock_tolerance_ms
                    )
                    results, appended, last_clock = self._append_specs(
                        connection,
                        config,
                        channel_def,
                        specs,
                        config_version=version,
                        batch_size=batch_size,
                        created_at_ms=now_ms(),
                        default_produced_at_ms=effective_clock,
                    )
                    update_clock(
                        connection,
                        effective_clock,
                        config.clock_tolerance_ms,
                        event_clock=last_clock,
                    )
                    result = {
                        "ok": True,
                        "channel": channel,
                        "appended": appended,
                        "results": results,
                        "config_version": version,
                    }
                    self._complete_operation(connection, intent_id, result)
                    hit("append_before_commit")
                hit("append_after_commit")
                return result
            finally:
                connection.close()

    def edit(
        self,
        patch: dict[str, Any],
        *,
        clock_ms: int | None = None,
        actor: str = "edit",
    ) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise err("PATCH_INVALID", "补丁必须为对象")
        events_patch = patch.get("events", [])
        bridge_patch = patch.get("bridges", [])
        subscription_patch = patch.get("subscriptions", [])
        for name, value in (
            ("events", events_patch),
            ("bridges", bridge_patch),
            ("subscriptions", subscription_patch),
        ):
            if not isinstance(value, list):
                raise err("PATCH_INVALID", f"{name} 必须为数组")
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    parent_version, config = get_config(connection)
                    version = latest_config_version(connection) + 1
                    effective_clock = update_clock(
                        connection, clock_ms, config.clock_tolerance_ms
                    )
                    summary: dict[str, Any] = {
                        "events": {"add": 0, "update": 0, "delete": 0},
                        "bridges": {"add": 0, "update": 0, "delete": 0},
                        "subscriptions": {"add": 0, "update": 0, "delete": 0},
                    }
                    last_clock = self._apply_event_patches(
                        connection,
                        config,
                        events_patch,
                        config_version=version,
                        created_at_ms=now_ms(),
                        default_produced_at_ms=effective_clock,
                        summary=summary,
                    )
                    self._apply_bridge_patches(
                        connection, config, bridge_patch, version, summary
                    )
                    self._apply_subscription_patches(
                        connection, config, subscription_patch, version, summary
                    )
                    _validate_bridge_graph(config.bridges)
                    stage_config_version(
                        connection,
                        config,
                        version=version,
                        parent_version=parent_version,
                        patch={
                            "patch_id": patch.get(
                                "patch_id", f"patch-{parent_version}-{version}"
                            ),
                            "operations": summary,
                            "body": patch,
                        },
                        created_at_ms=now_ms(),
                        actor=actor,
                    )
                    update_clock(
                        connection,
                        effective_clock,
                        config.clock_tolerance_ms,
                        event_clock=last_clock,
                    )
                    hit("edit_before_commit")
                hit("edit_after_commit")
                return {
                    "ok": True,
                    "config_version": version,
                    "parent_version": parent_version,
                    "patch_id": patch.get("patch_id", f"patch-{parent_version}-{version}"),
                    "operations": summary,
                }
            finally:
                connection.close()

    def publish_version(
        self, version: int, *, clock_ms: int | None = None
    ) -> dict[str, Any]:
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    current = get_config_version(connection)
                    _, config = get_config(connection, version)
                    if current == version:
                        return {
                            "ok": True,
                            "idempotent": True,
                            "config_version": version,
                        }
                    self._reconcile_runtime_config(connection, config, version)
                    now = now_ms()
                    connection.execute(
                        "CREATE TEMP TABLE IF NOT EXISTS config_stage ("
                        "version INTEGER PRIMARY KEY, parent_version INTEGER, config_json TEXT, "
                        "patch_json TEXT, fingerprint TEXT, created_at_ms INTEGER, actor TEXT)"
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO config_stage "
                        "SELECT version, parent_version, config_json, patch_json, fingerprint, "
                        "created_at_ms, actor FROM config_versions WHERE version=?",
                        (version,),
                    )
                    connection.execute(
                        "UPDATE config_pointer SET version=? WHERE id=1", (version,)
                    )
                    connection.execute("DROP TABLE config_stage")
                    update_clock(connection, clock_ms, config.clock_tolerance_ms)
                return {
                    "ok": True,
                    "idempotent": False,
                    "config_version": version,
                    "previous_version": current,
                }
            finally:
                connection.close()

    def run_bridge(
        self,
        bridge_id: str,
        *,
        max_events: int | None = None,
        clock_ms: int | None = None,
    ) -> dict[str, Any]:
        version, config, bridge, start_offset = self._bind_bridge(bridge_id)
        run_id = self._create_bridge_intent(
            bridge_id, version, start_offset
        )
        hit("bridge_after_intent")
        hold_after_bind()
        return self._execute_bridge_run(
            run_id,
            bridge_id=bridge_id,
            bound_version=version,
            max_events=max_events,
            clock_ms=clock_ms,
        )

    def run_bridge_cycle(
        self,
        bridge_ids: list[str],
        *,
        max_events: int | None = None,
        max_runs: int = 1000,
        clock_ms: int | None = None,
    ) -> dict[str, Any]:
        payload = {
            "bridge_ids": bridge_ids,
            "max_events": max_events,
            "max_runs": max_runs,
            "clock_ms": clock_ms,
        }
        intent_id = self._start_operation("bridge-cycle", payload)
        try:
            return self._run_bridge_cycle_intent(intent_id, payload)
        except BusinessError as exc:
            self._fail_operation(intent_id, exc)
            raise

    def _run_bridge_cycle_intent(
        self, intent_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        connection = connect(self.db_path)
        try:
            completed = self._completed_operation(connection, intent_id)
            if completed is not None:
                return completed
        finally:
            connection.close()
        bridge_ids = list(payload["bridge_ids"])
        max_events = payload.get("max_events")
        max_runs = int(payload.get("max_runs", 1000))
        clock_ms = payload.get("clock_ms")
        runs: list[dict[str, Any]] = []
        for bridge_id in bridge_ids:
            run_count = 0
            limit = 1 if max_events is not None else max_runs
            while run_count < limit:
                result = self.run_bridge(
                    bridge_id,
                    max_events=max_events,
                    clock_ms=clock_ms,
                )
                runs.append(result)
                run_count += 1
                if result["next_offset"] == result["start_offset"]:
                    break
        result = {
            "ok": True,
            "bridges": bridge_ids,
            "runs": runs,
        }
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    stored = self._completed_operation(connection, intent_id)
                    if stored is not None:
                        return stored
                    self._complete_operation(connection, intent_id, result)
            finally:
                connection.close()
        return result

    def report_bridge_position(
        self,
        bridge_id: str,
        to_offset: int,
        *,
        expected_digest: str | None = None,
        config_version: int | None = None,
    ) -> dict[str, Any]:
        if not is_int(to_offset) or to_offset < 0:
            raise err("BRIDGE_POSITION_REWIND", "桥接位点必须为非负整数")
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    version, config = get_config(connection)
                    if config_version is not None and version != config_version:
                        raise err(
                            "CONFIG_CHANGED",
                            "位点回报绑定的配置版本已变化",
                            bound_version=config_version,
                            current_version=version,
                        )
                    bridge = config.bridges.get(bridge_id)
                    if bridge is None:
                        raise err("NOT_FOUND", "桥接规则不存在", bridge=bridge_id)
                    position = connection.execute(
                        "SELECT * FROM bridge_positions WHERE bridge_id=? AND active=1",
                        (bridge_id,),
                    ).fetchone()
                    if position is None:
                        raise err("NOT_FOUND", "桥接位点不存在", bridge=bridge_id)
                    current = int(position["next_offset"])
                    if to_offset < current:
                        raise err(
                            "BRIDGE_POSITION_REWIND",
                            "桥接位点不能回退",
                            bridge=bridge_id,
                            current_offset=current,
                            reported_offset=to_offset,
                        )
                    high = int(
                        connection.execute(
                            "SELECT high_watermark FROM channels WHERE name=?",
                            (bridge.source,),
                        ).fetchone()["high_watermark"]
                    )
                    if to_offset > high:
                        raise err(
                            "BRIDGE_UPSTREAM_WATERMARK",
                            "桥接位点超过上游提交水位",
                            bridge=bridge_id,
                            reported_offset=to_offset,
                            high_watermark=high,
                        )
                    if to_offset == current:
                        if (
                            expected_digest is not None
                            and expected_digest != position["last_digest"]
                        ):
                            raise err(
                                "BRIDGE_DIGEST_MISMATCH",
                                "位点回报摘要不匹配",
                                bridge=bridge_id,
                                expected=position["last_digest"],
                                actual=expected_digest,
                            )
                        return {
                            "ok": True,
                            "idempotent": True,
                            "bridge_id": bridge_id,
                            "next_offset": current,
                            "last_digest": position["last_digest"],
                        }
                    raise err(
                        "BRIDGE_UPSTREAM_WATERMARK",
                        "位点回报不能越过已提交的桥接扫描摘要",
                        bridge=bridge_id,
                        current_offset=current,
                        reported_offset=to_offset,
                    )
            finally:
                connection.close()

    def resume(self) -> dict[str, Any]:
        connection = connect(self.db_path)
        try:
            pending_operations = connection.execute(
                "SELECT id, kind, payload_json FROM operation_intents "
                "WHERE status='pending' ORDER BY id"
            ).fetchall()
            rows = connection.execute(
                "SELECT id, bridge_id, config_version FROM bridge_runs "
                "WHERE status='running' ORDER BY id"
            ).fetchall()
        finally:
            connection.close()
        recovered_operations: list[dict[str, Any]] = []
        operation_error = False
        for operation in pending_operations:
            intent_id = int(operation["id"])
            kind = operation["kind"]
            payload = json.loads(operation["payload_json"])
            try:
                if kind == "append":
                    result = self._publish_events_intent(intent_id, payload)
                elif kind == "consume":
                    result = self._consume_intent(intent_id, payload)
                elif kind == "cleanup":
                    result = self._cleanup_intent(intent_id, payload)
                elif kind == "bridge-cycle":
                    result = self._run_bridge_cycle_intent(intent_id, payload)
                else:
                    raise err("INTERNAL_ERROR", "未知待恢复操作类型", kind=kind)
                recovered_operations.append(
                    {
                        "intent_id": intent_id,
                        "kind": kind,
                        "status": "completed",
                        "result": result,
                    }
                )
            except BusinessError as exc:
                operation_error = True
                self._fail_operation(intent_id, exc)
                recovered_operations.append(
                    {
                        "intent_id": intent_id,
                        "kind": kind,
                        "status": "failed",
                        "error_code": exc.code,
                        "details": exc.details,
                    }
                )
        recovered: list[dict[str, Any]] = []
        bridge_error = False
        for row in rows:
            run_id = int(row["id"])
            bridge_id = str(row["bridge_id"])
            bound_version = int(row["config_version"])
            try:
                result = self._execute_bridge_run(
                    run_id,
                    bridge_id=bridge_id,
                    bound_version=bound_version,
                    max_events=None,
                    clock_ms=None,
                )
                recovered.append({"run_id": run_id, "status": "completed", **result})
            except BusinessError as exc:
                bridge_error = True
                recovered.append(
                    {
                        "run_id": run_id,
                        "bridge_id": bridge_id,
                        "status": "aborted",
                        "error_code": exc.code,
                        "details": exc.details,
                    }
                )
        connection = connect(self.db_path)
        try:
            remaining = connection.execute(
                "SELECT COUNT(*) AS n FROM bridge_runs WHERE status='running'"
            ).fetchone()["n"]
            remaining_operations = connection.execute(
                "SELECT COUNT(*) AS n FROM operation_intents WHERE status='pending'"
            ).fetchone()["n"]
            high_watermarks = {
                row["name"]: {
                    "low": int(row["low_watermark"]),
                    "high": int(row["high_watermark"]),
                }
                for row in connection.execute(
                    "SELECT name, low_watermark, high_watermark FROM channels ORDER BY name"
                )
            }
        finally:
            connection.close()
        return {
            "ok": (
                remaining == 0
                and remaining_operations == 0
                and not operation_error
                and not bridge_error
            ),
            "recovered_operations": recovered_operations,
            "recovered": recovered,
            "remaining_running": int(remaining),
            "remaining_pending_operations": int(remaining_operations),
            "channels": high_watermarks,
        }

    def consume(
        self,
        subscription_name: str,
        *,
        count: int | None = None,
        to_offset: int | None = None,
        request_id: str | None = None,
        clock_ms: int | None = None,
    ) -> dict[str, Any]:
        if (count is None) == (to_offset is None):
            raise err("PATCH_INVALID", "count 与 to_offset 必须且只能指定一个")
        if count is not None and (not is_int(count) or count < 0):
            raise err("PATCH_INVALID", "count 必须为非负整数")
        if to_offset is not None and (not is_int(to_offset) or to_offset < 0):
            raise err("SUBSCRIPTION_OFFSET_OUT_OF_RANGE", "目标偏移必须为非负整数")
        payload = {
            "subscription_name": subscription_name,
            "count": count,
            "to_offset": to_offset,
            "request_id": request_id,
            "clock_ms": clock_ms,
        }
        intent_id = self._start_operation("consume", payload)
        try:
            return self._consume_intent(intent_id, payload)
        except BusinessError as exc:
            self._fail_operation(intent_id, exc)
            raise

    def _consume_intent(
        self, intent_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        subscription_name = payload["subscription_name"]
        count = payload.get("count")
        to_offset = payload.get("to_offset")
        request_id = payload.get("request_id")
        clock_ms = payload.get("clock_ms")
        if (count is None) == (to_offset is None):
            raise err("PATCH_INVALID", "count 与 to_offset 必须且只能指定一个")
        if count is not None and (not is_int(count) or count < 0):
            raise err("PATCH_INVALID", "count 必须为非负整数")
        if to_offset is not None and (not is_int(to_offset) or to_offset < 0):
            raise err("SUBSCRIPTION_OFFSET_OUT_OF_RANGE", "目标偏移必须为非负整数")
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    completed = self._completed_operation(connection, intent_id)
                    if completed is not None:
                        return completed

                    def finish(result: dict[str, Any]) -> dict[str, Any]:
                        self._complete_operation(connection, intent_id, result)
                        return result

                    version, config = get_config(connection)
                    if subscription_name not in config.subscriptions:
                        raise err(
                            "NOT_FOUND", "订阅不存在", subscription=subscription_name
                        )
                    subscription = config.subscriptions[subscription_name]
                    row = connection.execute(
                        "SELECT channel, offset, active FROM subscriptions WHERE name=?",
                        (subscription_name,),
                    ).fetchone()
                    if row is None or not row["active"]:
                        raise err(
                            "NOT_FOUND", "订阅运行状态不存在", subscription=subscription_name
                        )
                    if row["channel"] != subscription.channel:
                        raise err(
                            "CONFIG_CHANGED",
                            "订阅通道与配置版本不一致",
                            subscription=subscription_name,
                        )
                    high = int(
                        connection.execute(
                            "SELECT high_watermark FROM channels WHERE name=?",
                            (subscription.channel,),
                        ).fetchone()["high_watermark"]
                    )
                    current = int(row["offset"])
                    target = (
                        min(current + count, high) if count is not None else int(to_offset)
                    )
                    if target > high or target < 0:
                        raise err(
                            "SUBSCRIPTION_OFFSET_OUT_OF_RANGE",
                            "订阅目标偏移越界",
                            channel=subscription.channel,
                            target_offset=target,
                            high_watermark=high,
                        )
                    existing_commit = connection.execute(
                        "SELECT request_id, from_offset, to_offset FROM subscription_commits "
                        "WHERE subscription_name=? AND to_offset=? LIMIT 1",
                        (subscription_name, target),
                    ).fetchone()
                    if target < current:
                        if existing_commit is not None:
                            return finish({
                                "ok": True,
                                "idempotent": True,
                                "subscription": subscription_name,
                                "offset": current,
                                "target_offset": target,
                                "events": [],
                            })
                        raise err(
                            "SUBSCRIPTION_REWIND",
                            "订阅偏移不能回退",
                            subscription=subscription_name,
                            current_offset=current,
                            target_offset=target,
                        )
                    if request_id is not None:
                        request_row = connection.execute(
                            "SELECT from_offset, to_offset FROM subscription_commits "
                            "WHERE subscription_name=? AND request_id=?",
                            (subscription_name, request_id),
                        ).fetchone()
                        if request_row is not None:
                            if int(request_row["to_offset"]) != target:
                                raise err(
                                    "SUBSCRIPTION_CONFLICT",
                                    "同一 request_id 对应不同目标偏移",
                                    request_id=request_id,
                                )
                            return finish({
                                "ok": True,
                                "idempotent": True,
                                "subscription": subscription_name,
                                "offset": current,
                                "target_offset": target,
                                "events": [],
                            })
                    if target == current:
                        return finish({
                            "ok": True,
                            "idempotent": True,
                            "subscription": subscription_name,
                            "offset": current,
                            "target_offset": target,
                            "events": [],
                        })
                    events = []
                    rows = connection.execute(
                        "SELECT * FROM events WHERE channel=? AND offset>=? AND offset<? "
                        "ORDER BY offset",
                        (subscription.channel, current, target),
                    ).fetchall()
                    if len(rows) != target - current:
                        raise err(
                            "VERIFY_FAILED",
                            "订阅区间内事件缺失",
                            channel=subscription.channel,
                            from_offset=current,
                            to_offset=target,
                            found=len(rows),
                        )
                    for event_row in rows:
                        events.append(self._event_for_output(event_row))
                    now = now_ms()
                    connection.execute(
                        "UPDATE subscriptions SET offset=?, config_version=?, updated_at_ms=? "
                        "WHERE name=? AND offset=?",
                        (target, version, now, subscription_name, current),
                    )
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        raise err(
                            "CONCURRENT_MODIFICATION",
                            "订阅偏移在事务内被其他写入者修改",
                            subscription=subscription_name,
                        )
                    effective_request_id = request_id or (
                        f"auto:{subscription_name}:{current}:{target}"
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO subscription_commits("
                        "subscription_name, request_id, from_offset, to_offset, created_at_ms) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (
                            subscription_name,
                            effective_request_id,
                            current,
                            target,
                            now,
                        ),
                    )
                    update_clock(connection, clock_ms, config.clock_tolerance_ms)
                    result = {
                        "ok": True,
                        "idempotent": False,
                        "subscription": subscription_name,
                        "channel": subscription.channel,
                        "from_offset": current,
                        "offset": target,
                        "target_offset": target,
                        "events": events,
                    }
                    self._complete_operation(connection, intent_id, result)
                    hit("consume_before_commit")
                hit("consume_after_commit")
                return result
            finally:
                connection.close()

    def cleanup(
        self,
        *,
        clock_ms: int | None = None,
        retention_bytes: int | None = None,
    ) -> dict[str, Any]:
        payload = {"clock_ms": clock_ms, "retention_bytes": retention_bytes}
        intent_id = self._start_operation("cleanup", payload)
        try:
            return self._cleanup_intent(intent_id, payload)
        except BusinessError as exc:
            self._fail_operation(intent_id, exc)
            raise

    def _cleanup_intent(
        self, intent_id: int, payload: dict[str, Any]
    ) -> dict[str, Any]:
        clock_ms = payload.get("clock_ms")
        retention_bytes = payload.get("retention_bytes")
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    completed = self._completed_operation(connection, intent_id)
                    if completed is not None:
                        return completed
                    _, config = get_config(connection)
                    if retention_bytes is None:
                        retention = config.retention_bytes
                    else:
                        retention = retention_bytes
                    if not is_int(retention) or retention <= 0:
                        raise err("PATCH_INVALID", "retention_bytes 必须为正整数")
                    effective_clock = update_clock(
                        connection, clock_ms, config.clock_tolerance_ms
                    )
                    channels = {
                        row["name"]: dict(row)
                        for row in connection.execute(
                            "SELECT name, low_watermark, high_watermark FROM channels "
                            "ORDER BY name"
                        )
                    }
                    readers: dict[str, list[dict[str, Any]]] = {
                        name: [] for name in channels
                    }
                    for row in connection.execute(
                        "SELECT name, channel, offset FROM subscriptions "
                        "WHERE active=1 ORDER BY channel, name"
                    ):
                        readers[row["channel"]].append(
                            {
                                "kind": "subscription",
                                "name": row["name"],
                                "offset": int(row["offset"]),
                            }
                        )
                    for row in connection.execute(
                        "SELECT bridge_id, source_channel, next_offset FROM bridge_positions "
                        "WHERE active=1 ORDER BY source_channel, bridge_id"
                    ):
                        readers[row["source_channel"]].append(
                            {
                                "kind": "bridge",
                                "name": row["bridge_id"],
                                "offset": int(row["next_offset"]),
                            }
                        )
                    safe_offsets: dict[str, int] = {}
                    blocked: list[dict[str, Any]] = []
                    for name in sorted(channels):
                        high = int(channels[name]["high_watermark"])
                        channel_readers = sorted(
                            readers[name],
                            key=lambda item: (item["offset"], item["kind"], item["name"]),
                        )
                        safe = (
                            channel_readers[0]["offset"] if channel_readers else high
                        )
                        safe_offsets[name] = safe
                        if safe < high:
                            slowest = channel_readers[0]
                            blocked.append(
                                {
                                    "channel": name,
                                    "kind": slowest["kind"],
                                    "name": slowest["name"],
                                    "offset": safe,
                                    "reason": "unconsumed_reader",
                                }
                            )
                    blocked.sort(
                        key=lambda item: (
                            item["offset"],
                            item["channel"],
                            item["kind"],
                            item["name"],
                        )
                    )
                    before = self._retained_bytes(connection)
                    if before <= retention:
                        report = {
                            "logical_clock_ms": effective_clock,
                            "retention_bytes": retention,
                            "bytes_before": before,
                            "bytes_after": before,
                            "physical_removed": 0,
                            "tombstones_marked": 0,
                            "items": [],
                            "blocked": blocked,
                            "blocked_slowest": blocked[0] if blocked else None,
                            "reason": "under_limit",
                        }
                    else:
                        items, removed, marked, cleared_up_to = self._cleanup_reclaim(
                            connection, channels, safe_offsets, retention
                        )
                        for channel_name, offset in cleared_up_to.items():
                            connection.execute(
                                "UPDATE channels SET low_watermark=? "
                                "WHERE name=? AND low_watermark<?",
                                (offset, channel_name, offset),
                            )
                        after = self._retained_bytes(connection)
                        report = {
                            "logical_clock_ms": effective_clock,
                            "retention_bytes": retention,
                            "bytes_before": before,
                            "bytes_after": after,
                            "physical_removed": removed,
                            "tombstones_marked": marked,
                            "items": items,
                            "blocked": blocked,
                            "blocked_slowest": blocked[0] if blocked else None,
                            "reason": (
                                "retention_bytes_exceeded"
                                if after > retention
                                else "retention_bytes_reached"
                            ),
                        }
                    now = now_ms()
                    cursor = connection.execute(
                        "INSERT INTO cleanup_runs(logical_clock_ms, retention_bytes, "
                        "bytes_before, bytes_after, physical_removed, tombstones_marked, "
                        "blocked_json, reason_json, created_at_ms) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            report["logical_clock_ms"],
                            report["retention_bytes"],
                            report["bytes_before"],
                            report["bytes_after"],
                            report["physical_removed"],
                            report["tombstones_marked"],
                            canonical_json(report["blocked"]),
                            canonical_json({"reason": report["reason"]}),
                            now,
                        ),
                    )
                    run_id = int(cursor.lastrowid)
                    for item in report["items"]:
                        connection.execute(
                            "INSERT INTO cleanup_items(run_id, channel, offset, action, "
                            "bytes_reclaimed, reason) VALUES(?, ?, ?, ?, ?, ?)",
                            (
                                run_id,
                                item["channel"],
                                item["offset"],
                                item["action"],
                                item["bytes_reclaimed"],
                                item["reason"],
                            ),
                        )
                    update_clock(
                        connection, effective_clock, config.clock_tolerance_ms
                    )
                    report["run_id"] = run_id
                    report["ok"] = True
                    self._complete_operation(connection, intent_id, report)
                    hit("cleanup_before_commit")
                hit("cleanup_after_commit")
                return report
            finally:
                connection.close()

    def status(self) -> dict[str, Any]:
        connection = connect(self.db_path)
        try:
            version, config = get_config(connection)
            channel_rows = connection.execute(
                "SELECT * FROM channels ORDER BY name"
            ).fetchall()
            channels = []
            for row in channel_rows:
                event_stats = connection.execute(
                    "SELECT COUNT(*) AS n, "
                    "COALESCE(SUM(LENGTH(CAST(fields_json AS BLOB))), 0) AS bytes, "
                    "COALESCE(SUM(LENGTH(CAST(fields_json AS BLOB)) + "
                    "LENGTH(CAST(raw_repr_json AS BLOB))), 0) AS retained_bytes, "
                    "COALESCE(SUM(tombstone), 0) AS tombstones "
                    "FROM events WHERE channel=?",
                    (row["name"],),
                ).fetchone()
                channels.append(
                    {
                        "name": row["name"],
                        "generation": int(row["generation"]),
                        "partitions": int(row["partitions"]),
                        "low_watermark": int(row["low_watermark"]),
                        "high_watermark": int(row["high_watermark"]),
                        "event_count": int(event_stats["n"]),
                        "payload_bytes": int(event_stats["bytes"]),
                        "retained_bytes": int(event_stats["retained_bytes"]),
                        "tombstones": int(event_stats["tombstones"]),
                        "sources": config.channels[row["name"]].sources,
                    }
                )
            bridges = []
            for row in connection.execute(
                "SELECT * FROM bridge_positions WHERE active=1 ORDER BY bridge_id"
            ):
                source_high = int(
                    connection.execute(
                        "SELECT high_watermark FROM channels WHERE name=?",
                        (row["source_channel"],),
                    ).fetchone()["high_watermark"]
                )
                cfg = config.bridges.get(row["bridge_id"])
                bridges.append(
                    {
                        "id": row["bridge_id"],
                        "source_channel": row["source_channel"],
                        "target_channel": row["target_channel"],
                        "next_offset": int(row["next_offset"]),
                        "source_high_watermark": source_high,
                        "lag": max(0, source_high - int(row["next_offset"])),
                        "last_digest": row["last_digest"],
                        "source_generation": int(row["source_generation"]),
                        "target_generation": int(row["target_generation"]),
                        "config_version": int(row["config_version"]),
                        "configured_source_generation": cfg.source_generation if cfg else None,
                        "configured_target_generation": cfg.target_generation if cfg else None,
                    }
                )
            subscriptions = []
            for row in connection.execute(
                "SELECT * FROM subscriptions WHERE active=1 ORDER BY name"
            ):
                high = int(
                    connection.execute(
                        "SELECT high_watermark FROM channels WHERE name=?",
                        (row["channel"],),
                    ).fetchone()["high_watermark"]
                )
                subscriptions.append(
                    {
                        "name": row["name"],
                        "channel": row["channel"],
                        "offset": int(row["offset"]),
                        "high_watermark": high,
                        "lag": max(0, high - int(row["offset"])),
                        "config_version": int(row["config_version"]),
                    }
                )
            degradations = [
                {
                    "channel": row["channel"],
                    "sequence": int(row["sequence"]),
                    "field": row["field"],
                    "source_version": row["source_version"],
                    "action": row["action"],
                    "default": json.loads(row["default_json"]),
                    "reason": row["reason"],
                }
                for row in connection.execute(
                    "SELECT * FROM degradations "
                    "ORDER BY channel, sequence, field, source_version, action"
                )
            ]
            decisions = [
                {
                    "channel": row["channel"],
                    "sequence": int(row["sequence"]),
                    "selected_source": row["selected_source"],
                    "selected_generation": int(row["selected_generation"]),
                    "logical_clock": int(row["logical_clock"]),
                    "decision_basis": row["decision_basis"],
                }
                for row in connection.execute(
                    "SELECT * FROM merge_decisions ORDER BY channel, sequence"
                )
            ]
            clock = connection.execute("SELECT * FROM clock_state WHERE id=1").fetchone()
            pending = [
                {
                    "run_id": int(row["id"]),
                    "bridge_id": row["bridge_id"],
                    "config_version": int(row["config_version"]),
                    "start_offset": int(row["start_offset"]),
                    "started_at_ms": int(row["started_at_ms"]),
                }
                for row in connection.execute(
                    "SELECT * FROM bridge_runs WHERE status='running' ORDER BY id"
                )
            ]
            pending_operations = [
                {
                    "intent_id": int(row["id"]),
                    "kind": row["kind"],
                    "created_at_ms": int(row["created_at_ms"]),
                }
                for row in connection.execute(
                    "SELECT id, kind, created_at_ms FROM operation_intents "
                    "WHERE status='pending' ORDER BY id"
                )
            ]
            last_cleanup_row = connection.execute(
                "SELECT * FROM cleanup_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            cleanup_history = [
                {
                    "run_id": int(row["id"]),
                    "logical_clock_ms": int(row["logical_clock_ms"]),
                    "retention_bytes": int(row["retention_bytes"]),
                    "bytes_before": int(row["bytes_before"]),
                    "bytes_after": int(row["bytes_after"]),
                    "physical_removed": int(row["physical_removed"]),
                    "tombstones_marked": int(row["tombstones_marked"]),
                    "blocked": json.loads(row["blocked_json"]),
                    "reason": json.loads(row["reason_json"])["reason"],
                }
                for row in connection.execute(
                    "SELECT * FROM cleanup_runs ORDER BY id"
                )
            ]
            last_cleanup = None
            if last_cleanup_row is not None:
                last_cleanup = cleanup_history[-1]
            return {
                "ok": True,
                "config_version": version,
                "source_fingerprint": config.source_fingerprint,
                "logical_clock_ms": int(clock["logical_clock_ms"]) if clock else 0,
                "event_clock_watermark": (
                    int(clock["event_clock_watermark"]) if clock else 0
                ),
                "channels": channels,
                "bridges": bridges,
                "subscriptions": subscriptions,
                "compatibility_degradations": degradations,
                "merge_decisions": decisions,
                "pending_bridge_runs": pending,
                "pending_operations": pending_operations,
                "last_cleanup": last_cleanup,
                "cleanup_history": cleanup_history,
            }
        finally:
            connection.close()

    def verify(self) -> dict[str, Any]:
        connection = connect(self.db_path)
        errors: list[dict[str, Any]] = []
        checks: dict[str, Any] = {}
        try:
            version, config = get_config(connection)
            checks["config_version"] = version
            channels = {
                row["name"]: dict(row)
                for row in connection.execute("SELECT * FROM channels ORDER BY name")
            }
            event_by_key: dict[tuple[str, int], Any] = {}
            event_by_offset: dict[tuple[str, int], Any] = {}
            for row in connection.execute("SELECT * FROM events ORDER BY channel, offset"):
                event_by_key[(row["channel"], int(row["sequence"]))] = row
                event_by_offset[(row["channel"], int(row["offset"]))] = row
            decision_by_key = {
                (row["channel"], int(row["sequence"])): row
                for row in connection.execute(
                    "SELECT * FROM merge_decisions ORDER BY channel, sequence"
                )
            }
            batch_by_key = {
                (row["channel"], row["batch_id"]): row
                for row in connection.execute("SELECT * FROM batches")
            }
            channel_checks = {}
            for name, channel in channels.items():
                rows = connection.execute(
                    "SELECT * FROM events WHERE channel=? ORDER BY offset", (name,)
                ).fetchall()
                high = int(channel["high_watermark"])
                low = int(channel["low_watermark"])
                seen_offsets: set[int] = set()
                seen_sequences: set[int] = set()
                for row in rows:
                    offset = int(row["offset"])
                    sequence = int(row["sequence"])
                    if offset in seen_offsets:
                        errors.append(
                            self._verify_error(
                                "OFFSET_CONFLICT", name, offset=offset, sequence=sequence
                            )
                        )
                    if sequence in seen_sequences:
                        errors.append(
                            self._verify_error(
                                "SEQUENCE_DUPLICATE",
                                name,
                                offset=offset,
                                sequence=sequence,
                            )
                        )
                    seen_offsets.add(offset)
                    seen_sequences.add(sequence)
                    if offset < 0 or offset >= high:
                        errors.append(
                            self._verify_error(
                                "OFFSET_CONFLICT",
                                name,
                                offset=offset,
                                high_watermark=high,
                            )
                        )
                    fields = json.loads(row["fields_json"])
                    if payload_digest(fields) != row["payload_sha256"]:
                        errors.append(
                            self._verify_error(
                                "BRIDGE_DIGEST_MISMATCH",
                                name,
                                offset=offset,
                                reason="stored payload digest mismatch",
                            )
                        )
                    route = self._partition(
                        name, int(row["partition_count"]), row["partition_key"]
                    )
                    if route != int(row["partition"]):
                        errors.append(
                            self._verify_error(
                                "VERIFY_FAILED",
                                name,
                                offset=offset,
                                reason="partition route mismatch",
                            )
                        )
                    decision = decision_by_key.get((name, sequence))
                    if decision is None:
                        errors.append(
                            self._verify_error(
                                "VERIFY_FAILED",
                                name,
                                offset=offset,
                                sequence=sequence,
                                reason="merge decision missing",
                            )
                        )
                    elif (
                        decision["selected_source"] != row["source_version"]
                        or int(decision["selected_generation"])
                        != int(row["source_generation"])
                        or int(decision["logical_clock"])
                        != int(row["logical_clock"])
                    ):
                        errors.append(
                            self._verify_error(
                                "VERIFY_FAILED",
                                name,
                                offset=offset,
                                sequence=sequence,
                                reason="merge decision and event differ",
                            )
                        )
                    batch = batch_by_key.get((name, row["batch_id"]))
                    if batch is None or not (
                        int(batch["start_offset"])
                        <= offset
                        < int(batch["end_offset"])
                    ):
                        errors.append(
                            self._verify_error(
                                "VERIFY_FAILED",
                                name,
                                offset=offset,
                                batch_id=row["batch_id"],
                                reason="event batch metadata mismatch",
                            )
                        )
                missing = sorted(set(range(low, high)) - seen_offsets)
                if missing:
                    errors.append(
                        self._verify_error(
                            "VERIFY_FAILED",
                            name,
                            reason="committed offset range has gaps",
                            first_missing=missing[0],
                        )
                    )
                if high == 0 and rows:
                    errors.append(
                        self._verify_error(
                            "VERIFY_FAILED", name, reason="events exist with zero watermark"
                        )
                    )
                channel_checks[name] = {
                    "events": len(rows),
                    "low_watermark": low,
                    "high_watermark": high,
                    "gaps": len(missing),
                }
            checks["channels"] = channel_checks

            bridge_checks = {}
            for bridge_id in sorted(config.bridges):
                bridge = config.bridges[bridge_id]
                position = connection.execute(
                    "SELECT * FROM bridge_positions WHERE bridge_id=?", (bridge_id,)
                ).fetchone()
                if position is None or not position["active"]:
                    errors.append(
                        self._verify_error(
                            "VERIFY_FAILED", bridge.source, bridge=bridge_id, reason="missing active position"
                        )
                    )
                    continue
                next_offset = int(position["next_offset"])
                high = int(channels[bridge.source]["high_watermark"])
                if next_offset > high:
                    errors.append(
                        self._verify_error(
                            "BRIDGE_UPSTREAM_WATERMARK",
                            bridge.source,
                            bridge=bridge_id,
                            next_offset=next_offset,
                            high_watermark=high,
                        )
                    )
                check_rows = connection.execute(
                    "SELECT * FROM bridge_checks WHERE bridge_id=? ORDER BY source_offset",
                    (bridge_id,),
                ).fetchall()
                check_offsets = {int(row["source_offset"]) for row in check_rows}
                expected = set(range(0, next_offset))
                if check_offsets != expected:
                    errors.append(
                        self._verify_error(
                            "VERIFY_FAILED",
                            bridge.source,
                            bridge=bridge_id,
                            reason="bridge scan watermark and checks differ",
                            expected=len(expected),
                            actual=len(check_offsets),
                        )
                    )
                for check in check_rows:
                    source_offset = int(check["source_offset"])
                    source_event = event_by_offset.get((bridge.source, source_offset))
                    check_fields = json.loads(check["source_fields_json"])
                    if payload_digest(check_fields) != check["source_payload_sha256"]:
                        errors.append(
                            self._verify_error(
                                "BRIDGE_DIGEST_MISMATCH",
                                bridge.source,
                                bridge=bridge_id,
                                source_offset=source_offset,
                                reason="snapshot digest mismatch",
                            )
                        )
                    if (
                        source_event is not None
                        and check["source_payload_sha256"]
                        != source_event["payload_sha256"]
                        and not (
                            bool(source_event["compacted"])
                            and source_offset
                            < int(channels[bridge.source]["low_watermark"])
                        )
                    ):
                        errors.append(
                            self._verify_error(
                                "BRIDGE_DIGEST_MISMATCH",
                                bridge.source,
                                bridge=bridge_id,
                                source_offset=source_offset,
                                reason="source digest changed",
                            )
                        )
                    delivery = connection.execute(
                        "SELECT * FROM bridge_deliveries WHERE bridge_id=? AND source_offset=?",
                        (bridge_id, source_offset),
                    ).fetchone()
                    if bool(check["matched"]) != (delivery is not None):
                        errors.append(
                            self._verify_error(
                                "VERIFY_FAILED",
                                bridge.source,
                                bridge=bridge_id,
                                source_offset=source_offset,
                                reason="matched flag and delivery differ",
                            )
                        )
                    config_row = connection.execute(
                        "SELECT config_json FROM config_versions WHERE version=?",
                        (int(check["config_version"]),),
                    ).fetchone()
                    if config_row is None:
                        errors.append(
                            self._verify_error(
                                "CONFIG_VERSION_INVALID",
                                bridge.source,
                                bridge=bridge_id,
                                source_offset=source_offset,
                            )
                        )
                        continue
                    check_config = Config.from_dict(json.loads(config_row["config_json"]))
                    old_bridge = check_config.bridges.get(bridge_id)
                    if old_bridge is None:
                        errors.append(
                            self._verify_error(
                                "CONFIG_VERSION_INVALID",
                                bridge.source,
                                bridge=bridge_id,
                                source_offset=source_offset,
                                reason="rule missing in bound config",
                            )
                        )
                        continue
                    fields = check_fields
                    matched = evaluate_filter(old_bridge.filter, fields)
                    if matched != bool(check["matched"]):
                        errors.append(
                            self._verify_error(
                                "VERIFY_FAILED",
                                bridge.source,
                                bridge=bridge_id,
                                source_offset=source_offset,
                                reason="filter replay mismatch",
                            )
                        )
                    if matched:
                        transformed, _trace = apply_transforms(fields, old_bridge.transforms)
                        for field in check_config.channels[old_bridge.target].field_schema:
                            if field.name not in transformed:
                                transformed[field.name] = field.default
                            elif (
                                transformed[field.name] is not None
                                and field.type != "any"
                            ):
                                transformed[field.name] = typed_value(
                                    transformed[field.name], field.type
                                )
                        recomputed = payload_digest(transformed)
                        if recomputed != check["transform_digest"]:
                            errors.append(
                                self._verify_error(
                                    "BRIDGE_DIGEST_MISMATCH",
                                    bridge.source,
                                    bridge=bridge_id,
                                    source_offset=source_offset,
                                    reason="transform replay digest mismatch",
                                )
                            )
                        if delivery is not None:
                            if delivery["transform_digest"] != recomputed:
                                errors.append(
                                    self._verify_error(
                                        "BRIDGE_DIGEST_MISMATCH",
                                        bridge.source,
                                        bridge=bridge_id,
                                        source_offset=source_offset,
                                        reason="delivery digest mismatch",
                                    )
                                )
                            target_row = event_by_offset.get(
                                (delivery["target_channel"], int(delivery["target_offset"]))
                            )
                            target_low = int(
                                channels[delivery["target_channel"]]["low_watermark"]
                            )
                            if target_row is None:
                                if int(delivery["target_offset"]) >= target_low:
                                    errors.append(
                                        self._verify_error(
                                            "VERIFY_FAILED",
                                            delivery["target_channel"],
                                            bridge=bridge_id,
                                            target_offset=int(delivery["target_offset"]),
                                            reason="delivery target event missing",
                                        )
                                    )
                            elif (
                                target_row["payload_sha256"] != recomputed
                                and not (
                                    bool(target_row["compacted"])
                                    and int(delivery["target_offset"]) < target_low
                                )
                            ):
                                errors.append(
                                    self._verify_error(
                                        "BRIDGE_DIGEST_MISMATCH",
                                        delivery["target_channel"],
                                        bridge=bridge_id,
                                        target_offset=int(delivery["target_offset"]),
                                    )
                                )
                bridge_checks[bridge_id] = {
                    "next_offset": next_offset,
                    "checks": len(check_rows),
                    "source_generation": int(channels[bridge.source]["generation"]),
                    "rule_source_generation": bridge.source_generation,
                    "target_generation": int(channels[bridge.target]["generation"]),
                    "rule_target_generation": bridge.target_generation,
                }
            checks["bridges"] = bridge_checks

            subscription_checks = {}
            for name in sorted(config.subscriptions):
                subscription = config.subscriptions[name]
                row = connection.execute(
                    "SELECT * FROM subscriptions WHERE name=?", (name,)
                ).fetchone()
                if row is None or not row["active"]:
                    errors.append(
                        self._verify_error(
                            "VERIFY_FAILED",
                            subscription.channel,
                            subscription=name,
                            reason="missing active offset",
                        )
                    )
                    continue
                high = int(channels[subscription.channel]["high_watermark"])
                offset = int(row["offset"])
                if offset < 0 or offset > high:
                    errors.append(
                        self._verify_error(
                            "SUBSCRIPTION_OFFSET_OUT_OF_RANGE",
                            subscription.channel,
                            subscription=name,
                            offset=offset,
                            high_watermark=high,
                        )
                    )
                commits = connection.execute(
                    "SELECT * FROM subscription_commits WHERE subscription_name=? "
                    "ORDER BY id",
                    (name,),
                ).fetchall()
                last = 0
                for commit in commits:
                    if int(commit["from_offset"]) > int(commit["to_offset"]):
                        errors.append(
                            self._verify_error(
                                "SUBSCRIPTION_REWIND",
                                subscription.channel,
                                subscription=name,
                                commit_id=int(commit["id"]),
                            )
                        )
                    if int(commit["to_offset"]) > high:
                        errors.append(
                            self._verify_error(
                                "SUBSCRIPTION_OFFSET_OUT_OF_RANGE",
                                subscription.channel,
                                subscription=name,
                                commit_id=int(commit["id"]),
                            )
                        )
                    last = max(last, int(commit["to_offset"]))
                if last > offset:
                    errors.append(
                        self._verify_error(
                            "SUBSCRIPTION_REWIND",
                            subscription.channel,
                            subscription=name,
                            offset=offset,
                            last_committed=last,
                        )
                    )
                subscription_checks[name] = {
                    "offset": offset,
                    "high_watermark": high,
                    "commits": len(commits),
                }
            checks["subscriptions"] = subscription_checks

            clock = connection.execute("SELECT * FROM clock_state WHERE id=1").fetchone()
            maximum_clock = connection.execute(
                "SELECT COALESCE(MAX(logical_clock), 0) AS v FROM events"
            ).fetchone()["v"]
            if clock is None:
                errors.append(
                    self._verify_error("VERIFY_FAILED", None, reason="clock state missing")
                )
            elif int(clock["event_clock_watermark"]) < int(maximum_clock):
                errors.append(
                    self._verify_error(
                        "VERIFY_FAILED",
                        None,
                        reason="event logical clock watermark behind events",
                        watermark=int(clock["event_clock_watermark"]),
                        maximum=int(maximum_clock),
                    )
                )
            checks["event_clock"] = {
                "watermark": int(clock["event_clock_watermark"]) if clock else None,
                "maximum_event_clock": int(maximum_clock),
            }
            checks["merge_decisions"] = int(
                connection.execute("SELECT COUNT(*) AS n FROM merge_decisions").fetchone()["n"]
            )
            checks["pending_bridge_runs"] = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM bridge_runs WHERE status='running'"
                ).fetchone()["n"]
            )
            if checks["pending_bridge_runs"]:
                errors.append(
                    self._verify_error(
                        "VERIFY_FAILED",
                        None,
                        reason="unfinished bridge intent exists; run resume",
                        count=checks["pending_bridge_runs"],
                    )
                )
            checks["pending_operations"] = int(
                connection.execute(
                    "SELECT COUNT(*) AS n FROM operation_intents WHERE status='pending'"
                ).fetchone()["n"]
            )
            if checks["pending_operations"]:
                errors.append(
                    self._verify_error(
                        "VERIFY_FAILED",
                        None,
                        reason="unfinished operation intent exists; run resume",
                        count=checks["pending_operations"],
                    )
                )
            return {
                "ok": not errors,
                "config_version": version,
                "errors": errors,
                "checks": checks,
            }
        finally:
            connection.close()

    def _append_specs(
        self,
        connection: Any,
        config: Config,
        channel: Any,
        specs: list[Any],
        *,
        config_version: int,
        batch_size: int,
        created_at_ms: int,
        default_produced_at_ms: int,
        source_version_override: str | None = None,
        source_generation_override: int | None = None,
        raw_override: dict[str, Any] | None = None,
    ) -> tuple[list[dict[str, Any]], int, int]:
        existing_seq = {
            int(row["sequence"]): row
            for row in connection.execute(
                "SELECT * FROM events WHERE channel=?", (channel.name,)
            )
        }
        next_sequence = max(existing_seq, default=0) + 1
        high = int(
            connection.execute(
                "SELECT high_watermark FROM channels WHERE name=?", (channel.name,)
            ).fetchone()["high_watermark"]
        )
        seen_batch_sequences: set[int] = set()
        results: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        last_clock = 0
        for index, raw_spec in enumerate(specs):
            if not isinstance(raw_spec, dict):
                raise err("MALFORMED_EVENT", "事件必须为对象", index=index)
            sequence = raw_spec.get("sequence")
            if sequence is None:
                sequence = next_sequence
                next_sequence += 1
            if not is_int(sequence) or sequence <= 0:
                raise err("MALFORMED_EVENT", "sequence 必须为正整数", index=index)
            if sequence in seen_batch_sequences:
                raise err(
                    "SEQUENCE_DUPLICATE",
                    "同一批次内事件序号重复",
                    channel=channel.name,
                    sequence=sequence,
                )
            seen_batch_sequences.add(sequence)
            fields, time_conversion = self._normalize_event_fields(
                config, channel, raw_spec
            )
            logical_clock = raw_spec.get("logical_clock", 0)
            if not is_int(logical_clock) or logical_clock < 0:
                raise err(
                    "V2_CLOCK_INVALID",
                    "logical_clock 必须为非负整数",
                    channel=channel.name,
                    sequence=sequence,
                )
            last_clock = max(last_clock, logical_clock)
            produced_at_ms = raw_spec.get("produced_at_ms", default_produced_at_ms)
            if (
                "produced_at_ms" not in raw_spec
                and time_conversion is not None
            ):
                produced_at_ms = time_conversion["absolute_ms"]
            if not is_int(produced_at_ms) or produced_at_ms < 0:
                raise err(
                    "MALFORMED_EVENT",
                    "produced_at_ms 必须为非负整数",
                    channel=channel.name,
                    sequence=sequence,
                )
            tombstone = raw_spec.get("tombstone", False)
            if not isinstance(tombstone, bool):
                raise err("MALFORMED_EVENT", "tombstone 必须为布尔值")
            partition_key = raw_spec.get("partition_key", str(sequence))
            if not isinstance(partition_key, str) or not partition_key:
                raise err("MALFORMED_EVENT", "partition_key 必须为非空字符串")
            if sequence in existing_seq:
                row = existing_seq[sequence]
                same = (
                    row["fields_json"] == canonical_json(fields)
                    and int(row["logical_clock"]) == logical_clock
                    and int(row["produced_at_ms"]) == produced_at_ms
                    and bool(row["tombstone"]) == tombstone
                    and row["partition_key"] == partition_key
                )
                if not same:
                    raise err(
                        "DATA_CONFLICT",
                        "同一通道事件序号已存在且载荷不同",
                        channel=channel.name,
                        sequence=sequence,
                        existing_offset=int(row["offset"]),
                    )
                results.append(
                    {
                        "sequence": sequence,
                        "offset": int(row["offset"]),
                        "appended": False,
                        "payload_sha256": row["payload_sha256"],
                        "logical_clock": int(row["logical_clock"]),
                    }
                )
                continue
            pending.append(
                {
                    "sequence": sequence,
                    "fields": fields,
                    "time_conversion": time_conversion,
                    "logical_clock": logical_clock,
                    "produced_at_ms": produced_at_ms,
                    "tombstone": tombstone,
                    "partition_key": partition_key,
                    "raw": raw_spec,
                }
            )
        appended = 0
        for batch_start in range(0, len(pending), batch_size):
            batch = pending[batch_start : batch_start + batch_size]
            payload_root = digest_value(
                [payload_digest(item["fields"]) for item in batch]
            )
            batch_id = (
                f"{channel.name}:{high + appended}:{high + appended + len(batch)}:"
                f"{payload_root.split(':', 1)[1][:16]}"
            )
            for item in batch:
                offset = high + appended
                source_version = source_version_override or "publish"
                source_generation = (
                    source_generation_override
                    if source_generation_override is not None
                    else channel.generation
                )
                raw_repr = raw_override if raw_override is not None else item["raw"]
                self._insert_event(
                    connection,
                    channel=channel,
                    offset=offset,
                    sequence=item["sequence"],
                    partition_key=item["partition_key"],
                    logical_clock=item["logical_clock"],
                    produced_at_ms=item["produced_at_ms"],
                    fields=item["fields"],
                    tombstone=item["tombstone"],
                    source_version=source_version,
                    source_generation=source_generation,
                    raw_repr=raw_repr,
                    time_conversion=item["time_conversion"],
                    config_version=config_version,
                    batch_id=batch_id,
                    created_at_ms=created_at_ms,
                )
                connection.execute(
                    "INSERT INTO merge_decisions(channel, sequence, selected_source, "
                    "selected_generation, logical_clock, decision_basis, options_json, "
                    "raw_v1_json, raw_v2_json, config_version) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?) "
                    "ON CONFLICT(channel, sequence) DO UPDATE SET "
                    "selected_source=excluded.selected_source, "
                    "selected_generation=excluded.selected_generation, "
                    "logical_clock=excluded.logical_clock, "
                    "decision_basis=excluded.decision_basis, "
                    "options_json=excluded.options_json, "
                    "config_version=excluded.config_version",
                    (
                        channel.name,
                        item["sequence"],
                        source_version,
                        source_generation,
                        item["logical_clock"],
                        f"runtime append via {source_version}",
                        canonical_json(
                            [
                                {
                                    "source_version": source_version,
                                    "generation": source_generation,
                                    "logical_clock": item["logical_clock"],
                                    "selected": True,
                                }
                            ]
                        ),
                        config_version,
                    ),
                )
                appended += 1
                results.append(
                    {
                        "sequence": item["sequence"],
                        "offset": offset,
                        "appended": True,
                        "payload_sha256": payload_digest(item["fields"]),
                        "logical_clock": item["logical_clock"],
                    }
                )
            connection.execute(
                "INSERT INTO batches(channel, batch_id, start_offset, end_offset, "
                "event_count, config_version, payload_root, created_at_ms) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    channel.name,
                    batch_id,
                    high + appended - len(batch),
                    high + appended,
                    len(batch),
                    config_version,
                    payload_root,
                    created_at_ms,
                ),
            )
        if appended:
            connection.execute(
                "UPDATE channels SET high_watermark=? WHERE name=?",
                (high + appended, channel.name),
            )
        results.sort(key=lambda item: (item["offset"], item["sequence"]))
        return results, appended, last_clock

    def _start_operation(self, kind: str, payload: dict[str, Any]) -> int:
        payload_json = canonical_json(payload)
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    cursor = connection.execute(
                        "INSERT INTO operation_intents(kind, payload_json, status, "
                        "created_at_ms) VALUES(?, ?, 'pending', ?)",
                        (kind, payload_json, now_ms()),
                    )
                    return int(cursor.lastrowid)
            finally:
                connection.close()

    @staticmethod
    def _completed_operation(
        connection: Any, intent_id: int
    ) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT status, result_json FROM operation_intents WHERE id=?",
            (intent_id,),
        ).fetchone()
        if row is None:
            raise err("NOT_FOUND", "操作意图不存在", intent_id=intent_id)
        if row["status"] == "completed":
            return json.loads(row["result_json"])
        if row["status"] == "failed":
            raise err(
                "CONCURRENT_MODIFICATION",
                "操作意图已失败，不能重做",
                intent_id=intent_id,
            )
        return None

    @staticmethod
    def _complete_operation(
        connection: Any, intent_id: int, result: dict[str, Any]
    ) -> None:
        connection.execute(
            "UPDATE operation_intents SET status='completed', completed_at_ms=?, "
            "result_json=? WHERE id=? AND status='pending'",
            (now_ms(), canonical_json(result), int(intent_id)),
        )
        if connection.execute("SELECT changes()").fetchone()[0] != 1:
            raise err(
                "CONCURRENT_MODIFICATION",
                "操作意图完成标记失败",
                intent_id=intent_id,
            )

    def _fail_operation(self, intent_id: int, exc: BusinessError) -> None:
        try:
            with file_lock(self.db_path):
                connection = connect(self.db_path)
                try:
                    connection.execute(
                        "UPDATE operation_intents SET status='failed', error_code=?, "
                        "completed_at_ms=? WHERE id=? AND status='pending'",
                        (exc.code, now_ms(), int(intent_id)),
                    )
                finally:
                    connection.close()
        except Exception:
            pass

    def _insert_event(
        self,
        connection: Any,
        *,
        channel: Any,
        offset: int,
        sequence: int,
        partition_key: str,
        logical_clock: int,
        produced_at_ms: int,
        fields: dict[str, Any],
        tombstone: bool,
        source_version: str,
        source_generation: int,
        raw_repr: dict[str, Any],
        time_conversion: dict[str, Any] | None,
        config_version: int,
        batch_id: str,
        created_at_ms: int,
    ) -> None:
        partition = self._partition(channel.name, channel.partitions, partition_key)
        connection.execute(
            "INSERT INTO events(channel, offset, sequence, partition, partition_count, "
            "partition_key, "
            "logical_clock, produced_at_ms, fields_json, payload_sha256, source_version, "
            "source_generation, raw_repr_json, time_conversion_json, tombstone, compacted, "
            "config_version, batch_id, committed, created_at_ms) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1, ?)",
            (
                channel.name,
                offset,
                sequence,
                partition,
                channel.partitions,
                partition_key,
                logical_clock,
                produced_at_ms,
                canonical_json(fields),
                payload_digest(fields),
                source_version,
                source_generation,
                canonical_json(raw_repr),
                canonical_json(time_conversion)
                if time_conversion is not None
                else None,
                1 if tombstone else 0,
                config_version,
                batch_id,
                created_at_ms,
            ),
        )

    def _normalize_event_fields(
        self, config: Config, channel: Any, raw_spec: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        raw_fields = raw_spec.get("fields")
        time_conversion = None
        if isinstance(raw_fields, list):
            if "relative_seconds" in raw_spec:
                relative = raw_spec["relative_seconds"]
                if not is_int(relative) or relative < 0:
                    raise err("V1_TIME_INVALID", "relative_seconds 必须为非负整数")
                absolute = config.base_epoch_ms + relative * 1000
                time_conversion = {
                    "source_relative_seconds": relative,
                    "scale": "seconds*1000",
                    "rounding_direction": "none",
                    "rounding_remainder_ms": 0,
                    "epoch_ms": config.base_epoch_ms,
                    "absolute_ms": absolute,
                }
                raw_spec = dict(raw_spec)
                raw_spec.setdefault("produced_at_ms", absolute)
            if len(raw_fields) != len(channel.field_schema):
                raise err(
                    "FIELD_COUNT_MISMATCH",
                    "扁平字段数组长度与通道字段声明不一致",
                    channel=channel.name,
                    expected=len(channel.field_schema),
                    actual=len(raw_fields),
                )
            fields = {
                field.name: value
                for field, value in zip(channel.field_schema, raw_fields, strict=True)
            }
        elif isinstance(raw_fields, dict):
            fields = dict(raw_fields)
        else:
            raise err(
                "MALFORMED_EVENT",
                "事件 fields 必须为具名对象或扁平数组",
                channel=channel.name,
            )
        for key, value in list(fields.items()):
            if not isinstance(key, str) or not key:
                raise err("MALFORMED_EVENT", "字段名必须为非空字符串")
            ensure_json_value(value, path=f"fields.{key}")
        for field in channel.field_schema:
            if field.name not in fields:
                fields[field.name] = field.default
            value = fields[field.name]
            if value is not None and field.type != "any":
                fields[field.name] = typed_value(value, field.type)
        ensure_json_value(fields, path=f"{channel.name}.fields")
        return fields, time_conversion

    def _apply_event_patches(
        self,
        connection: Any,
        config: Config,
        patches: list[Any],
        *,
        config_version: int,
        created_at_ms: int,
        default_produced_at_ms: int,
        summary: dict[str, Any],
    ) -> int:
        last_clock = 0
        for index, patch in enumerate(patches):
            if not isinstance(patch, dict):
                raise err("PATCH_INVALID", "事件补丁必须为对象", index=index)
            op = patch.get("op")
            channel_name = patch.get("channel")
            if channel_name not in config.channels:
                raise err(
                    "NOT_FOUND", "事件补丁通道不存在", channel=channel_name, index=index
                )
            channel = config.channels[channel_name]
            if op == "add":
                spec = {
                    key: value
                    for key, value in patch.items()
                    if key not in {"op", "channel"}
                }
                results, appended, clock = self._append_specs(
                    connection,
                    config,
                    channel,
                    [spec],
                    config_version=config_version,
                    batch_size=1,
                    created_at_ms=created_at_ms,
                    default_produced_at_ms=default_produced_at_ms,
                    source_version_override="edit",
                    source_generation_override=channel.generation,
                    raw_override=patch,
                )
                summary["events"]["add"] += appended
                last_clock = max(last_clock, clock)
            elif op in {"update", "delete"}:
                sequence = patch.get("sequence")
                if not is_int(sequence) or sequence <= 0:
                    raise err("PATCH_INVALID", "sequence 必须为正整数", index=index)
                row = connection.execute(
                    "SELECT * FROM events WHERE channel=? AND sequence=?",
                    (channel_name, sequence),
                ).fetchone()
                if row is None:
                    raise err(
                        "NOT_FOUND",
                        "待编辑事件不存在",
                        channel=channel_name,
                        sequence=sequence,
                    )
                offset = int(row["offset"])
                consumer_offsets = [
                    int(item[0])
                    for item in connection.execute(
                        "SELECT offset FROM subscriptions WHERE channel=? AND active=1 "
                        "UNION ALL SELECT next_offset FROM bridge_positions "
                        "WHERE source_channel=? AND active=1",
                        (channel_name, channel_name),
                    )
                ]
                if any(reader > offset for reader in consumer_offsets):
                    raise err(
                        "EVENT_LOCKED",
                        "事件已被订阅或桥接位点越过，不能编辑",
                        channel=channel_name,
                        sequence=sequence,
                        offset=offset,
                    )
                if op == "delete":
                    fields = {}
                    tombstone = True
                else:
                    spec = {
                        key: value
                        for key, value in patch.items()
                        if key not in {"op", "channel", "sequence"}
                    }
                    fields, _trace = self._normalize_event_fields(
                        config, channel, spec
                    )
                    tombstone = patch.get("tombstone", bool(row["tombstone"]))
                    if not isinstance(tombstone, bool):
                        raise err("PATCH_INVALID", "tombstone 必须为布尔值")
                logical_clock = patch.get("logical_clock")
                if logical_clock is None:
                    logical_clock = int(row["logical_clock"]) + 1
                if not is_int(logical_clock) or logical_clock < int(row["logical_clock"]):
                    raise err(
                        "V2_CLOCK_INVALID",
                        "编辑后的 logical_clock 必须为非负且不回退的整数",
                        sequence=sequence,
                    )
                produced_at_ms = patch.get("produced_at_ms", int(row["produced_at_ms"]))
                if not is_int(produced_at_ms) or produced_at_ms < 0:
                    raise err("MALFORMED_EVENT", "produced_at_ms 非法")
                payload = payload_digest(fields)
                connection.execute(
                    "UPDATE events SET fields_json=?, payload_sha256=?, logical_clock=?, "
                    "produced_at_ms=?, tombstone=?, compacted=0, source_version='edit', "
                    "source_generation=?, raw_repr_json=?, time_conversion_json=NULL, "
                    "config_version=? WHERE channel=? AND sequence=?",
                    (
                        canonical_json(fields),
                        payload,
                        logical_clock,
                        produced_at_ms,
                        1 if tombstone else 0,
                        channel.generation,
                        canonical_json(patch),
                        config_version,
                        channel_name,
                        sequence,
                    ),
                )
                connection.execute(
                    "DELETE FROM degradations WHERE channel=? AND sequence=?",
                    (channel_name, sequence),
                )
                connection.execute(
                    "INSERT INTO merge_decisions(channel, sequence, selected_source, "
                    "selected_generation, logical_clock, decision_basis, options_json, "
                    "raw_v1_json, raw_v2_json, config_version) "
                    "VALUES(?, ?, 'edit', ?, ?, ?, ?, NULL, NULL, ?) "
                    "ON CONFLICT(channel, sequence) DO UPDATE SET "
                    "selected_source='edit', selected_generation=excluded.selected_generation, "
                    "logical_clock=excluded.logical_clock, "
                    "decision_basis=excluded.decision_basis, "
                    "options_json=excluded.options_json, "
                    "config_version=excluded.config_version",
                    (
                        channel_name,
                        sequence,
                        channel.generation,
                        logical_clock,
                        "domain edit patch",
                        canonical_json(
                            [
                                {
                                    "source_version": "edit",
                                    "generation": channel.generation,
                                    "logical_clock": logical_clock,
                                    "selected": True,
                                }
                            ]
                        ),
                        config_version,
                    ),
                )
                summary["events"][op] += 1
                last_clock = max(last_clock, logical_clock)
            else:
                raise err("PATCH_INVALID", "事件补丁 op 必须为 add/update/delete", index=index)
        return last_clock

    def _apply_bridge_patches(
        self,
        connection: Any,
        config: Config,
        patches: list[Any],
        config_version: int,
        summary: dict[str, Any],
    ) -> None:
        for index, patch in enumerate(patches):
            if not isinstance(patch, dict):
                raise err("PATCH_INVALID", "桥接补丁必须为对象", index=index)
            op = patch.get("op")
            if op == "delete":
                bridge_id = (
                    patch.get("id")
                    or (patch.get("bridge") or {}).get("id")
                )
                if bridge_id not in config.bridges:
                    raise err("NOT_FOUND", "待删除桥接规则不存在", bridge=bridge_id)
                del config.bridges[bridge_id]
                connection.execute(
                    "UPDATE bridge_positions SET active=0, config_version=?, updated_at_ms=? "
                    "WHERE bridge_id=?",
                    (config_version, now_ms(), bridge_id),
                )
                summary["bridges"]["delete"] += 1
                continue
            bridge_doc = patch.get("bridge", patch)
            if not isinstance(bridge_doc, dict):
                raise err("PATCH_INVALID", "bridge 必须为对象", index=index)
            bridge_id = bridge_doc.get("id")
            if not isinstance(bridge_id, str) or not bridge_id:
                raise err("PATCH_INVALID", "bridge.id 必须为非空字符串", index=index)
            source = bridge_doc.get("source_channel", bridge_doc.get("source"))
            target = bridge_doc.get("target_channel", bridge_doc.get("target"))
            if source not in config.channels or target not in config.channels:
                raise err(
                    "BRIDGE_CHANNEL_MISSING",
                    "桥接规则引用了不存在的通道",
                    bridge=bridge_id,
                    source=source,
                    target=target,
                )
            source_generation = bridge_doc.get(
                "source_generation", config.channels[source].generation
            )
            target_generation = bridge_doc.get(
                "target_generation", config.channels[target].generation
            )
            minimum = bridge_doc.get("min_upstream_watermark", 0)
            max_batch = bridge_doc.get("max_batch", 100)
            filter_document = bridge_doc.get("filter", {})
            transforms = bridge_doc.get(
                "transforms", bridge_doc.get("transform", [])
            )
            if not is_int(source_generation) or source_generation <= 0:
                raise err("PATCH_INVALID", "source_generation 必须为正整数")
            if not is_int(target_generation) or target_generation <= 0:
                raise err("PATCH_INVALID", "target_generation 必须为正整数")
            if not is_int(minimum) or minimum < 0:
                raise err("PATCH_INVALID", "min_upstream_watermark 必须为非负整数")
            if not is_int(max_batch) or max_batch <= 0:
                raise err("PATCH_INVALID", "max_batch 必须为正整数")
            if not isinstance(filter_document, dict) or not isinstance(transforms, list):
                raise err("PATCH_INVALID", "filter/transforms 类型不合法")
            validate_filter(filter_document)
            validate_transforms(transforms)
            rule = BridgeDef(
                id=bridge_id,
                source=source,
                target=target,
                source_generation=source_generation,
                target_generation=target_generation,
                min_upstream_watermark=minimum,
                filter=filter_document,
                transforms=transforms,
                max_batch=max_batch,
            )
            position = connection.execute(
                "SELECT * FROM bridge_positions WHERE bridge_id=?", (bridge_id,)
            ).fetchone()
            if op == "add":
                if bridge_id in config.bridges:
                    raise err("PATCH_INVALID", "桥接规则已存在", bridge=bridge_id)
                config.bridges[bridge_id] = rule
                if position is None:
                    connection.execute(
                        "INSERT INTO bridge_positions(bridge_id, source_channel, "
                        "target_channel, next_offset, last_digest, source_generation, "
                        "target_generation, config_version, active, updated_at_ms) "
                        "VALUES(?, ?, ?, 0, NULL, ?, ?, ?, 1, ?)",
                        (
                            bridge_id,
                            source,
                            target,
                            source_generation,
                            target_generation,
                            config_version,
                            now_ms(),
                        ),
                    )
                else:
                    connection.execute(
                        "UPDATE bridge_positions SET source_channel=?, target_channel=?, "
                        "next_offset=0, last_digest=NULL, source_generation=?, "
                        "target_generation=?, config_version=?, active=1, updated_at_ms=? "
                        "WHERE bridge_id=?",
                        (
                            source,
                            target,
                            source_generation,
                            target_generation,
                            config_version,
                            now_ms(),
                            bridge_id,
                        ),
                    )
                summary["bridges"]["add"] += 1
            elif op == "update":
                if bridge_id not in config.bridges:
                    raise err("NOT_FOUND", "待更新桥接规则不存在", bridge=bridge_id)
                if position is not None and int(position["next_offset"]) > 0:
                    if (
                        position["source_channel"] != source
                        or position["target_channel"] != target
                    ):
                        raise err(
                            "EVENT_LOCKED",
                            "桥接位点已推进，不能改变上下游通道",
                            bridge=bridge_id,
                            next_offset=int(position["next_offset"]),
                        )
                config.bridges[bridge_id] = rule
                connection.execute(
                    "UPDATE bridge_positions SET source_channel=?, target_channel=?, "
                    "source_generation=?, target_generation=?, config_version=?, active=1, "
                    "updated_at_ms=? WHERE bridge_id=?",
                    (
                        source,
                        target,
                        source_generation,
                        target_generation,
                        config_version,
                        now_ms(),
                        bridge_id,
                    ),
                )
                summary["bridges"]["update"] += 1
            else:
                raise err("PATCH_INVALID", "桥接补丁 op 必须为 add/update/delete", index=index)

    def _apply_subscription_patches(
        self,
        connection: Any,
        config: Config,
        patches: list[Any],
        config_version: int,
        summary: dict[str, Any],
    ) -> None:
        for index, patch in enumerate(patches):
            if not isinstance(patch, dict):
                raise err("PATCH_INVALID", "订阅补丁必须为对象", index=index)
            op = patch.get("op")
            subscription_doc = patch.get("subscription", patch)
            if not isinstance(subscription_doc, dict):
                raise err("PATCH_INVALID", "subscription 必须为对象", index=index)
            name = subscription_doc.get("name")
            if not isinstance(name, str) or not name:
                raise err("PATCH_INVALID", "subscription.name 必须为非空字符串", index=index)
            if op == "delete":
                if name not in config.subscriptions:
                    raise err("NOT_FOUND", "待删除订阅不存在", subscription=name)
                del config.subscriptions[name]
                connection.execute(
                    "UPDATE subscriptions SET active=0, config_version=?, updated_at_ms=? "
                    "WHERE name=?",
                    (config_version, now_ms(), name),
                )
                summary["subscriptions"]["delete"] += 1
                continue
            channel = subscription_doc.get("channel")
            if channel not in config.channels:
                raise err(
                    "SUBSCRIPTION_RANGE",
                    "订阅引用了不存在的通道",
                    subscription=name,
                    channel=channel,
                )
            current_row = connection.execute(
                "SELECT * FROM subscriptions WHERE name=?", (name,)
            ).fetchone()
            high = int(
                connection.execute(
                    "SELECT high_watermark FROM channels WHERE name=?", (channel,)
                ).fetchone()["high_watermark"]
            )
            if op == "add":
                if name in config.subscriptions:
                    raise err("PATCH_INVALID", "订阅已存在", subscription=name)
                start = subscription_doc.get("start_offset")
                if not is_int(start) or start < 0 or start > high:
                    raise err(
                        "SUBSCRIPTION_RANGE",
                        "订阅起始偏移超出合法区间",
                        subscription=name,
                        start_offset=start,
                        max_offset=high,
                    )
                config.subscriptions[name] = SubscriptionDef(
                    name=name, channel=channel, start_offset=start
                )
                if current_row is None:
                    connection.execute(
                        "INSERT INTO subscriptions(name, channel, offset, config_version, "
                        "active, updated_at_ms) VALUES(?, ?, ?, ?, 1, ?)",
                        (name, channel, start, config_version, now_ms()),
                    )
                else:
                    connection.execute(
                        "UPDATE subscriptions SET channel=?, offset=?, config_version=?, "
                        "active=1, updated_at_ms=? WHERE name=?",
                        (channel, start, config_version, now_ms(), name),
                    )
                summary["subscriptions"]["add"] += 1
            elif op == "update":
                if name not in config.subscriptions:
                    raise err("NOT_FOUND", "待更新订阅不存在", subscription=name)
                if current_row is None:
                    raise err("NOT_FOUND", "订阅运行状态不存在", subscription=name)
                current_offset = int(current_row["offset"])
                new_offset = subscription_doc.get("offset", current_offset)
                if not is_int(new_offset) or new_offset < 0 or new_offset > high:
                    raise err(
                        "SUBSCRIPTION_OFFSET_OUT_OF_RANGE",
                        "订阅偏移越界",
                        subscription=name,
                        offset=new_offset,
                        high_watermark=high,
                    )
                if new_offset < current_offset:
                    raise err(
                        "SUBSCRIPTION_REWIND",
                        "订阅偏移不能回退",
                        subscription=name,
                        current_offset=current_offset,
                        offset=new_offset,
                    )
                if channel != current_row["channel"] and new_offset != current_offset:
                    raise err(
                        "EVENT_LOCKED",
                        "改变订阅通道时不能同时改变偏移",
                        subscription=name,
                    )
                config.subscriptions[name] = SubscriptionDef(
                    name=name, channel=channel, start_offset=new_offset
                )
                connection.execute(
                    "UPDATE subscriptions SET channel=?, offset=?, config_version=?, "
                    "active=1, updated_at_ms=? WHERE name=?",
                    (channel, new_offset, config_version, now_ms(), name),
                )
                summary["subscriptions"]["update"] += 1
            else:
                raise err("PATCH_INVALID", "订阅补丁 op 必须为 add/update/delete", index=index)

    def _bind_bridge(
        self, bridge_id: str
    ) -> tuple[int, Config, BridgeDef, int]:
        connection = connect(self.db_path)
        try:
            version, config = get_config(connection)
            bridge = config.bridges.get(bridge_id)
            if bridge is None:
                raise err("NOT_FOUND", "桥接规则不存在", bridge=bridge_id)
            position = connection.execute(
                "SELECT next_offset FROM bridge_positions WHERE bridge_id=? AND active=1",
                (bridge_id,),
            ).fetchone()
            if position is None:
                raise err("NOT_FOUND", "桥接位点不存在", bridge=bridge_id)
            return version, config, bridge, int(position["next_offset"])
        finally:
            connection.close()

    def _create_bridge_intent(
        self, bridge_id: str, config_version: int, start_offset: int
    ) -> int:
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                with transaction(connection):
                    cursor = connection.execute(
                        "INSERT INTO bridge_runs(bridge_id, config_version, start_offset, "
                        "status, started_at_ms) VALUES(?, ?, ?, 'running', ?)",
                        (bridge_id, config_version, start_offset, now_ms()),
                    )
                    return int(cursor.lastrowid)
            finally:
                connection.close()

    def _execute_bridge_run(
        self,
        run_id: int,
        *,
        bridge_id: str,
        bound_version: int,
        max_events: int | None,
        clock_ms: int | None,
    ) -> dict[str, Any]:
        with file_lock(self.db_path):
            connection = connect(self.db_path)
            try:
                current_version = get_config_version(connection)
                if current_version != bound_version:
                    with transaction(connection):
                        connection.execute(
                            "UPDATE bridge_runs SET status='aborted', error_code='CONFIG_CHANGED', "
                            "completed_at_ms=?, details_json=? WHERE id=? AND status='running'",
                            (
                                now_ms(),
                                canonical_json(
                                    {
                                        "bound_version": bound_version,
                                        "current_version": current_version,
                                    }
                                ),
                                run_id,
                            ),
                        )
                    raise err(
                        "CONFIG_CHANGED",
                        "桥接转发绑定的配置版本已变化",
                        bridge=bridge_id,
                        bound_version=bound_version,
                        current_version=current_version,
                    )
                with transaction(connection):
                    version, config = get_config(connection)
                    if version != bound_version:
                        raise err(
                            "CONFIG_CHANGED",
                            "桥接转发绑定的配置版本已变化",
                            bridge=bridge_id,
                        )
                    bridge = config.bridges.get(bridge_id)
                    if bridge is None:
                        raise err("CONFIG_CHANGED", "桥接规则已被删除", bridge=bridge_id)
                    source = config.channels[bridge.source]
                    target = config.channels[bridge.target]
                    if bridge.source_generation != source.generation:
                        raise err(
                            "GENERATION_MISMATCH",
                            "上游通道代数与桥接规则不一致",
                            bridge=bridge_id,
                            expected=bridge.source_generation,
                            actual=source.generation,
                        )
                    if bridge.target_generation != target.generation:
                        raise err(
                            "GENERATION_MISMATCH",
                            "下游通道代数与桥接规则不一致",
                            bridge=bridge_id,
                            expected=bridge.target_generation,
                            actual=target.generation,
                        )
                    position = connection.execute(
                        "SELECT * FROM bridge_positions WHERE bridge_id=? AND active=1",
                        (bridge_id,),
                    ).fetchone()
                    if position is None:
                        raise err("NOT_FOUND", "桥接位点不存在", bridge=bridge_id)
                    if (
                        position["source_channel"] != bridge.source
                        or position["target_channel"] != bridge.target
                    ):
                        raise err(
                            "CONFIG_CHANGED",
                            "桥接位点与当前规则通道不一致",
                            bridge=bridge_id,
                        )
                    start_offset = int(position["next_offset"])
                    source_row = connection.execute(
                        "SELECT low_watermark, high_watermark FROM channels WHERE name=?",
                        (bridge.source,),
                    ).fetchone()
                    high = int(source_row["high_watermark"])
                    low = int(source_row["low_watermark"])
                    if high < bridge.min_upstream_watermark:
                        raise err(
                            "BRIDGE_UPSTREAM_WATERMARK",
                            "上游提交水位低于桥接依赖水位",
                            bridge=bridge_id,
                            high_watermark=high,
                            required=bridge.min_upstream_watermark,
                        )
                    if high < start_offset or start_offset < low:
                        raise err(
                            "BRIDGE_UPSTREAM_WATERMARK",
                            "上游偏移水位发生回退或必要事件已被清理",
                            bridge=bridge_id,
                            start_offset=start_offset,
                            low_watermark=low,
                            high_watermark=high,
                        )
                    if max_events is not None and (
                        not is_int(max_events) or max_events <= 0
                    ):
                        raise err("PATCH_INVALID", "max_events 必须为正整数")
                    end_limit = min(
                        high,
                        start_offset
                        + (max_events if max_events is not None else bridge.max_batch),
                    )
                    effective_clock = update_clock(
                        connection, clock_ms, config.clock_tolerance_ms
                    )
                    matched = 0
                    filtered = 0
                    delivered = 0
                    last_digest = None
                    target_appended = 0
                    created_at = now_ms()
                    pending_target: list[dict[str, Any]] = []
                    for offset in range(start_offset, end_limit):
                        source_event = connection.execute(
                            "SELECT * FROM events WHERE channel=? AND offset=?",
                            (bridge.source, offset),
                        ).fetchone()
                        if source_event is None or not source_event["committed"]:
                            raise err(
                                "BRIDGE_UPSTREAM_WATERMARK",
                                "上游事件尚未提交或已缺失",
                                bridge=bridge_id,
                                channel=bridge.source,
                                offset=offset,
                            )
                        fields = json.loads(source_event["fields_json"])
                        if payload_digest(fields) != source_event["payload_sha256"]:
                            raise err(
                                "BRIDGE_DIGEST_MISMATCH",
                                "上游事件载荷摘要校验失败",
                                bridge=bridge_id,
                                offset=offset,
                            )
                        last_digest = source_event["payload_sha256"]
                        is_match = evaluate_filter(bridge.filter, fields)
                        if not is_match:
                            filtered += 1
                            connection.execute(
                                "INSERT INTO bridge_checks(bridge_id, source_offset, matched, "
                                "source_payload_sha256, source_fields_json, source_logical_clock, "
                                "source_produced_at_ms, source_tombstone, transform_digest, "
                                "config_version, reason, created_at_ms) "
                                "VALUES(?, ?, 0, ?, ?, ?, ?, ?, NULL, ?, 'filtered', ?) "
                                "ON CONFLICT(bridge_id, source_offset) DO UPDATE SET "
                                "matched=0, source_fields_json=excluded.source_fields_json, "
                                "source_logical_clock=excluded.source_logical_clock, "
                                "source_produced_at_ms=excluded.source_produced_at_ms, "
                                "source_tombstone=excluded.source_tombstone, "
                                "reason='filtered', config_version=excluded.config_version",
                                (
                                    bridge_id,
                                    offset,
                                    source_event["payload_sha256"],
                                    source_event["fields_json"],
                                    int(source_event["logical_clock"]),
                                    int(source_event["produced_at_ms"]),
                                    int(source_event["tombstone"]),
                                    version,
                                    created_at,
                                ),
                            )
                            continue
                        matched += 1
                        transformed, trace = apply_transforms(fields, bridge.transforms)
                        for field in target.field_schema:
                            if field.name not in transformed:
                                transformed[field.name] = field.default
                            elif (
                                transformed[field.name] is not None
                                and field.type != "any"
                            ):
                                transformed[field.name] = typed_value(
                                    transformed[field.name], field.type
                                )
                        transform_digest = payload_digest(transformed)
                        existing_delivery = connection.execute(
                            "SELECT * FROM bridge_deliveries WHERE bridge_id=? AND source_offset=?",
                            (bridge_id, offset),
                        ).fetchone()
                        if existing_delivery is not None:
                            if existing_delivery["transform_digest"] != transform_digest:
                                raise err(
                                    "BRIDGE_DIGEST_MISMATCH",
                                    "重复投递的变换摘要不一致",
                                    bridge=bridge_id,
                                    offset=offset,
                                )
                        else:
                            pending_target.append(
                                {
                                    "source_offset": offset,
                                    "fields": transformed,
                                    "digest": transform_digest,
                                    "trace": trace,
                                    "partition_key": source_event["partition_key"],
                                    "produced_at_ms": int(source_event["produced_at_ms"]),
                                    "logical_clock": int(source_event["logical_clock"]),
                                }
                            )
                        connection.execute(
                            "INSERT INTO bridge_checks(bridge_id, source_offset, matched, "
                            "source_payload_sha256, source_fields_json, source_logical_clock, "
                            "source_produced_at_ms, source_tombstone, transform_digest, "
                            "config_version, reason, created_at_ms) "
                            "VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 'matched', ?) "
                            "ON CONFLICT(bridge_id, source_offset) DO UPDATE SET "
                            "matched=1, transform_digest=excluded.transform_digest, "
                            "source_fields_json=excluded.source_fields_json, "
                            "source_logical_clock=excluded.source_logical_clock, "
                            "source_produced_at_ms=excluded.source_produced_at_ms, "
                            "source_tombstone=excluded.source_tombstone, "
                            "reason='matched', config_version=excluded.config_version",
                            (
                                bridge_id,
                                offset,
                                source_event["payload_sha256"],
                                source_event["fields_json"],
                                int(source_event["logical_clock"]),
                                int(source_event["produced_at_ms"]),
                                int(source_event["tombstone"]),
                                transform_digest,
                                version,
                                created_at,
                            ),
                        )
                    for item in pending_target:
                        target_results, added, _ = self._append_specs(
                            connection,
                            config,
                            target,
                            [
                                {
                                    "partition_key": item["partition_key"],
                                    "logical_clock": item["logical_clock"],
                                    "produced_at_ms": item["produced_at_ms"],
                                    "fields": item["fields"],
                                }
                            ],
                            config_version=version,
                            batch_size=1,
                            created_at_ms=created_at,
                            default_produced_at_ms=effective_clock,
                            source_version_override="bridge",
                            source_generation_override=target.generation,
                            raw_override={
                                "bridge_id": bridge_id,
                                "source_channel": bridge.source,
                                "source_offset": item["source_offset"],
                                "transform_trace": item["trace"],
                            },
                        )
                        target_offset = target_results[0]["offset"]
                        stored_target_digest = connection.execute(
                            "SELECT payload_sha256 FROM events WHERE channel=? AND offset=?",
                            (target.name, target_offset),
                        ).fetchone()["payload_sha256"]
                        if stored_target_digest != item["digest"]:
                            raise err(
                                "BRIDGE_DIGEST_MISMATCH",
                                "下游持久载荷摘要与变换结果不一致",
                                bridge=bridge_id,
                                source_offset=item["source_offset"],
                                target_offset=target_offset,
                            )
                        connection.execute(
                            "INSERT INTO bridge_deliveries(bridge_id, source_offset, "
                            "target_channel, target_offset, transform_digest, "
                            "source_payload_sha256, config_version, rule_snapshot_json, "
                            "transform_trace_json, created_at_ms) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                bridge_id,
                                item["source_offset"],
                                target.name,
                                target_offset,
                                item["digest"],
                                connection.execute(
                                    "SELECT payload_sha256 FROM events WHERE channel=? AND offset=?",
                                    (bridge.source, item["source_offset"]),
                                ).fetchone()["payload_sha256"],
                                version,
                                canonical_json(bridge.to_dict()),
                                canonical_json(item["trace"]),
                                created_at,
                            ),
                        )
                        target_appended += added
                        delivered += 1
                    high_after = int(
                        connection.execute(
                            "SELECT high_watermark FROM channels WHERE name=?",
                            (bridge.source,),
                        ).fetchone()["high_watermark"]
                    )
                    if high_after < end_limit:
                        raise err(
                            "BRIDGE_UPSTREAM_WATERMARK",
                            "桥接过程中上游水位回退",
                            bridge=bridge_id,
                            expected=end_limit,
                            actual=high_after,
                        )
                    connection.execute(
                        "UPDATE bridge_positions SET next_offset=?, last_digest=?, "
                        "config_version=?, updated_at_ms=? WHERE bridge_id=?",
                        (
                            end_limit,
                            last_digest
                            if end_limit > start_offset
                            else position["last_digest"],
                            version,
                            now_ms(),
                            bridge_id,
                        ),
                    )
                    connection.execute(
                        "UPDATE bridge_runs SET end_offset=?, status='completed', "
                        "completed_at_ms=?, details_json=? WHERE id=?",
                        (
                            end_limit,
                            now_ms(),
                            canonical_json(
                                {
                                    "start_offset": start_offset,
                                    "end_offset": end_limit,
                                    "matched": matched,
                                    "filtered": filtered,
                                    "delivered": delivered,
                                    "target_appended": target_appended,
                                    "config_version": version,
                                }
                            ),
                            run_id,
                        ),
                    )
                    update_clock(
                        connection,
                        effective_clock,
                        config.clock_tolerance_ms,
                        event_clock=(
                            int(
                                connection.execute(
                                    "SELECT COALESCE(MAX(logical_clock), 0) AS v FROM events"
                                ).fetchone()["v"]
                            )
                        ),
                    )
                    hit("bridge_before_commit")
                hit("bridge_after_commit")
                return {
                    "ok": True,
                    "bridge_id": bridge_id,
                    "run_id": run_id,
                    "config_version": version,
                    "start_offset": start_offset,
                    "next_offset": end_limit,
                    "matched": matched,
                    "filtered": filtered,
                    "delivered": delivered,
                    "target_appended": target_appended,
                    "last_digest": (
                        last_digest
                        if end_limit > start_offset
                        else position["last_digest"]
                    ),
                }
            except BusinessError:
                connection.execute(
                    "UPDATE bridge_runs SET status='failed', error_code=?, "
                    "completed_at_ms=?, details_json=? WHERE id=? AND status='running'",
                    (
                        _current_error_code(),
                        now_ms(),
                        canonical_json(
                            {"error": _current_error_details()}
                        ),
                        run_id,
                    ),
                )
                raise
            finally:
                connection.close()

    def _reconcile_runtime_config(
        self, connection: Any, config: Config, version: int
    ) -> None:
        for channel in config.channels.values():
            row = connection.execute(
                "SELECT high_watermark FROM channels WHERE name=?", (channel.name,)
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO channels(name, generation, partitions, field_schema_json, "
                    "config_version, low_watermark, high_watermark, retention_bytes) "
                    "VALUES(?, ?, ?, ?, ?, 0, 0, ?)",
                    (
                        channel.name,
                        channel.generation,
                        channel.partitions,
                        canonical_json([item.to_dict() for item in channel.field_schema]),
                        version,
                        config.retention_bytes,
                    ),
                )
            else:
                connection.execute(
                    "UPDATE channels SET generation=?, partitions=?, field_schema_json=?, "
                    "config_version=?, retention_bytes=? WHERE name=?",
                    (
                        channel.generation,
                        channel.partitions,
                        canonical_json([item.to_dict() for item in channel.field_schema]),
                        version,
                        config.retention_bytes,
                        channel.name,
                    ),
                )
        active_bridges = set(config.bridges)
        for bridge_id, bridge in config.bridges.items():
            row = connection.execute(
                "SELECT * FROM bridge_positions WHERE bridge_id=?", (bridge_id,)
            ).fetchone()
            high = int(
                connection.execute(
                    "SELECT high_watermark FROM channels WHERE name=?", (bridge.source,)
                ).fetchone()["high_watermark"]
            )
            if row is None:
                connection.execute(
                    "INSERT INTO bridge_positions(bridge_id, source_channel, target_channel, "
                    "next_offset, last_digest, source_generation, target_generation, "
                    "config_version, active, updated_at_ms) "
                    "VALUES(?, ?, ?, 0, NULL, ?, ?, ?, 1, ?)",
                    (
                        bridge_id,
                        bridge.source,
                        bridge.target,
                        bridge.source_generation,
                        bridge.target_generation,
                        version,
                        now_ms(),
                    ),
                )
            else:
                if (
                    row["source_channel"] != bridge.source
                    or row["target_channel"] != bridge.target
                ) and int(row["next_offset"]) > 0:
                    raise err(
                        "CONFIG_VERSION_INVALID",
                        "目标配置版本会改变已推进桥接的通道",
                        bridge=bridge_id,
                    )
                if int(row["next_offset"]) > high:
                    raise err(
                        "BRIDGE_UPSTREAM_WATERMARK",
                        "目标配置版本的上游水位低于桥接位点",
                        bridge=bridge_id,
                        next_offset=int(row["next_offset"]),
                        high_watermark=high,
                    )
                connection.execute(
                    "UPDATE bridge_positions SET source_channel=?, target_channel=?, "
                    "source_generation=?, target_generation=?, config_version=?, active=1, "
                    "updated_at_ms=? WHERE bridge_id=?",
                    (
                        bridge.source,
                        bridge.target,
                        bridge.source_generation,
                        bridge.target_generation,
                        version,
                        now_ms(),
                        bridge_id,
                    ),
                )
        for row in connection.execute(
            "SELECT bridge_id FROM bridge_positions WHERE active=1"
        ):
            if row["bridge_id"] not in active_bridges:
                connection.execute(
                    "UPDATE bridge_positions SET active=0, config_version=? WHERE bridge_id=?",
                    (version, row["bridge_id"]),
                )
        active_subscriptions = set(config.subscriptions)
        for name, subscription in config.subscriptions.items():
            row = connection.execute(
                "SELECT * FROM subscriptions WHERE name=?", (name,)
            ).fetchone()
            high = int(
                connection.execute(
                    "SELECT high_watermark FROM channels WHERE name=?",
                    (subscription.channel,),
                ).fetchone()["high_watermark"]
            )
            if row is None:
                connection.execute(
                    "INSERT INTO subscriptions(name, channel, offset, config_version, "
                    "active, updated_at_ms) VALUES(?, ?, ?, ?, 1, ?)",
                    (
                        name,
                        subscription.channel,
                        subscription.start_offset,
                        version,
                        now_ms(),
                    ),
                )
            else:
                offset = int(row["offset"])
                if row["channel"] != subscription.channel and offset > 0:
                    raise err(
                        "CONFIG_VERSION_INVALID",
                        "目标配置版本会改变已有进度的订阅通道",
                        subscription=name,
                    )
                if offset > high:
                    raise err(
                        "SUBSCRIPTION_OFFSET_OUT_OF_RANGE",
                        "订阅偏移高于目标配置通道水位",
                        subscription=name,
                    )
                connection.execute(
                    "UPDATE subscriptions SET channel=?, offset=?, config_version=?, "
                    "active=1, updated_at_ms=? WHERE name=?",
                    (subscription.channel, offset, version, now_ms(), name),
                )
        for row in connection.execute(
            "SELECT name FROM subscriptions WHERE active=1"
        ):
            if row["name"] not in active_subscriptions:
                connection.execute(
                    "UPDATE subscriptions SET active=0, config_version=? WHERE name=?",
                    (version, row["name"]),
                )

    def _cleanup_reclaim(
        self,
        connection: Any,
        channels: dict[str, dict[str, Any]],
        safe_offsets: dict[str, int],
        retention: int,
    ) -> tuple[list[dict[str, Any]], int, int, dict[str, int]]:
        items: list[dict[str, Any]] = []
        removed = 0
        marked = 0
        bytes_after = self._retained_bytes(connection)
        cleared_up_to: dict[str, int] = {}
        rows = connection.execute(
            "SELECT channel, offset, tombstone, compacted, fields_json, raw_repr_json "
            "FROM events ORDER BY channel, offset"
        ).fetchall()
        for row in rows:
            if bytes_after <= retention:
                break
            channel = row["channel"]
            offset = int(row["offset"])
            if offset >= safe_offsets[channel]:
                continue
            size = len(row["fields_json"].encode("utf-8")) + len(
                row["raw_repr_json"].encode("utf-8")
            )
            if bool(row["tombstone"]):
                if bool(row["compacted"]):
                    cleared_up_to[channel] = max(
                        cleared_up_to.get(channel, 0), offset + 1
                    )
                    continue
                compacted_fields = canonical_json({})
                compacted_raw = canonical_json({"compacted": True, "tombstone": True})
                reclaimed = max(
                    0,
                    size
                    - len(compacted_fields.encode("utf-8"))
                    - len(compacted_raw.encode("utf-8")),
                )
                connection.execute(
                    "UPDATE events SET fields_json=?, payload_sha256=?, "
                    "raw_repr_json=?, compacted=1 WHERE channel=? AND offset=?",
                    (
                        compacted_fields,
                        payload_digest({}),
                        compacted_raw,
                        channel,
                        offset,
                    ),
                )
                bytes_after -= reclaimed
                marked += 1
                items.append(
                    {
                        "channel": channel,
                        "offset": offset,
                        "action": "tombstone_marked",
                        "bytes_reclaimed": reclaimed,
                        "reason": "consumed_tombstone_retained_as_marker",
                    }
                )
            else:
                connection.execute(
                    "DELETE FROM events WHERE channel=? AND offset=?", (channel, offset)
                )
                bytes_after -= size
                removed += 1
                items.append(
                    {
                        "channel": channel,
                        "offset": offset,
                        "action": "physically_removed",
                        "bytes_reclaimed": size,
                        "reason": "below_safe_offset_and_fully_consumed",
                    }
                )
            cleared_up_to[channel] = max(cleared_up_to.get(channel, 0), offset + 1)
        return items, removed, marked, cleared_up_to

    def _retained_bytes(self, connection: Any) -> int:
        return int(
            connection.execute(
                "SELECT COALESCE(SUM(LENGTH(CAST(fields_json AS BLOB)) + "
                "LENGTH(CAST(raw_repr_json AS BLOB))), 0) AS n FROM events"
            ).fetchone()["n"]
        )

    def _clear_all_state(self, connection: Any) -> None:
        for table in (
            "operation_intents",
            "cleanup_items",
            "cleanup_runs",
            "subscription_commits",
            "bridge_checks",
            "bridge_deliveries",
            "bridge_runs",
            "merge_decisions",
            "degradations",
            "batches",
            "events",
            "subscriptions",
            "bridge_positions",
            "channels",
            "clock_state",
            "config_pointer",
            "config_versions",
        ):
            connection.execute(f"DELETE FROM {table}")

    def _event_for_output(self, row: Any) -> dict[str, Any]:
        return {
            "channel": row["channel"],
            "offset": int(row["offset"]),
            "sequence": int(row["sequence"]),
            "partition": int(row["partition"]),
            "partition_key": row["partition_key"],
            "logical_clock": int(row["logical_clock"]),
            "produced_at_ms": int(row["produced_at_ms"]),
            "fields": json.loads(row["fields_json"]),
            "payload_sha256": row["payload_sha256"],
            "source_version": row["source_version"],
            "source_generation": int(row["source_generation"]),
            "time_conversion": (
                json.loads(row["time_conversion_json"])
                if row["time_conversion_json"]
                else None
            ),
            "tombstone": bool(row["tombstone"]),
            "compacted": bool(row["compacted"]),
        }

    @staticmethod
    def _partition(channel: str, partitions: int, partition_key: str) -> int:
        digest = hashlib.sha256(
            f"{channel}\0{partition_key}".encode("utf-8")
        ).digest()
        return int.from_bytes(digest[:8], "big") % partitions

    @staticmethod
    def _verify_error(
        code: str, channel: str | None, **details: Any
    ) -> dict[str, Any]:
        return {"code": code, "channel": channel, "details": details}


def _current_error_code() -> str:
    import sys

    exc = sys.exc_info()[1]
    if isinstance(exc, BusinessError):
        return exc.code
    return "INTERNAL_ERROR"


def _current_error_details() -> dict[str, Any]:
    import sys

    exc = sys.exc_info()[1]
    if isinstance(exc, BusinessError):
        return {"message": exc.message, "details": exc.details}
    return {"message": str(exc) if exc else "unknown"}
