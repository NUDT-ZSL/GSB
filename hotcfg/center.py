"""配置中心: 版本链, 两阶段原子提交, 订阅注册与热更新推送。"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from dataclasses import dataclass

from .errors import BizError, E_COMMIT, E_PUSH, E_SUB, E_VERSION
from .jlog import JsonlLogger, MemoryLogger
from .latency import LatencyObserver, KIND_PUSH, KIND_QUERY
from .loader import ConfigItem


@dataclass(frozen=True)
class VersionRecord:
    """不可变版本记录: 变更键集合 + 完整生效值集合 + 父版本号。"""

    version: int
    parent: int | None
    changed_keys: tuple
    effective: dict  # key -> {"value":.., "weight":.., "type":..}

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "parent": self.parent,
            "changed_keys": list(self.changed_keys),
            "effective": self.effective,
        }

    @staticmethod
    def from_dict(doc: dict) -> "VersionRecord":
        return VersionRecord(
            version=doc["version"],
            parent=doc["parent"],
            changed_keys=tuple(doc["changed_keys"]),
            effective=doc["effective"],
        )


def _canonical_chain(records: list[VersionRecord]) -> str:
    payload = [r.to_dict() for r in records]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


class ConfigCenter:
    """配置中心。版本号单调递增; 提交分暂存与生效两阶段。"""

    def __init__(self, items: dict[str, ConfigItem], logger=None, clock=None):
        if not items:
            raise BizError(E_COMMIT, "config center requires at least one item")
        self._items = dict(items)
        self._lock = threading.RLock()
        self._log = logger if logger is not None else MemoryLogger()
        self.observer = LatencyObserver(clock=clock) if clock else LatencyObserver()
        base = {
            k: {"value": it.value, "weight": it.weight, "type": it.type}
            for k, it in self._items.items()
        }
        self._versions: dict[int, VersionRecord] = {
            1: VersionRecord(1, None, tuple(sorted(base)), copy.deepcopy(base))
        }
        self._current = 1
        self._pending: VersionRecord | None = None
        self._subs: dict[str, dict] = {}
        self._handlers: dict[str, object] = {}
        self._acks: list[dict] = []
        self.push_errors: list[dict] = []
        # 异常注入开关
        self.fail_push_once: set[str] = set()
        self.slow_push_ms: dict[str, float] = {}
        self.ack_timeout_ms: float | None = None

    # ---------- 版本链 ----------
    @property
    def current_version(self) -> int:
        with self._lock:
            return self._current

    def records(self) -> list[VersionRecord]:
        with self._lock:
            return [self._versions[v] for v in sorted(self._versions)]

    def chain_digest(self) -> str:
        return hashlib.sha256(_canonical_chain(self.records()).encode("utf-8")).hexdigest()

    def stage(self, changes: dict) -> None:
        """暂存阶段: 校验变更并构造待生效版本记录。"""
        if not isinstance(changes, dict) or not changes:
            raise BizError(E_COMMIT, "changes must be a non-empty object")
        with self._lock:
            base = self._versions[self._current].effective
            new_eff = copy.deepcopy(base)
            for key, value in changes.items():
                item = self._items.get(key)
                if item is None:
                    raise BizError(E_COMMIT, f"unknown config key: {key}",
                                   {"key": key, "stage": "stage"})
                if not item.check_value(value):
                    raise BizError(E_COMMIT, f"value {value!r} violates type {item.type} of {key}",
                                   {"key": key, "value": value, "stage": "stage"})
                new_eff[key] = {"value": value, "weight": item.weight, "type": item.type}
            self._pending = VersionRecord(
                version=self._current + 1,
                parent=self._current,
                changed_keys=tuple(sorted(changes)),
                effective=new_eff,
            )

    def commit(self) -> int:
        """生效阶段: 原子切换当前版本, 随后按订阅关系推送。"""
        with self._lock:
            if self._pending is None:
                raise BizError(E_COMMIT, "no staged changes to commit",
                               {"stage": "commit", "current_version": self._current})
            record = self._pending
            self._pending = None
            self._versions[record.version] = record
            self._current = record.version
        self._log.emit("commit", version=record.version,
                       parent=record.parent, changed_keys=list(record.changed_keys))
        self._push(record)
        return record.version

    def commit_changes(self, changes: dict) -> int:
        self.stage(changes)
        return self.commit()

    def get_effective(self, version: int | None = None, keys=None) -> dict:
        """一次原子读取某版本的完整生效值集合 (深拷贝, 绝不出现混合状态)。"""
        with self._lock:
            v = self._current if version is None else version
            record = self._versions.get(v)
            if record is None:
                raise BizError(E_VERSION, f"unknown version: {v}",
                               {"version": v, "current_version": self._current})
            eff = copy.deepcopy(record.effective)
        if keys is not None:
            missing = [k for k in keys if k not in eff]
            if missing:
                raise BizError(E_VERSION, f"keys not in version {v}: {missing}",
                               {"version": v, "missing": missing})
            eff = {k: eff[k] for k in keys}
        return eff

    def query(self, task_id: str, version: int | None = None, keys=None) -> dict:
        """带延迟观测的查询。"""
        v = version if version is not None else self.current_version
        with self.observer.measure(task_id, v, KIND_QUERY):
            return self.get_effective(v, keys)

    # ---------- 订阅与推送 ----------
    def subscribe(self, task_id: str, keys, expected_version: int, handler=None) -> None:
        unknown = [k for k in keys if k not in self._items]
        if unknown:
            raise BizError(E_SUB, f"subscription references unknown config keys: {unknown}",
                           {"task_id": task_id, "unknown_keys": unknown})
        with self._lock:
            self._subs[task_id] = {
                "keys": sorted(keys),
                "expected_version": expected_version,
            }
            if handler is not None:
                self._handlers[task_id] = handler
        self._log.emit("subscribe", task_id=task_id, keys=sorted(keys),
                       expected_version=expected_version)

    def update_subscription(self, task_id: str, expected_version: int) -> None:
        with self._lock:
            if task_id not in self._subs:
                raise BizError(E_SUB, f"task not subscribed: {task_id}",
                               {"task_id": task_id})
            self._subs[task_id]["expected_version"] = expected_version

    def subscriptions(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._subs)

    @property
    def acks(self) -> list[dict]:
        with self._lock:
            return list(self._acks)

    def _push(self, record: VersionRecord) -> None:
        with self._lock:
            targets = [
                tid for tid, sub in self._subs.items()
                if set(sub["keys"]) & set(record.changed_keys)
            ]
        for tid in targets:
            self._deliver(tid, record)

    def _deliver(self, task_id: str, record: VersionRecord) -> None:
        start = self.observer.now_ms()
        try:
            if task_id in self.fail_push_once:
                self.fail_push_once.discard(task_id)
                raise BizError(E_PUSH, "injected push failure",
                               {"task_id": task_id, "version": record.version})
            slow = self.slow_push_ms.get(task_id, 0.0)
            if slow:
                time.sleep(slow / 1000.0)
            if self.ack_timeout_ms is not None:
                elapsed = self.observer.now_ms() - start
                if elapsed > self.ack_timeout_ms:
                    raise BizError(E_PUSH, "push ack timeout",
                                   {"task_id": task_id, "version": record.version,
                                    "elapsed_ms": elapsed,
                                    "ack_timeout_ms": self.ack_timeout_ms})
            notification = {
                "version": record.version,
                "changed_keys": list(record.changed_keys),
            }
            handler = self._handlers.get(task_id)
            if handler is not None:
                handler.on_push(self, notification)
        except BizError as exc:
            end = self.observer.now_ms()
            err = {"task_id": task_id, "version": record.version,
                   "error": exc.to_dict()}
            with self._lock:
                self.push_errors.append(err)
            self._log.emit("push_error", task_id=task_id, version=record.version,
                           code=exc.code, message=exc.message,
                           latency_ms=end - start)
            return
        end = self.observer.now_ms()
        self.observer.record(task_id, record.version, KIND_PUSH, start, end)
        with self._lock:
            self._acks.append({
                "task_id": task_id,
                "version": record.version,
                "changed_keys": list(record.changed_keys),
                "start_ms": start,
                "end_ms": end,
            })
        self._log.emit("push_ack", task_id=task_id, version=record.version,
                       changed_keys=list(record.changed_keys),
                       latency_ms=end - start)

    def retry_push(self, task_id: str) -> None:
        """重试推送: 只投递当前最新已提交版本, 不暴露中间版本。"""
        with self._lock:
            if task_id not in self._subs:
                raise BizError(E_SUB, f"task not subscribed: {task_id}",
                               {"task_id": task_id})
            record = self._versions[self._current]
        self._log.emit("push_retry", task_id=task_id, version=record.version)
        self._deliver(task_id, record)

    # ---------- 快照支撑 ----------
    def export_state(self) -> dict:
        return {
            "current_version": self.current_version,
            "chain_digest": self.chain_digest(),
            "versions": [r.to_dict() for r in self.records()],
            "subscriptions": self.subscriptions(),
            "push_acks": self.acks,
            "latency_samples": self.observer.samples_json(),
        }

    @classmethod
    def from_state(cls, state: dict, logger=None) -> "ConfigCenter":
        versions = [VersionRecord.from_dict(v) for v in state["versions"]]
        if not versions:
            raise BizError(E_VERSION, "snapshot contains no versions")
        base_eff = versions[0].effective
        items = {
            k: ConfigItem(key=k, type=entry["type"], value=entry["value"],
                          weight=entry["weight"])
            for k, entry in base_eff.items()
        }
        center = cls(items, logger=logger)
        with center._lock:
            center._versions = {r.version: r for r in versions}
            center._current = state["current_version"]
            center._subs = copy.deepcopy(state.get("subscriptions", {}))
            center._acks = copy.deepcopy(state.get("push_acks", []))
        center.observer.load_samples(state.get("latency_samples", []))
        return center
