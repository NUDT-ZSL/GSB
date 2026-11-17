"""SQLite session store with crash-safe snapshots and switch transactions."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .codes import BusinessCode
from .errors import BusinessError
from .models import (
    BusinessEvent,
    InstanceRuntime,
    PolicySpec,
    ProbeAttempt,
    ProbeConfig,
    RecoveryReport,
    ServiceRuntime,
    StateTransition,
    SwitchDecision,
)
from .jsonl_logger import JsonlLogger
from .models import Scene


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
    service_id TEXT NOT NULL,
    instance_id TEXT PRIMARY KEY,
    role TEXT NOT NULL,
    port INTEGER NOT NULL,
    version INTEGER NOT NULL,
    isolated INTEGER NOT NULL DEFAULT 0,
    health_state TEXT NOT NULL DEFAULT 'unknown',
    consecutive_successes INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    probe_json TEXT NOT NULL,
    requires_reprobe INTEGER NOT NULL DEFAULT 0,
    updated_at_epoch_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS policies (
    service_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    cooldown_ms INTEGER NOT NULL,
    min_healthy_replicas INTEGER NOT NULL,
    policy_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_runtime (
    service_id TEXT PRIMARY KEY,
    cooldown_started_epoch_ms INTEGER,
    last_switch_target TEXT,
    switching INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS probe_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    conclusion TEXT NOT NULL,
    detail TEXT NOT NULL,
    started_mono_ms INTEGER NOT NULL,
    ended_mono_ms INTEGER NOT NULL,
    created_epoch_ms INTEGER NOT NULL,
    fault_kind TEXT
);
CREATE INDEX IF NOT EXISTS idx_probe_run ON probe_attempts(run_id, round_no);
CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    service_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    at_mono_ms INTEGER NOT NULL,
    at_epoch_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transition_run ON state_transitions(run_id, round_no);
CREATE TABLE IF NOT EXISTS business_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    code TEXT NOT NULL,
    service_id TEXT,
    instance_id TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    at_epoch_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_run ON business_events(run_id, round_no);
CREATE TABLE IF NOT EXISTS switch_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    service_id TEXT NOT NULL,
    code TEXT NOT NULL,
    source_instance TEXT,
    target_instance TEXT,
    trigger_reason TEXT,
    policy_version TEXT NOT NULL,
    switched INTEGER NOT NULL DEFAULT 0,
    started_epoch_ms INTEGER NOT NULL,
    ended_epoch_ms INTEGER,
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS switch_transactions (
    service_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    started_epoch_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_switches (
    request_id TEXT PRIMARY KEY,
    service_id TEXT NOT NULL,
    requested_epoch_ms INTEGER NOT NULL,
    trigger_reason TEXT NOT NULL,
    source_instance TEXT NOT NULL,
    target_instance TEXT,
    policy_version TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scene_version TEXT NOT NULL,
    scene_fingerprint TEXT NOT NULL,
    created_epoch_ms INTEGER NOT NULL,
    is_complete INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SnapshotRecord:
    snapshot_id: int
    scene_version: str
    scene_fingerprint: str
    created_epoch_ms: int
    is_complete: bool
    payload: dict[str, Any]


class SessionRepository:
    def __init__(
        self,
        path: str | Path,
        *,
        logger: JsonlLogger | None = None,
        fail_writes: bool = False,
        fail_snapshot_writes: bool = False,
    ) -> None:
        self.path = str(path)
        self.logger = logger
        self.fail_writes = fail_writes
        self.fail_snapshot_writes = fail_snapshot_writes
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                self.path,
                timeout=10.0,
                isolation_level=None,
                check_same_thread=False,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.execute("PRAGMA synchronous=FULL")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self.ensure_schema()
        except sqlite3.Error as exc:
            try:
                self._conn.close()
            except (AttributeError, sqlite3.Error):
                pass
            raise BusinessError(
                BusinessCode.E_SNAPSHOT,
                "打开或初始化会话库失败",
                details={"db": self.path, "error": f"{type(exc).__name__}: {exc}"},
            ) from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "SessionRepository":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _ensure_write(self, operation: str, *, snapshot: bool = False) -> None:
        if self.fail_writes or (snapshot and self.fail_snapshot_writes):
            raise BusinessError(
                BusinessCode.E_SNAPSHOT,
                f"会话库写入失败（注入）: {operation}",
                details={"operation": operation, "db": self.path, "injected": True},
            )

    def _meta_get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def _meta_set(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def initialize_scene(self, scene: Scene, *, clear_snapshots: bool = False) -> None:
        with self._lock:
            self._ensure_write("initialize_scene")
            now = int(time.time() * 1000)
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._clear_state_tables()
                self._meta_set("scene_version", scene.scene_version)
                self._meta_set("scene_fingerprint", scene.fingerprint)
                self._meta_set("round_no", "0")
                for service in sorted(scene.services, key=lambda item: item.service_id):
                    self._conn.execute(
                        "INSERT INTO policies(service_id,mode,cooldown_ms,min_healthy_replicas,policy_version)"
                        " VALUES(?,?,?,?,?)",
                        (
                            service.service_id,
                            service.policy.mode,
                            service.policy.cooldown_ms,
                            service.policy.min_healthy_replicas,
                            service.policy.version,
                        ),
                    )
                    self._conn.execute(
                        "INSERT INTO service_runtime(service_id,cooldown_started_epoch_ms,last_switch_target,switching)"
                        " VALUES(?,NULL,NULL,0)",
                        (service.service_id,),
                    )
                    for instance in sorted(
                        service.instances, key=lambda item: item.instance_id
                    ):
                        self._conn.execute(
                            "INSERT INTO instances(service_id,instance_id,role,port,version,isolated,"
                            "health_state,consecutive_successes,consecutive_failures,probe_json,"
                            "requires_reprobe,updated_at_epoch_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                service.service_id,
                                instance.instance_id,
                                instance.role,
                                instance.port,
                                1,
                                0,
                                "unknown",
                                0,
                                0,
                                json.dumps(instance.probe.as_dict(), sort_keys=True),
                                0,
                                now,
                            ),
                        )
                if clear_snapshots:
                    self._conn.execute("DELETE FROM snapshots")
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _clear_state_tables(self) -> None:
        for table in (
            "probe_attempts",
            "state_transitions",
            "business_events",
            "switch_decisions",
            "switch_transactions",
            "pending_switches",
            "instances",
            "policies",
            "service_runtime",
        ):
            self._conn.execute(f"DELETE FROM {table}")

    def has_session(self) -> bool:
        with self._lock:
            return self._meta_get("scene_version") is not None

    def open_scene(self, scene: Scene) -> RecoveryReport:
        """Open a scene and restore the latest complete snapshot if present.

        A snapshot is deliberately restored as a whole.  Runtime rows are never
        merged field-by-field with whatever happens to be in the database.
        """

        with self._lock:
            latest = self.latest_snapshot()
            if latest is None:
                current_version = self._meta_get("scene_version")
                if current_version is None:
                    self.initialize_scene(scene)
                    return RecoveryReport(
                        scene_version_match=True,
                        snapshot_id=None,
                        full_replace=False,
                        incomplete_services=(),
                        requires_reprobe_services=(),
                        messages=("初始化新场景会话",),
                    )
                if current_version != scene.scene_version:
                    self.initialize_scene(scene, clear_snapshots=True)
                    return RecoveryReport(
                        scene_version_match=False,
                        snapshot_id=None,
                        full_replace=True,
                        incomplete_services=(),
                        requires_reprobe_services=tuple(
                            sorted(service.service_id for service in scene.services)
                        ),
                        messages=(
                            "没有快照且数据库场景版本不匹配，已按当前场景整体重建",
                        ),
                    )
                incomplete = {
                    str(row["service_id"])
                    for row in self._conn.execute(
                        "SELECT service_id FROM switch_transactions ORDER BY service_id"
                    ).fetchall()
                }
                unverified = self._services_with_unverified_health()
                if incomplete or unverified:
                    run_id = "recovery-no-snapshot"
                    round_no = self._next_round_unlocked()
                    for service_id in sorted(incomplete | unverified):
                        self._mark_service_unknown_unlocked(
                            service_id,
                            run_id=run_id,
                            round_no=round_no,
                            reason=(
                                "restore_incomplete"
                                if service_id in incomplete
                                else "probe_missing"
                            ),
                        )
                    self._conn.execute("DELETE FROM switch_transactions")
                    self._conn.execute("UPDATE service_runtime SET switching=0")
                return RecoveryReport(
                    scene_version_match=True,
                    snapshot_id=None,
                    full_replace=False,
                    incomplete_services=tuple(sorted(incomplete)),
                    requires_reprobe_services=tuple(sorted(incomplete | unverified)),
                    messages=(
                        "从既有会话运行态打开（无快照）",
                        *(
                            ("检测到未完成切换事务，相关服务已标记 unknown 并要求重跑",)
                            if incomplete
                            else ()
                        ),
                        *(
                            ("检测到无探测记录的状态，相关服务已回到 unknown",)
                            if unverified
                            else ()
                        ),
                    ),
                )

            incomplete_before = {
                str(row["service_id"])
                for row in self._conn.execute(
                    "SELECT service_id FROM switch_transactions ORDER BY service_id"
                ).fetchall()
            }
            incomplete_before.update(
                str(row["service_id"])
                for row in self._conn.execute(
                    "SELECT service_id FROM service_runtime WHERE switching=1"
                ).fetchall()
            )
            dirty_services = self._services_with_events_after_watermark(latest.payload)
            self._replace_runtime_from_payload(latest.payload)
            match = latest.scene_version == scene.scene_version
            messages: list[str] = ["已整体替换为最新快照运行态"]
            required: set[str] = set()
            unverified = self._services_with_unverified_health()
            if not match:
                messages.append(
                    f"快照场景版本 {latest.scene_version!r} 与当前场景 {scene.scene_version!r} 不一致；已整体替换并要求重跑"
                )
                required.update(
                    service.policy.service_id for service in self._services_from_payload()
                )
            if not latest.is_complete or not bool(latest.payload.get("complete", True)):
                messages.append("快照未完整完成，所有服务要求重跑")
                required.update(
                    service.policy.service_id for service in self._services_from_payload()
                )

            incomplete = set(incomplete_before)
            incomplete.update(
                str(row["service_id"])
                for row in self._conn.execute(
                    "SELECT service_id FROM service_runtime WHERE switching=1"
                ).fetchall()
            )
            if incomplete:
                messages.append("检测到未完成切换事务，相关服务已标记 unknown 并要求重跑")
            if dirty_services:
                messages.append(
                    "检测到快照之后仍有已落库但未快照的探测/切换事件，相关服务已标记 unknown 并要求重跑"
                )
            if unverified:
                messages.append("检测到无探测记录的健康/不健康状态，相关服务已回到 unknown")
            run_id = f"recovery-{latest.snapshot_id}"
            round_no = self._next_round_unlocked()
            for service_id in sorted(required | incomplete | dirty_services | unverified):
                if not self._service_exists_unlocked(service_id):
                    continue
                self._mark_service_unknown_unlocked(
                    service_id,
                    run_id=run_id,
                    round_no=round_no,
                    reason=(
                        "restore_incomplete"
                        if service_id in incomplete
                        else "unfinished_round"
                        if service_id in dirty_services
                        else "probe_missing"
                        if service_id in unverified
                        else "scene_version_replaced"
                    ),
                )
            self._conn.execute("DELETE FROM switch_transactions")
            self._conn.execute("UPDATE service_runtime SET switching=0")
            return RecoveryReport(
                scene_version_match=match,
                snapshot_id=latest.snapshot_id,
                full_replace=True,
                incomplete_services=tuple(sorted(incomplete)),
                requires_reprobe_services=tuple(
                    sorted(required | incomplete | dirty_services | unverified)
                ),
                messages=tuple(messages),
            )

    def _services_with_unverified_health(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT i.service_id FROM instances i "
            "WHERE i.health_state!='unknown' AND NOT EXISTS ("
            "SELECT 1 FROM probe_attempts p WHERE p.instance_id=i.instance_id)"
        ).fetchall()
        return {str(row["service_id"]) for row in rows}

    def _services_with_events_after_watermark(self, payload: dict[str, Any]) -> set[str]:
        watermark = payload.get("watermark", {})
        services: set[str] = set()
        checks = (
            ("probe_attempts", "probe_attempts_watermark"),
            ("state_transitions", "state_transitions_watermark"),
            ("business_events", "business_events_watermark"),
            ("switch_decisions", "switch_decisions_watermark"),
        )
        for table, key in checks:
            after_id = int(watermark.get(key, 0) or 0)
            for row in self._conn.execute(
                f"SELECT service_id FROM {table} WHERE id>? AND service_id IS NOT NULL",
                (after_id,),
            ).fetchall():
                if row["service_id"] is not None:
                    services.add(str(row["service_id"]))
        return services

    def _services_from_payload(self) -> list[ServiceRuntime]:
        rows = self._conn.execute(
            "SELECT p.service_id,p.mode,p.cooldown_ms,p.min_healthy_replicas,p.policy_version,"
            "r.cooldown_started_epoch_ms,r.last_switch_target,r.switching "
            "FROM policies p LEFT JOIN service_runtime r ON r.service_id=p.service_id "
            "ORDER BY p.service_id"
        ).fetchall()
        return [self._service_from_row(row) for row in rows]

    def _service_exists_unlocked(self, service_id: str) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM policies WHERE service_id=?", (service_id,)
            ).fetchone()
            is not None
        )

    def _mark_service_unknown_unlocked(
        self, service_id: str, *, run_id: str, round_no: int, reason: str
    ) -> None:
        now = int(time.time() * 1000)
        mono = int(time.monotonic() * 1000)
        rows = self._conn.execute(
            "SELECT instance_id,health_state FROM instances WHERE service_id=? ORDER BY instance_id",
            (service_id,),
        ).fetchall()
        for row in rows:
            previous = str(row["health_state"])
            self._conn.execute(
                "UPDATE instances SET health_state='unknown',consecutive_successes=0,"
                "consecutive_failures=0,requires_reprobe=1,updated_at_epoch_ms=? "
                "WHERE instance_id=?",
                (now, row["instance_id"]),
            )
            if previous != "unknown" or reason != "probe_missing":
                self._conn.execute(
                    "INSERT INTO state_transitions(run_id,round_no,service_id,instance_id,"
                    "from_state,to_state,reason,at_mono_ms,at_epoch_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        round_no,
                        service_id,
                        row["instance_id"],
                        previous,
                        "unknown",
                        reason,
                        mono,
                        now,
                    ),
                )

    def _replace_runtime_from_payload(self, payload: dict[str, Any]) -> None:
        try:
            after = payload["after"]
            instances = after["instances"]
            services = after["services"]
            scene_version = str(payload["scene_version"])
            scene_fingerprint = str(payload["scene_fingerprint"])
        except (KeyError, TypeError) as exc:
            raise BusinessError(
                BusinessCode.E_SNAPSHOT,
                "快照负载缺少恢复所需字段",
                details={"error": str(exc), "snapshot": payload.get("snapshot_format")},
            ) from exc
        self._ensure_write("restore_snapshot")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            for table in (
                "instances",
                "policies",
                "service_runtime",
                "switch_transactions",
                "pending_switches",
            ):
                self._conn.execute(f"DELETE FROM {table}")
            for item in instances:
                probe = item["probe"]
                self._conn.execute(
                    "INSERT INTO instances(service_id,instance_id,role,port,version,isolated,"
                    "health_state,consecutive_successes,consecutive_failures,probe_json,"
                    "requires_reprobe,updated_at_epoch_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item["service_id"],
                        item["instance_id"],
                        item["role"],
                        int(item["port"]),
                        int(item["version"]),
                        1 if item["isolated"] else 0,
                        item["health_state"],
                        int(item["consecutive_successes"]),
                        int(item["consecutive_failures"]),
                        json.dumps(probe, sort_keys=True),
                        1 if item.get("requires_reprobe") else 0,
                        int(item.get("updated_at_epoch_ms", 0)),
                    ),
                )
            for item in services:
                self._conn.execute(
                    "INSERT INTO policies(service_id,mode,cooldown_ms,min_healthy_replicas,policy_version)"
                    " VALUES(?,?,?,?,?)",
                    (
                        item["service_id"],
                        item["mode"],
                        int(item["cooldown_ms"]),
                        int(item["min_healthy_replicas"]),
                        item["policy_version"],
                    ),
                )
                self._conn.execute(
                    "INSERT INTO service_runtime(service_id,cooldown_started_epoch_ms,last_switch_target,switching)"
                    " VALUES(?,?,?,?)",
                    (
                        item["service_id"],
                        item.get("cooldown_started_epoch_ms"),
                        item.get("last_switch_target"),
                        1 if item.get("switching") else 0,
                    ),
                )
            for item in after.get("pending_requests", []):
                self._conn.execute(
                    "INSERT INTO pending_switches(request_id,service_id,requested_epoch_ms,"
                    "trigger_reason,source_instance,target_instance,policy_version,status)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (
                        item["request_id"],
                        item["service_id"],
                        int(item["requested_epoch_ms"]),
                        item["trigger_reason"],
                        item["source_instance"],
                        item.get("target_instance"),
                        item["policy_version"],
                        item["status"],
                    ),
                )
            self._meta_set("scene_version", scene_version)
            self._meta_set("scene_fingerprint", scene_fingerprint)
            if self._meta_get("round_no") is None:
                self._meta_set("round_no", "0")
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def replace_runtime_from_state(
        self,
        state: dict[str, Any],
        *,
        scene_version: str,
        scene_fingerprint: str,
    ) -> None:
        """Replace current runtime state from a snapshot state section.

        This is used by replay to reconstruct the exact pre-cycle baseline in an
        isolated temporary database.
        """

        self._replace_runtime_from_payload(
            {
                "snapshot_format": 1,
                "scene_version": scene_version,
                "scene_fingerprint": scene_fingerprint,
                "after": state,
            }
        )

    def latest_snapshot(self) -> SnapshotRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id,scene_version,scene_fingerprint,created_epoch_ms,is_complete,payload_json "
                "FROM snapshots ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            try:
                payload = json.loads(row["payload_json"])
            except json.JSONDecodeError as exc:
                raise BusinessError(
                    BusinessCode.E_SNAPSHOT,
                    "快照 JSON 损坏",
                    details={"snapshot_id": row["id"], "error": str(exc)},
                ) from exc
            return SnapshotRecord(
                snapshot_id=int(row["id"]),
                scene_version=str(row["scene_version"]),
                scene_fingerprint=str(row["scene_fingerprint"]),
                created_epoch_ms=int(row["created_epoch_ms"]),
                is_complete=bool(row["is_complete"]),
                payload=payload,
            )

    def save_snapshot(
        self,
        scene: Scene,
        before: dict[str, Any],
        after: dict[str, Any],
        trace: dict[str, Any],
        *,
        complete: bool = True,
    ) -> int:
        with self._lock:
            self._ensure_write("save_snapshot", snapshot=True)
            watermark = {
                f"{table}_watermark": int(
                    self._conn.execute(
                        f"SELECT COALESCE(MAX(id),0) FROM {table}"
                    ).fetchone()[0]
                )
                for table in (
                    "probe_attempts",
                    "state_transitions",
                    "business_events",
                    "switch_decisions",
                )
            }
            payload = {
                "snapshot_format": 1,
                "scene_version": scene.scene_version,
                "scene_fingerprint": scene.fingerprint,
                "probe_limits": scene.probe_limits.as_dict(),
                "scene_config": scene.canonical_payload(),
                "created_epoch_ms": int(time.time() * 1000),
                "complete": complete,
                "before": before,
                "after": after,
                "trace": trace,
                "watermark": watermark,
            }
            try:
                encoded = json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
            except TypeError as exc:
                raise BusinessError(
                    BusinessCode.E_SNAPSHOT, "快照序列化失败", details={"error": str(exc)}
                ) from exc
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = self._conn.execute(
                    "INSERT INTO snapshots(scene_version,scene_fingerprint,created_epoch_ms,"
                    "is_complete,payload_json) VALUES(?,?,?,?,?)",
                    (
                        scene.scene_version,
                        scene.fingerprint,
                        int(time.time() * 1000),
                        1 if complete else 0,
                        encoded,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
            return int(cursor.lastrowid)

    def export_state(self) -> dict[str, Any]:
        with self._lock:
            instance_rows = self._conn.execute(
                "SELECT * FROM instances ORDER BY service_id,instance_id"
            ).fetchall()
            service_rows = self._conn.execute(
                "SELECT p.service_id,p.mode,p.cooldown_ms,p.min_healthy_replicas,p.policy_version,"
                "r.cooldown_started_epoch_ms,r.last_switch_target,r.switching "
                "FROM policies p LEFT JOIN service_runtime r ON r.service_id=p.service_id "
                "ORDER BY p.service_id"
            ).fetchall()
            switch_rows = self._conn.execute(
                "SELECT request_id,run_id,service_id,code,source_instance,target_instance,"
                "trigger_reason,policy_version,switched,started_epoch_ms,ended_epoch_ms,details_json "
                "FROM switch_decisions WHERE switched=1 ORDER BY id"
            ).fetchall()
            pending_rows = self._conn.execute(
                "SELECT request_id,service_id,requested_epoch_ms,trigger_reason,source_instance,"
                "target_instance,policy_version,status FROM pending_switches "
                "ORDER BY requested_epoch_ms,request_id"
            ).fetchall()
            return {
                "scene_version": self._meta_get("scene_version"),
                "scene_fingerprint": self._meta_get("scene_fingerprint"),
                "instances": [self._instance_from_row(row).as_dict() for row in instance_rows],
                "services": [self._service_from_row(row).as_dict() for row in service_rows],
                "completed_switches": [
                    {
                        **dict(row),
                        "switched": bool(row["switched"]),
                        "details": json.loads(row["details_json"]),
                    }
                    for row in switch_rows
                ],
                "pending_requests": [dict(row) for row in pending_rows],
            }

    def load_instances(self) -> dict[str, InstanceRuntime]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM instances ORDER BY service_id,instance_id"
            ).fetchall()
            return {
                str(row["instance_id"]): self._instance_from_row(row) for row in rows
            }

    def load_services(self) -> dict[str, ServiceRuntime]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT p.service_id,p.mode,p.cooldown_ms,p.min_healthy_replicas,p.policy_version,"
                "r.cooldown_started_epoch_ms,r.last_switch_target,r.switching "
                "FROM policies p LEFT JOIN service_runtime r ON r.service_id=p.service_id "
                "ORDER BY p.service_id"
            ).fetchall()
            return {str(row["service_id"]): self._service_from_row(row) for row in rows}

    @staticmethod
    def _instance_from_row(row: sqlite3.Row) -> InstanceRuntime:
        probe_data = json.loads(row["probe_json"])
        return InstanceRuntime(
            service_id=str(row["service_id"]),
            instance_id=str(row["instance_id"]),
            role=str(row["role"]),
            port=int(row["port"]),
            version=int(row["version"]),
            isolated=bool(row["isolated"]),
            health_state=str(row["health_state"]),
            consecutive_successes=int(row["consecutive_successes"]),
            consecutive_failures=int(row["consecutive_failures"]),
            probe=ProbeConfig(**probe_data),
            requires_reprobe=bool(row["requires_reprobe"]),
            updated_at_epoch_ms=int(row["updated_at_epoch_ms"]),
        )

    @staticmethod
    def _service_from_row(row: sqlite3.Row) -> ServiceRuntime:
        return ServiceRuntime(
            policy=PolicySpec(
                service_id=str(row["service_id"]),
                mode=str(row["mode"]),
                cooldown_ms=int(row["cooldown_ms"]),
                min_healthy_replicas=int(row["min_healthy_replicas"]),
                version=str(row["policy_version"]),
            ),
            cooldown_started_epoch_ms=(
                int(row["cooldown_started_epoch_ms"])
                if row["cooldown_started_epoch_ms"] is not None
                else None
            ),
            last_switch_target=(
                str(row["last_switch_target"])
                if row["last_switch_target"] is not None
                else None
            ),
            switching=bool(row["switching"]),
        )

    def add_probe_attempt(self, attempt: ProbeAttempt) -> None:
        with self._lock:
            self._ensure_write("add_probe_attempt")
            self._conn.execute(
                "INSERT INTO probe_attempts(run_id,round_no,service_id,instance_id,attempt_no,"
                "conclusion,detail,started_mono_ms,ended_mono_ms,created_epoch_ms,fault_kind)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt.run_id,
                    attempt.round_no,
                    attempt.service_id,
                    attempt.instance_id,
                    attempt.attempt_no,
                    attempt.conclusion,
                    attempt.detail,
                    attempt.started_mono_ms,
                    attempt.ended_mono_ms,
                    attempt.created_epoch_ms,
                    attempt.fault_kind,
                ),
            )

    def add_transition(self, transition: StateTransition) -> None:
        with self._lock:
            self._ensure_write("add_transition")
            self._conn.execute(
                "INSERT INTO state_transitions(run_id,round_no,service_id,instance_id,from_state,"
                "to_state,reason,at_mono_ms,at_epoch_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    transition.run_id,
                    transition.round_no,
                    transition.service_id,
                    transition.instance_id,
                    transition.from_state,
                    transition.to_state,
                    transition.reason,
                    transition.at_mono_ms,
                    transition.at_epoch_ms,
                ),
            )

    def update_instance(self, instance: InstanceRuntime) -> None:
        with self._lock:
            self._ensure_write("update_instance")
            self._conn.execute(
                "UPDATE instances SET role=?,port=?,version=?,isolated=?,health_state=?,"
                "consecutive_successes=?,consecutive_failures=?,probe_json=?,requires_reprobe=?,"
                "updated_at_epoch_ms=? WHERE instance_id=?",
                (
                    instance.role,
                    instance.port,
                    instance.version,
                    1 if instance.isolated else 0,
                    instance.health_state,
                    instance.consecutive_successes,
                    instance.consecutive_failures,
                    json.dumps(instance.probe.as_dict(), sort_keys=True),
                    1 if instance.requires_reprobe else 0,
                    instance.updated_at_epoch_ms,
                    instance.instance_id,
                ),
            )

    def apply_instance_transition(
        self, instance: InstanceRuntime, transition: StateTransition
    ) -> None:
        """Persist a health state and its transition in one transaction."""

        with self._lock:
            self._ensure_write("apply_instance_transition")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE instances SET role=?,port=?,version=?,isolated=?,health_state=?,"
                    "consecutive_successes=?,consecutive_failures=?,probe_json=?,requires_reprobe=?,"
                    "updated_at_epoch_ms=? WHERE instance_id=?",
                    (
                        instance.role,
                        instance.port,
                        instance.version,
                        1 if instance.isolated else 0,
                        instance.health_state,
                        instance.consecutive_successes,
                        instance.consecutive_failures,
                        json.dumps(instance.probe.as_dict(), sort_keys=True),
                        1 if instance.requires_reprobe else 0,
                        instance.updated_at_epoch_ms,
                        instance.instance_id,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO state_transitions(run_id,round_no,service_id,instance_id,from_state,"
                    "to_state,reason,at_mono_ms,at_epoch_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        transition.run_id,
                        transition.round_no,
                        transition.service_id,
                        transition.instance_id,
                        transition.from_state,
                        transition.to_state,
                        transition.reason,
                        transition.at_mono_ms,
                        transition.at_epoch_ms,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def record_business_event(self, event: BusinessEvent) -> None:
        with self._lock:
            self._ensure_write("record_business_event")
            self._conn.execute(
                "INSERT INTO business_events(run_id,round_no,code,service_id,instance_id,message,"
                "details_json,at_epoch_ms) VALUES(?,?,?,?,?,?,?,?)",
                (
                    event.run_id,
                    event.round_no,
                    event.code,
                    event.service_id,
                    event.instance_id,
                    event.message,
                    json.dumps(event.details, ensure_ascii=False, sort_keys=True),
                    event.at_epoch_ms,
                ),
            )

    def record_standalone_decision(self, decision: SwitchDecision) -> None:
        """Persist a non-transactional decision such as E_BUSY/E_COOLDOWN."""

        with self._lock:
            self._ensure_write("record_standalone_decision")
            self._conn.execute(
                "INSERT INTO switch_decisions(request_id,run_id,service_id,code,source_instance,"
                "target_instance,trigger_reason,policy_version,switched,started_epoch_ms,"
                "ended_epoch_ms,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.request_id,
                    decision.run_id,
                    decision.service_id,
                    decision.code,
                    decision.source_instance,
                    decision.target_instance,
                    decision.trigger_reason,
                    decision.policy_version,
                    1 if decision.switched else 0,
                    decision.started_epoch_ms,
                    decision.ended_epoch_ms,
                    json.dumps(decision.details, ensure_ascii=False, sort_keys=True),
                ),
            )

    def allocate_round(self) -> int:
        with self._lock:
            return self._next_round_unlocked()

    def _next_round_unlocked(self) -> int:
        current = int(self._meta_get("round_no") or "0") + 1
        self._meta_set("round_no", str(current))
        return current

    def begin_switch(
        self,
        *,
        request_id: str,
        run_id: str,
        service_id: str,
        source_instance: str | None,
        target_instance: str | None,
        trigger_reason: str | None,
        policy_version: str,
        code: str,
        details: dict[str, Any],
        now_epoch_ms: int,
    ) -> None:
        with self._lock:
            self._ensure_write("begin_switch")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO switch_transactions(service_id,request_id,started_epoch_ms)"
                    " VALUES(?,?,?)",
                    (service_id, request_id, now_epoch_ms),
                )
                self._conn.execute(
                    "INSERT INTO switch_decisions(request_id,run_id,service_id,code,source_instance,"
                    "target_instance,trigger_reason,policy_version,switched,started_epoch_ms,"
                    "ended_epoch_ms,details_json) VALUES(?,?,?,?,?,?,?,?,?,?,NULL,?)",
                    (
                        request_id,
                        run_id,
                        service_id,
                        code,
                        source_instance,
                        target_instance,
                        trigger_reason,
                        policy_version,
                        0,
                        now_epoch_ms,
                        json.dumps(details, ensure_ascii=False, sort_keys=True),
                    ),
                )
                self._conn.execute(
                    "UPDATE service_runtime SET switching=1 WHERE service_id=?", (service_id,)
                )
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                raise BusinessError(
                    BusinessCode.E_BUSY,
                    f"服务 {service_id} 已有进行中的切换事务",
                    details={"service_id": service_id},
                ) from exc
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def atomic_apply_roles(
        self,
        *,
        request_id: str,
        service_id: str,
        source_instance: str,
        target_instance: str,
        now_epoch_ms: int,
    ) -> tuple[int, int]:
        with self._lock:
            self._ensure_write("atomic_apply_roles")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                tx = self._conn.execute(
                    "SELECT request_id FROM switch_transactions WHERE service_id=?",
                    (service_id,),
                ).fetchone()
                if tx is None or str(tx["request_id"]) != request_id:
                    self._conn.execute("ROLLBACK")
                    raise BusinessError(
                        BusinessCode.E_BUSY,
                        f"服务 {service_id} 的切换事务已失效",
                        details={"service_id": service_id, "request_id": request_id},
                    )
                source = self._conn.execute(
                    "SELECT role,version FROM instances WHERE instance_id=? AND service_id=?",
                    (source_instance, service_id),
                ).fetchone()
                target = self._conn.execute(
                    "SELECT role,version FROM instances WHERE instance_id=? AND service_id=?",
                    (target_instance, service_id),
                ).fetchone()
                if source is None or target is None:
                    raise BusinessError(
                        BusinessCode.E_MANUAL_PENDING,
                        "切换源或目标实例不存在",
                        details={"source": source_instance, "target": target_instance},
                    )
                if source["role"] != "primary" or target["role"] != "standby":
                    raise BusinessError(
                        BusinessCode.E_BUSY,
                        "角色已变化，切换事务终止",
                        details={
                            "source_role": source["role"],
                            "target_role": target["role"],
                        },
                    )
                source_version = int(source["version"]) + 1
                target_version = int(target["version"]) + 1
                self._conn.execute(
                    "UPDATE instances SET role='standby',isolated=1,version=?,updated_at_epoch_ms=? "
                    "WHERE instance_id=?",
                    (source_version, now_epoch_ms, source_instance),
                )
                self._conn.execute(
                    "UPDATE instances SET role='primary',version=?,updated_at_epoch_ms=? "
                    "WHERE instance_id=?",
                    (target_version, now_epoch_ms, target_instance),
                )
                self._conn.execute(
                    "UPDATE service_runtime SET cooldown_started_epoch_ms=?,last_switch_target=? "
                    "WHERE service_id=?",
                    (now_epoch_ms, target_instance, service_id),
                )
                self._conn.execute(
                    "UPDATE switch_decisions SET switched=1 WHERE request_id=?", (request_id,)
                )
                self._conn.execute("COMMIT")
                return source_version, target_version
            except Exception:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def finalize_switch(
        self,
        *,
        request_id: str,
        service_id: str,
        code: str,
        details: dict[str, Any],
        ended_epoch_ms: int,
    ) -> None:
        with self._lock:
            self._ensure_write("finalize_switch")
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    "UPDATE switch_decisions SET code=?,details_json=?,ended_epoch_ms=? "
                    "WHERE request_id=?",
                    (
                        code,
                        json.dumps(details, ensure_ascii=False, sort_keys=True),
                        ended_epoch_ms,
                        request_id,
                    ),
                )
                self._conn.execute(
                    "DELETE FROM switch_transactions WHERE request_id=?", (request_id,)
                )
                self._conn.execute(
                    "UPDATE service_runtime SET switching=0 WHERE service_id=?", (service_id,)
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_switch_transactions(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM switch_transactions ORDER BY service_id"
            ).fetchall()
            return [dict(row) for row in rows]

    def service_has_transaction(self, service_id: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM switch_transactions WHERE service_id=?",
                    (service_id,),
                ).fetchone()
                is not None
            )

    def register_pending(
        self,
        *,
        request_id: str,
        service_id: str,
        requested_epoch_ms: int,
        trigger_reason: str,
        source_instance: str,
        target_instance: str | None,
        policy_version: str,
    ) -> None:
        with self._lock:
            self._ensure_write("register_pending")
            self._conn.execute(
                "INSERT INTO pending_switches(request_id,service_id,requested_epoch_ms,trigger_reason,"
                "source_instance,target_instance,policy_version,status) VALUES(?,?,?,?,?,?,?,?)",
                (
                    request_id,
                    service_id,
                    requested_epoch_ms,
                    trigger_reason,
                    source_instance,
                    target_instance,
                    policy_version,
                    "pending",
                ),
            )

    def get_pending(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pending_switches WHERE request_id=?", (request_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM pending_switches ORDER BY requested_epoch_ms,request_id"
                ).fetchall()
            ]

    def set_pending_status(self, request_id: str, status: str) -> None:
        with self._lock:
            self._ensure_write("set_pending_status")
            self._conn.execute(
                "UPDATE pending_switches SET status=? WHERE request_id=?",
                (status, request_id),
            )

    def list_state_transitions(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM state_transitions WHERE run_id=? ORDER BY id", (run_id,)
                ).fetchall()
            ]

    def list_probe_attempts(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM probe_attempts WHERE run_id=? ORDER BY id", (run_id,)
                ).fetchall()
            ]

    def list_business_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM business_events WHERE run_id=? ORDER BY id", (run_id,)
                ).fetchall()
            ]

    def list_switch_decisions(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return [
                dict(row)
                for row in self._conn.execute(
                    "SELECT * FROM switch_decisions WHERE run_id=? ORDER BY id", (run_id,)
                ).fetchall()
            ]
