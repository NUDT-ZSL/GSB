"""Persistent application service for the CLI and local dashboard."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .alerts import AlertStore
from .cache import CacheStore
from .canonical import canonical_bytes
from .config import default_config
from .engine import MonitorEngine, RunResult
from .errors import E_STATE_INVALID, AppError, config_invalid
from .lineage import replay_alert
from .models import (
    CATEGORY,
    NUMERIC,
    DriftEvent,
    FeatureSpec,
    MonitorConfig,
)
from .selfcheck import run_selfcheck


STATE_SCHEMA = "driftmon.state.v1"


@dataclass
class ServiceState:
    config: MonitorConfig
    cursor: int
    last_report: dict[str, Any] | None
    selfcheck: dict[str, Any] | None
    errors: list[dict[str, Any]]
    operations: list[dict[str, Any]]
    sequence: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": STATE_SCHEMA,
            "config": self.config.to_state(),
            "cursor": self.cursor,
            "last_report": self.last_report,
            "selfcheck": self.selfcheck,
            "errors": self.errors,
            "operations": self.operations,
            "sequence": self.sequence,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "ServiceState":
        if payload.get("schema") != STATE_SCHEMA:
            raise AppError(E_STATE_INVALID, "应用状态 schema 不匹配")
        return cls(
            config=MonitorConfig.from_state(payload["config"]),
            cursor=int(payload["cursor"]),
            last_report=payload.get("last_report"),
            selfcheck=payload.get("selfcheck"),
            errors=list(payload.get("errors", [])),
            operations=list(payload.get("operations", [])),
            sequence=int(payload.get("sequence", 0)),
        )


class MonitoringService:
    def __init__(self, root: str | Path, initial_cursor: int = 2) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        self.cache_root = self.root / "cache"
        self.alert_root = self.root / "alerts"
        self.initial_cursor = initial_cursor
        self._lock = threading.RLock()

    def _cache(self, config: MonitorConfig | None = None) -> CacheStore:
        active = config or self._state.config
        return CacheStore(
            self.cache_root,
            max_entries=active.limits.max_cache_entries,
            resource_policy=active.limits.resource_policy,
        )

    def _alerts(self) -> AlertStore:
        return AlertStore(self.alert_root)

    @property
    def _state(self) -> ServiceState:
        if not hasattr(self, "_loaded_state"):
            self._loaded_state = self._load_state()
        return self._loaded_state

    @_state.setter
    def _state(self, value: ServiceState) -> None:
        self._loaded_state = value

    def _load_state(self) -> ServiceState:
        if not self.state_path.exists():
            return self._new_state()
        try:
            payload = json.loads(self.state_path.read_bytes().decode("utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise AppError(
                E_STATE_INVALID,
                "应用状态无法读取",
                details={"path": str(self.state_path), "reason": str(exc)},
            ) from exc
        return ServiceState.from_payload(payload)

    def _new_state(self) -> ServiceState:
        config = default_config()
        cursor = max(1, min(self.initial_cursor, config.n_batches - 1))
        return ServiceState(
            config=config,
            cursor=cursor,
            last_report=None,
            selfcheck=None,
            errors=[],
            operations=[],
        )

    def _save_state(self) -> None:
        payload = canonical_bytes(self._state.to_payload())
        temporary = self.state_path.with_suffix(".tmp")
        with open(temporary, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    def _record_error(self, operation: str, error: AppError) -> None:
        self._state.sequence += 1
        error_payload = error.to_dict()
        error_payload.update(
            {"operation": operation, "sequence": self._state.sequence}
        )
        self._state.errors = (self._state.errors + [error_payload])[-20:]
        self._save_state()

    def _execute(
        self,
        operation: str,
        config: MonitorConfig,
        cursor: int,
        old_config: MonitorConfig | None,
        old_cursor: int | None,
        requested_parallel: int | None = None,
    ) -> RunResult:
        engine = MonitorEngine(config, self._cache(config), self._alerts())
        result = engine.run(
            cursor,
            previous_config=old_config,
            previous_cursor=old_cursor,
            requested_parallel=requested_parallel,
        )
        self._state.sequence += 1
        report = result.public()
        report["operation"] = {
            "name": operation,
            "sequence": self._state.sequence,
        }
        self._state.config = config
        self._state.cursor = cursor
        self._state.last_report = report
        self._state.selfcheck = None
        self._state.operations = (
            self._state.operations
            + [
                {
                    "sequence": self._state.sequence,
                    "name": operation,
                    "cursor": cursor,
                    "config_version": config.config_version,
                    "affected_features": result.affected_features,
                    "affected_batches": result.affected_batches,
                    "recomputed_count": len(result.recomputed_products),
                    "reused_count": len(result.reused_products),
                }
            ]
        )[-50:]
        self._save_state()
        return result

    def reset(self, clear: bool = True) -> dict[str, Any]:
        with self._lock:
            if clear:
                CacheStore(self.cache_root).clear()
                AlertStore(self.alert_root).clear()
            self._state = self._new_state()
            try:
                self._execute(
                    "reset",
                    self._state.config,
                    self._state.cursor,
                    None,
                    None,
                )
            except AppError as error:
                self._record_error("reset", error)
                raise
            return self.snapshot()

    def ensure_initialized(self) -> None:
        with self._lock:
            if self._state.last_report is None:
                self._execute(
                    "initialize",
                    self._state.config,
                    self._state.cursor,
                    None,
                    None,
                )

    def advance(self) -> RunResult:
        with self._lock:
            old_config = self._state.config
            old_cursor = self._state.cursor
            step = old_config.slide_step
            new_cursor = old_cursor + step
            if new_cursor >= old_config.n_batches:
                error = config_invalid(
                    "cursor",
                    "继续滑动将越过最后一批",
                    cursor=old_cursor,
                    slide_step=step,
                    n_batches=old_config.n_batches,
                )
                self._record_error("advance", error)
                raise error
            try:
                return self._execute(
                    "advance",
                    old_config,
                    new_cursor,
                    old_config,
                    old_cursor,
                )
            except AppError as error:
                self._record_error("advance", error)
                raise

    def set_window(self, window_size: int) -> RunResult:
        with self._lock:
            old_config = self._state.config
            old_cursor = self._state.cursor
            try:
                try:
                    parsed_window = int(window_size)
                except (TypeError, ValueError) as exc:
                    raise config_invalid(
                        "window_size", "参考窗口长度必须为整数"
                    ) from exc
                new_config = old_config.updated(
                    window_size=parsed_window,
                    slide_step=min(old_config.slide_step, parsed_window),
                )
                return self._execute(
                    "window_update",
                    new_config,
                    old_cursor,
                    old_config,
                    old_cursor,
                )
            except AppError as error:
                self._record_error("window_update", error)
                raise

    def bump_stat_version(self, feature_name: str) -> RunResult:
        with self._lock:
            old_config = self._state.config
            old_cursor = self._state.cursor
            try:
                features = tuple(
                    feature.with_updates(stat_version=feature.stat_version + 1)
                    if feature.name == feature_name
                    else feature
                    for feature in old_config.features
                )
                old_config.feature(feature_name)
                new_config = old_config.updated(features=features)
                return self._execute(
                    "stat_version_update",
                    new_config,
                    old_cursor,
                    old_config,
                    old_cursor,
                )
            except AppError as error:
                self._record_error("stat_version_update", error)
                raise

    def toggle_feature(self, feature_name: str) -> RunResult:
        with self._lock:
            old_config = self._state.config
            old_cursor = self._state.cursor
            try:
                current = old_config.feature(feature_name)
                features = tuple(
                    feature.with_updates(enabled=not feature.enabled)
                    if feature.name == feature_name
                    else feature
                    for feature in old_config.features
                )
                new_config = old_config.updated(features=features)
                return self._execute(
                    "feature_toggle",
                    new_config,
                    old_cursor,
                    old_config,
                    old_cursor,
                )
            except AppError as error:
                self._record_error("feature_toggle", error)
                raise

    def redefine_feature(self, feature_name: str, new_kind: str) -> RunResult:
        with self._lock:
            old_config = self._state.config
            old_cursor = self._state.cursor
            try:
                old_config.feature(feature_name)
                if new_kind not in {NUMERIC, CATEGORY}:
                    raise config_invalid(
                        "features.kind", "重定义类型只能是 numeric 或 category"
                    )
                features = []
                for feature in old_config.features:
                    if feature.name != feature_name:
                        features.append(feature)
                    elif new_kind == CATEGORY:
                        features.append(
                            FeatureSpec(
                                name=feature.name,
                                kind=CATEGORY,
                                stat_version=feature.stat_version,
                                enabled=feature.enabled,
                                levels=("high", "low", "mid"),
                                probabilities=(0.25, 0.50, 0.25),
                                description=f"{feature.name}（重定义为类别列）",
                            )
                        )
                    else:
                        features.append(
                            FeatureSpec(
                                name=feature.name,
                                kind=NUMERIC,
                                stat_version=feature.stat_version,
                                enabled=feature.enabled,
                                mean=0.5,
                                std=0.18,
                                description=f"{feature.name}（重定义为数值列）",
                            )
                        )
                if new_kind == CATEGORY:
                    incompatible = {"mean_shift", "variance_scale"}
                else:
                    incompatible = {"category_shift"}
                events = tuple(
                    event
                    for event in old_config.drift_events
                    if not (
                        event.feature == feature_name and event.kind in incompatible
                    )
                )
                new_config = old_config.updated(
                    features=tuple(features),
                    drift_events=events,
                )
                return self._execute(
                    "feature_redefine",
                    new_config,
                    old_cursor,
                    old_config,
                    old_cursor,
                )
            except AppError as error:
                self._record_error("feature_redefine", error)
                raise

    def inject_drift_and_advance(
        self,
        feature_name: str = "transaction_amount",
        kind: str = "mean_shift",
        magnitude: float = 3.5,
    ) -> RunResult:
        with self._lock:
            old_config = self._state.config
            old_cursor = self._state.cursor
            new_cursor = old_cursor + old_config.slide_step
            try:
                if new_cursor >= old_config.n_batches:
                    raise config_invalid(
                        "cursor", "没有下一批可用于注入漂移"
                    )
                old_config.feature(feature_name)
                try:
                    parsed_magnitude = float(magnitude)
                except (TypeError, ValueError) as exc:
                    raise config_invalid(
                        "drift_events.magnitude", "漂移幅度必须为数值"
                    ) from exc
                event = DriftEvent(
                    batch=new_cursor,
                    feature=feature_name,
                    kind=kind,
                    magnitude=parsed_magnitude,
                )
                events = tuple(
                    sorted(
                        existing
                        for existing in old_config.drift_events
                        if not (
                            existing.batch == new_cursor
                            and existing.feature == feature_name
                            and existing.kind == kind
                        )
                    )
                    + [event]
                )
                new_config = old_config.updated(drift_events=events)
                return self._execute(
                    "inject_drift_and_advance",
                    new_config,
                    new_cursor,
                    old_config,
                    old_cursor,
                )
            except AppError as error:
                self._record_error("inject_drift_and_advance", error)
                raise

    def selfcheck(self) -> dict[str, Any]:
        with self._lock:
            self.ensure_initialized()
            config = self._state.config
            engine = MonitorEngine(config, self._cache(config), self._alerts())
            result = engine.run(
                self._state.cursor,
                previous_config=config,
                previous_cursor=self._state.cursor,
                emit_alert=False,
            )
            report = (self._state.last_report or {}).get("incremental")
            check = run_selfcheck(
                result,
                self._cache(config),
                self._alerts(),
                incremental_report=report,
            )
            self._state.selfcheck = check
            self._save_state()
            return check

    def trace_alert(self, alert_id: str) -> dict[str, Any]:
        with self._lock:
            try:
                alert = self._alerts().get_alert(alert_id)
                return replay_alert(alert, self._cache(self._state.config))
            except AppError as error:
                self._record_error("trace_alert", error)
                raise

    def current_alert(self) -> dict[str, Any] | None:
        return self._alerts().load_current()

    def explain_cache(self, key: str) -> dict[str, Any]:
        return self._cache(self._state.config).explain(key)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self.ensure_initialized()
            alerts = self._alerts().list_alerts()
            cache = self._cache(self._state.config)
            return {
                "state": {
                    "cursor": self._state.cursor,
                    "config_version": self._state.config.config_version,
                    "seed": self._state.config.seed,
                    "n_batches": self._state.config.n_batches,
                    "batch_size": self._state.config.batch_size,
                    "window_size": self._state.config.window_size,
                    "slide_step": self._state.config.slide_step,
                    "numeric_test": self._state.config.numeric_test,
                    "numeric_threshold": self._state.config.numeric_threshold,
                    "category_threshold": self._state.config.category_threshold,
                    "features": [
                        feature.to_state() for feature in self._state.config.features
                    ],
                    "limits": self._state.config.limits.to_state(),
                },
                "last_run": self._state.last_report,
                "alerts": [
                    {
                        "alert_id": alert["alert_id"],
                        "version": alert["version"],
                        "checksum": alert["checksum"],
                        "target_batch": alert["content"]["window"]["target_batch"],
                        "summary": alert["content"]["summary"],
                        "features": [
                            {
                                "feature": feature["feature"],
                                "drift": feature["drift"],
                                "severity": feature["severity"],
                            }
                            for feature in alert["content"]["features"]
                        ],
                    }
                    for alert in alerts
                ],
                "recent_errors": self._state.errors[-10:],
                "operations": self._state.operations[-10:],
                "selfcheck": self._state.selfcheck,
                "cache": {
                    "entries": cache.count_entries(),
                    "max_entries": self._state.config.limits.max_cache_entries,
                    "root": str(self.cache_root),
                },
            }

    def dashboard_payload(self) -> dict[str, Any]:
        payload = self.snapshot()
        last = payload.get("last_run") or {}
        payload["view"] = {
            "features": last.get("results", []),
            "incremental": last.get("incremental", {}),
            "window_report": last.get("window_report", {}),
            "alert": last.get("alert"),
            "operation": last.get("operation"),
            "snapshot_sha256": last.get("snapshot_sha256"),
        }
        return payload

