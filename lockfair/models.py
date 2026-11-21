"""Bundle parsing and validation.

Validation is intentionally side-effect free. Callers can validate a bundle
without opening or creating a SQLite database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError, ErrorCode, error


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise error(ErrorCode.INVALID_INPUT, f"{path} 必须是对象", details={"path": path})
    return value


def _require_list(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise error(ErrorCode.INVALID_INPUT, f"{path} 必须是数组", details={"path": path})
    return value


def _required_string(obj: dict[str, Any], key: str, path: str) -> str:
    if key not in obj:
        raise error(ErrorCode.INVALID_INPUT, f"{path}.{key} 缺失", details={"path": f"{path}.{key}"})
    value = obj[key]
    if not isinstance(value, str) or not value.strip():
        raise error(ErrorCode.INVALID_INPUT, f"{path}.{key} 必须是非空字符串", details={"path": f"{path}.{key}"})
    return value.strip()


def _required_int(obj: dict[str, Any], key: str, path: str) -> int:
    if key not in obj:
        raise error(ErrorCode.INVALID_INPUT, f"{path}.{key} 缺失", details={"path": f"{path}.{key}"})
    value = obj[key]
    if not _is_int(value):
        raise error(ErrorCode.INVALID_INPUT, f"{path}.{key} 必须是整数", details={"path": f"{path}.{key}"})
    return value


@dataclass(frozen=True, slots=True)
class LockResource:
    lock_key: str
    lease_duration_ms: int
    lease_generation: int
    priority: int
    max_wait_rounds: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "lock_key": self.lock_key,
            "lease_duration_ms": self.lease_duration_ms,
            "lease_generation": self.lease_generation,
            "priority": self.priority,
            "max_wait_rounds": self.max_wait_rounds,
        }


@dataclass(frozen=True, slots=True)
class Holder:
    identity: str
    heartbeat_ms: int
    session_generation: int
    max_concurrent_grants: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity,
            "heartbeat_ms": self.heartbeat_ms,
            "session_generation": self.session_generation,
            "max_concurrent_grants": self.max_concurrent_grants,
        }


@dataclass(frozen=True, slots=True)
class LockApplication:
    seq: int
    holder: str
    lock_key: str
    arrival_ms: int
    desired_hold_ms: int
    fairness_label: str
    priority: int | None = None
    expected_lease_generation: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "holder": self.holder,
            "lock_key": self.lock_key,
            "arrival_ms": self.arrival_ms,
            "desired_hold_ms": self.desired_hold_ms,
            "fairness_label": self.fairness_label,
            "priority": self.priority,
            "expected_lease_generation": self.expected_lease_generation,
        }


@dataclass(frozen=True, slots=True)
class Config:
    round_budget_ms: int
    fair_quota_min_grants: int
    starvation_rounds: int
    clock_step_ms: int
    fairness_labels: tuple[str, ...]
    initial_clock_ms: int = 0
    max_forced_yields_per_application: int = 1
    clock_rollback_tolerance_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_budget_ms": self.round_budget_ms,
            "fair_quota_min_grants": self.fair_quota_min_grants,
            "starvation_rounds": self.starvation_rounds,
            "clock_step_ms": self.clock_step_ms,
            "fairness_labels": list(self.fairness_labels),
            "initial_clock_ms": self.initial_clock_ms,
            "max_forced_yields_per_application": self.max_forced_yields_per_application,
            "clock_rollback_tolerance_ms": self.clock_rollback_tolerance_ms,
        }


@dataclass(slots=True)
class Bundle:
    resources: dict[str, LockResource]
    holders: dict[str, Holder]
    applications: dict[int, LockApplication]
    config: Config
    source: str = "<memory>"
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources": [item.to_dict() for item in self.resources.values()],
            "holders": [item.to_dict() for item in self.holders.values()],
            "applications": [self.applications[seq].to_dict() for seq in sorted(self.applications)],
            "config": self.config.to_dict(),
        }

    def canonical_hash(self) -> str:
        import hashlib

        payload = json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_bundle(path: str | Path) -> Bundle:
    file_path = Path(path)
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise error(ErrorCode.INVALID_INPUT, f"样本文件不存在: {file_path}", details={"path": str(file_path)}) from exc
    except json.JSONDecodeError as exc:
        raise error(
            ErrorCode.INVALID_INPUT,
            f"样本 JSON 无法解析: {exc.msg}",
            details={"path": str(file_path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    bundle = parse_bundle(raw)
    bundle.source = str(file_path)
    return bundle


def parse_bundle(raw: Any) -> Bundle:
    root = _require_object(raw, "$")

    config_raw = _require_object(root.get("config"), "$.config")
    labels_raw = _require_list(config_raw.get("fairness_labels"), "$.config.fairness_labels")
    labels: list[str] = []
    for index, label in enumerate(labels_raw):
        if not isinstance(label, str) or not label.strip():
            raise error(
                ErrorCode.INVALID_FAIRNESS_LABEL,
                "公平配额标签必须是非空字符串",
                constraint="fairness_label",
                details={"path": f"$.config.fairness_labels[{index}]"},
            )
        normalized = label.strip()
        if normalized in labels:
            raise error(
                ErrorCode.INVALID_FAIRNESS_LABEL,
                f"公平配额标签重复: {normalized}",
                constraint="fairness_label",
                details={"label": normalized},
            )
        labels.append(normalized)
    if not labels:
        raise error(
            ErrorCode.INVALID_FAIRNESS_LABEL,
            "公平配额标签枚举不能为空",
            constraint="fairness_label",
        )

    budget = _required_int(config_raw, "round_budget_ms", "$.config")
    if budget <= 0:
        raise error(
            ErrorCode.INVALID_ROUND_BUDGET,
            "每轮总逻辑时间预算必须是正整数",
            constraint="round_budget",
            details={"round_budget_ms": budget},
        )
    quota = _required_int(config_raw, "fair_quota_min_grants", "$.config")
    if quota < 0:
        raise error(
            ErrorCode.INVALID_CONFIG,
            "公平配额每轮最少授予数必须是非负整数",
            constraint="fair_quota",
            details={"fair_quota_min_grants": quota},
        )
    starvation = _required_int(config_raw, "starvation_rounds", "$.config")
    if starvation <= 0:
        raise error(
            ErrorCode.INVALID_STARVATION_THRESHOLD,
            "饥饿判定轮数阈值必须是正整数",
            constraint="starvation_rounds",
            details={"starvation_rounds": starvation},
        )
    step = _required_int(config_raw, "clock_step_ms", "$.config")
    if step <= 0:
        raise error(
            ErrorCode.INVALID_CONFIG,
            "逻辑时钟步进必须是正整数",
            constraint="clock_step",
            details={"clock_step_ms": step},
        )
    initial_clock = config_raw.get("initial_clock_ms", 0)
    if not _is_int(initial_clock) or initial_clock < 0:
        raise error(
            ErrorCode.INVALID_CONFIG,
            "初始逻辑时钟必须是非负整数",
            constraint="logical_clock",
            details={"initial_clock_ms": initial_clock},
        )
    max_forced = config_raw.get("max_forced_yields_per_application", 1)
    if not _is_int(max_forced) or max_forced < 0:
        raise error(
            ErrorCode.INVALID_CONFIG,
            "单申请强制让渡上限必须是非负整数",
            constraint="forced_yield",
            details={"max_forced_yields_per_application": max_forced},
        )
    rollback_tolerance = config_raw.get("clock_rollback_tolerance_ms", 0)
    if not _is_int(rollback_tolerance) or rollback_tolerance < 0:
        raise error(
            ErrorCode.INVALID_CONFIG,
            "逻辑时钟回拨容差必须是非负整数",
            constraint="logical_clock",
            details={"clock_rollback_tolerance_ms": rollback_tolerance},
        )

    resources: dict[str, LockResource] = {}
    for index, item in enumerate(_require_list(root.get("resources"), "$.resources")):
        path = f"$.resources[{index}]"
        obj = _require_object(item, path)
        key = _required_string(obj, "lock_key", path)
        if key in resources:
            raise error(
                ErrorCode.DUPLICATE_RESOURCE_KEY,
                f"资源键重复: {key}",
                lock_key=key,
                details={"lock_key": key, "index": index},
            )
        duration = _required_int(obj, "lease_duration_ms", path)
        if duration <= 0:
            raise error(
                ErrorCode.INVALID_LEASE_DURATION,
                f"锁 {key} 的租约时长必须是正整数毫秒",
                lock_key=key,
                constraint="lease_duration",
                details={"lease_duration_ms": duration},
            )
        generation = _required_int(obj, "lease_generation", path)
        if generation < 0:
            raise error(
                ErrorCode.INVALID_LEASE_GENERATION,
                f"锁 {key} 的租约代数必须是非负整数",
                lock_key=key,
                constraint="lease_generation",
                details={"lease_generation": generation},
            )
        priority = _required_int(obj, "priority", path)
        max_wait = _required_int(obj, "max_wait_rounds", path)
        if max_wait <= 0:
            raise error(
                ErrorCode.INVALID_STARVATION_THRESHOLD,
                f"锁 {key} 的最大等待轮数上限必须是正整数",
                lock_key=key,
                constraint="max_wait_rounds",
                details={"max_wait_rounds": max_wait},
            )
        resources[key] = LockResource(key, duration, generation, priority, max_wait)

    holders: dict[str, Holder] = {}
    for index, item in enumerate(_require_list(root.get("holders"), "$.holders")):
        path = f"$.holders[{index}]"
        obj = _require_object(item, path)
        identity = _required_string(obj, "identity", path)
        if identity in holders:
            raise error(
                ErrorCode.DUPLICATE_HOLDER_IDENTITY,
                f"持有者身份重复: {identity}",
                details={"identity": identity, "index": index},
            )
        heartbeat = _required_int(obj, "heartbeat_ms", path)
        session_generation = _required_int(obj, "session_generation", path)
        limit = _required_int(obj, "max_concurrent_grants", path)
        if limit <= 0:
            raise error(
                ErrorCode.INVALID_CONCURRENCY_LIMIT,
                f"持有者 {identity} 的并发授予上限必须是正整数",
                constraint="holder_cap",
                details={"identity": identity, "max_concurrent_grants": limit},
            )
        if heartbeat <= 0 or session_generation < 0:
            raise error(
                ErrorCode.INVALID_CONFIG,
                f"持有者 {identity} 的心跳周期必须为正、会话代数必须非负",
                constraint="holder",
                details={"identity": identity},
            )
        holders[identity] = Holder(identity, heartbeat, session_generation, limit)

    applications: dict[int, LockApplication] = {}
    for index, item in enumerate(_require_list(root.get("applications"), "$.applications")):
        path = f"$.applications[{index}]"
        obj = _require_object(item, path)
        seq = _required_int(obj, "seq", path)
        if seq <= 0:
            raise error(
                ErrorCode.INVALID_INPUT,
                "申请序号必须是正整数",
                application_seq=seq,
                details={"path": f"{path}.seq"},
            )
        if seq in applications:
            raise error(
                ErrorCode.DUPLICATE_APPLICATION_SEQ,
                f"申请序号重复: {seq}",
                application_seq=seq,
                details={"application_seq": seq, "index": index},
            )
        holder = _required_string(obj, "holder", path)
        if holder not in holders:
            raise error(
                ErrorCode.HOLDER_NOT_FOUND,
                f"申请 {seq} 的持有者不存在: {holder}",
                application_seq=seq,
                constraint="holder",
                details={"holder": holder},
            )
        lock_key = _required_string(obj, "lock_key", path)
        if lock_key not in resources:
            raise error(
                ErrorCode.LOCK_NOT_FOUND,
                f"申请 {seq} 的目标锁键不存在: {lock_key}",
                lock_key=lock_key,
                application_seq=seq,
                constraint="lock_key",
                details={"lock_key": lock_key},
            )
        arrival = _required_int(obj, "logical_clock_ms", path)
        if arrival < 0:
            raise error(
                ErrorCode.INVALID_INPUT,
                "申请逻辑时钟必须是非负整数",
                lock_key=lock_key,
                application_seq=seq,
                details={"logical_clock_ms": arrival},
            )
        desired = _required_int(obj, "desired_hold_ms", path)
        if desired <= 0:
            raise error(
                ErrorCode.INVALID_INPUT,
                "期望持有逻辑时长必须是正整数毫秒",
                lock_key=lock_key,
                application_seq=seq,
                constraint="desired_hold",
                details={"desired_hold_ms": desired},
            )
        label = _required_string(obj, "fairness_label", path)
        if label not in labels:
            raise error(
                ErrorCode.INVALID_FAIRNESS_LABEL,
                f"申请 {seq} 的公平配额标签不在闭合枚举中: {label}",
                lock_key=lock_key,
                application_seq=seq,
                constraint="fairness_label",
                details={"fairness_label": label, "allowed": labels},
            )
        priority = obj.get("priority", resources[lock_key].priority)
        if not _is_int(priority):
            raise error(
                ErrorCode.INVALID_INPUT,
                "申请优先级必须是整数",
                lock_key=lock_key,
                application_seq=seq,
                details={"priority": priority},
            )
        expected_generation = obj.get("expected_lease_generation")
        if expected_generation is not None:
            if not _is_int(expected_generation) or expected_generation < 0:
                raise error(
                    ErrorCode.INVALID_LEASE_GENERATION,
                    "申请期望租约代数必须是非负整数",
                    lock_key=lock_key,
                    application_seq=seq,
                    constraint="lease_generation",
                    details={"expected_lease_generation": expected_generation},
                )
            if expected_generation > resources[lock_key].lease_generation:
                raise error(
                    ErrorCode.INVALID_LEASE_GENERATION,
                    "申请期望租约代数不能领先于资源当前代数",
                    lock_key=lock_key,
                    application_seq=seq,
                    constraint="lease_generation",
                    details={
                        "expected_lease_generation": expected_generation,
                        "current_lease_generation": resources[lock_key].lease_generation,
                    },
                )
        applications[seq] = LockApplication(
            seq=seq,
            holder=holder,
            lock_key=lock_key,
            arrival_ms=arrival,
            desired_hold_ms=desired,
            fairness_label=label,
            priority=priority,
            expected_lease_generation=expected_generation,
        )

    if not resources:
        raise error(ErrorCode.INVALID_INPUT, "锁资源清单不能为空", details={"path": "$.resources"})
    if not holders:
        raise error(ErrorCode.INVALID_INPUT, "持有者清单不能为空", details={"path": "$.holders"})

    for key, resource in resources.items():
        if starvation > resource.max_wait_rounds:
            raise error(
                ErrorCode.INVALID_CONFIG,
                f"饥饿阈值 {starvation} 超过锁 {key} 的最大等待轮数 {resource.max_wait_rounds}",
                lock_key=key,
                constraint="starvation_rounds",
                details={"starvation_rounds": starvation, "max_wait_rounds": resource.max_wait_rounds},
            )

    min_cost = min(
        min(app.desired_hold_ms, resources[app.lock_key].lease_duration_ms)
        for app in applications.values()
    ) if applications else 1
    if quota > 0 and quota * min_cost > budget:
        raise error(
            ErrorCode.FAIR_QUOTA_UNSATISFIABLE,
            "公平配额配置不可满足：最小授予成本乘以每轮最少授予数超过轮预算",
            constraint="fair_quota",
            details={
                "fair_quota_min_grants": quota,
                "min_grant_cost_ms": min_cost,
                "round_budget_ms": budget,
            },
        )

    config = Config(
        round_budget_ms=budget,
        fair_quota_min_grants=quota,
        starvation_rounds=starvation,
        clock_step_ms=step,
        fairness_labels=tuple(labels),
        initial_clock_ms=initial_clock,
        max_forced_yields_per_application=max_forced,
        clock_rollback_tolerance_ms=rollback_tolerance,
    )
    return Bundle(resources=resources, holders=holders, applications=applications, config=config, raw=root)


def validate_bundle_file(path: str | Path) -> Bundle:
    return load_bundle(path)


def format_validation_failure(exc: BusinessError) -> str:
    payload = exc.to_dict()
    return json.dumps(payload, ensure_ascii=False, indent=2)
