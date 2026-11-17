"""Strict scene loading and validation with line/field diagnostics."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterable

from .codes import BusinessCode
from .errors import BusinessError, ValidationIssue
from .models import (
    VALID_FAULT_KINDS,
    VALID_MODES,
    VALID_ROLES,
    FaultSpec,
    InstanceSpec,
    PolicySpec,
    ProbeConfig,
    ProbeLimits,
    Scene,
    ServiceSpec,
    compute_scene_fingerprint,
)

TOP_LEVEL_KEYS = {"scene_version", "probe_limits", "services", "faults"}
SERVICE_KEYS = {"service_id", "policy", "instances"}
POLICY_KEYS = {"mode", "cooldown_ms", "min_healthy_replicas", "version"}
INSTANCE_KEYS = {"instance_id", "role", "port", "probe"}
PROBE_KEYS = {"timeout_ms", "retries", "success_threshold", "failure_threshold"}
LIMIT_KEYS = {"global_concurrency", "service_quota"}
FAULT_KEYS = {"instance_id", "kind", "note"}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class _LineLocator:
    """Best-effort JSON path locator that keeps diagnostics actionable.

    JSON's standard decoder intentionally does not expose source spans.  The
    locator therefore searches the key and, when an id is available, narrows
    the search to the object containing that id.  Structural decode errors use
    the decoder's own line number.
    """

    def __init__(self, text: str) -> None:
        self.lines = text.splitlines()

    def line_for(self, field_name: str, hint: str | None = None) -> int:
        if not self.lines:
            return 1
        hint_line = 1
        if hint:
            quoted = json.dumps(str(hint), ensure_ascii=False)
            for number, line in enumerate(self.lines, start=1):
                if quoted in line:
                    hint_line = number
                    break
        key_pattern = re.compile(r'"' + re.escape(field_name) + r'"\s*:')
        candidates = [
            number
            for number, line in enumerate(self.lines, start=1)
            if key_pattern.search(line)
        ]
        if not candidates:
            return max(1, hint_line)
        after = [number for number in candidates if number >= hint_line]
        return after[0] if after else candidates[0]


class SceneLoader:
    def load(self, path: str | Path, *, inject_corruption: bool = False) -> Scene:
        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8")
        except OSError as exc:
            raise BusinessError(
                BusinessCode.E_SCENE,
                f"line 1 field $: 无法读取场景文件: {source}",
                details={"path": str(source), "error": str(exc)},
                issues=[ValidationIssue(1, "$", f"无法读取场景文件: {exc}")],
            ) from exc
        if inject_corruption:
            raise BusinessError(
                BusinessCode.E_SCENE,
                "line 1 field $: 注入的场景损坏（--inject scene-corruption）",
                details={"path": str(source), "injected": "scene-corruption"},
                issues=[ValidationIssue(1, "$", "注入的场景损坏")],
            )
        return self.loads(text, source_name=str(source))

    def loads(self, text: str, *, source_name: str = "<memory>") -> Scene:
        locator = _LineLocator(text)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BusinessError(
                BusinessCode.E_SCENE,
                f"{source_name}: JSON 解析失败，line {exc.lineno} field $: {exc.msg}",
                details={"path": source_name, "line": exc.lineno, "column": exc.colno},
                issues=[ValidationIssue(exc.lineno, "$", exc.msg)],
            ) from exc
        return self.from_dict(data, locator=locator, raw_text=text, source_name=source_name)

    def from_dict(
        self,
        data: Any,
        *,
        locator: _LineLocator | None = None,
        raw_text: str = "",
        source_name: str = "<memory>",
    ) -> Scene:
        locator = locator or _LineLocator(raw_text)
        issues: list[ValidationIssue] = []

        def issue(field_name: str, message: str, hint: str | None = None, line: int | None = None) -> None:
            issues.append(
                ValidationIssue(
                    line=line or locator.line_for(field_name.split(".")[-1], hint),
                    field=field_name,
                    message=message,
                )
            )

        if not isinstance(data, dict):
            issue("$", "场景根节点必须是 JSON 对象", line=1)
            self._raise(issues, source_name)

        for key in sorted(set(data) - TOP_LEVEL_KEYS):
            issue(key, f"未知顶层字段 {key!r}")

        version = data.get("scene_version")
        if not isinstance(version, str) or not version.strip():
            issue("scene_version", "必须是非空字符串")

        limits_data = data.get("probe_limits")
        limits: ProbeLimits | None = None
        if not isinstance(limits_data, dict):
            issue("probe_limits", "必须是对象")
        else:
            for key in sorted(set(limits_data) - LIMIT_KEYS):
                issue(f"probe_limits.{key}", f"未知字段 {key!r}")
            global_concurrency = limits_data.get("global_concurrency")
            service_quota = limits_data.get("service_quota")
            if not _is_int(global_concurrency) or global_concurrency <= 0:
                issue("probe_limits.global_concurrency", "必须是正整数")
            if not _is_int(service_quota) or service_quota <= 0:
                issue("probe_limits.service_quota", "必须是正整数")
            if _is_int(global_concurrency) and _is_int(service_quota):
                limits = ProbeLimits(global_concurrency, service_quota)

        services_raw = data.get("services")
        service_specs: list[ServiceSpec] = []
        known_instance_ids: dict[str, int] = {}
        known_ports: dict[int, str] = {}
        if not isinstance(services_raw, list):
            issue("services", "必须是数组")
            services_raw = []
        elif not services_raw:
            issue("services", "至少需要一个服务")

        service_ids: set[str] = set()
        for service_index, service_data in enumerate(services_raw):
            base = f"services[{service_index}]"
            if not isinstance(service_data, dict):
                issue(base, "服务必须是对象")
                continue
            for key in sorted(set(service_data) - SERVICE_KEYS):
                issue(f"{base}.{key}", f"未知字段 {key!r}")
            service_id = service_data.get("service_id")
            if not isinstance(service_id, str) or not service_id.strip():
                issue(f"{base}.service_id", "必须是非空字符串")
                service_id = f"<service-{service_index}>"
            elif service_id in service_ids:
                issue(f"{base}.service_id", f"服务标识重复: {service_id}", hint=service_id)
            service_ids.add(service_id)

            policy_data = service_data.get("policy")
            policy: PolicySpec | None = None
            if not isinstance(policy_data, dict):
                issue(f"{base}.policy", "必须是对象", hint=service_id)
            else:
                for key in sorted(set(policy_data) - POLICY_KEYS):
                    issue(f"{base}.policy.{key}", f"未知字段 {key!r}", hint=service_id)
                mode = policy_data.get("mode")
                cooldown_ms = policy_data.get("cooldown_ms")
                min_healthy = policy_data.get("min_healthy_replicas")
                policy_version = policy_data.get("version")
                if mode not in VALID_MODES:
                    issue(f"{base}.policy.mode", "必须是 auto 或 manual", hint=service_id)
                if not _is_int(cooldown_ms) or cooldown_ms <= 0:
                    issue(f"{base}.policy.cooldown_ms", "必须是正整数", hint=service_id)
                if not _is_int(min_healthy) or min_healthy <= 0:
                    issue(
                        f"{base}.policy.min_healthy_replicas",
                        "必须是正整数",
                        hint=service_id,
                    )
                if not isinstance(policy_version, str) or not policy_version.strip():
                    issue(f"{base}.policy.version", "必须是非空字符串", hint=service_id)
                if mode in VALID_MODES and _is_int(cooldown_ms) and cooldown_ms > 0 and _is_int(
                    min_healthy
                ) and min_healthy > 0 and isinstance(policy_version, str) and policy_version.strip():
                    policy = PolicySpec(service_id, mode, cooldown_ms, min_healthy, policy_version)

            instances_raw = service_data.get("instances")
            instances: list[InstanceSpec] = []
            if not isinstance(instances_raw, list) or not instances_raw:
                issue(f"{base}.instances", "必须是非空数组", hint=service_id)
                instances_raw = []
            primary_count = 0
            for instance_index, instance_data in enumerate(instances_raw):
                ibase = f"{base}.instances[{instance_index}]"
                if not isinstance(instance_data, dict):
                    issue(ibase, "实例必须是对象", hint=service_id)
                    continue
                for key in sorted(set(instance_data) - INSTANCE_KEYS):
                    issue(f"{ibase}.{key}", f"未知字段 {key!r}", hint=service_id)
                instance_id = instance_data.get("instance_id")
                if not isinstance(instance_id, str) or not instance_id.strip():
                    issue(f"{ibase}.instance_id", "必须是非空字符串", hint=service_id)
                    instance_id = f"<instance-{service_index}-{instance_index}>"
                elif instance_id in known_instance_ids:
                    issue(
                        f"{ibase}.instance_id",
                        f"实例标识全局重复: {instance_id}",
                        hint=instance_id,
                    )
                known_instance_ids[instance_id] = instance_index
                role = instance_data.get("role")
                if role not in VALID_ROLES:
                    issue(f"{ibase}.role", "必须是 primary 或 standby", hint=instance_id)
                elif role == "primary":
                    primary_count += 1
                port = instance_data.get("port")
                if not _is_int(port) or port < 1 or port > 65535:
                    issue(f"{ibase}.port", "必须是 1 至 65535 的整数", hint=instance_id)
                elif port in known_ports:
                    issue(
                        f"{ibase}.port",
                        f"端口全局重复: {port} (已用于 {known_ports[port]})",
                        hint=instance_id,
                    )
                else:
                    known_ports[port] = instance_id
                probe_data = instance_data.get("probe")
                probe: ProbeConfig | None = None
                if not isinstance(probe_data, dict):
                    issue(f"{ibase}.probe", "必须是对象", hint=instance_id)
                else:
                    for key in sorted(set(probe_data) - PROBE_KEYS):
                        issue(f"{ibase}.probe.{key}", f"未知字段 {key!r}", hint=instance_id)
                    timeout_ms = probe_data.get("timeout_ms")
                    retries = probe_data.get("retries")
                    success_threshold = probe_data.get("success_threshold")
                    failure_threshold = probe_data.get("failure_threshold")
                    if not _is_int(timeout_ms) or timeout_ms <= 0:
                        issue(f"{ibase}.probe.timeout_ms", "必须是正整数", hint=instance_id)
                    if not _is_int(retries) or retries < 0:
                        issue(f"{ibase}.probe.retries", "必须是大于等于 0 的整数", hint=instance_id)
                    if not _is_int(success_threshold) or success_threshold <= 0:
                        issue(
                            f"{ibase}.probe.success_threshold",
                            "必须是正整数",
                            hint=instance_id,
                        )
                    if not _is_int(failure_threshold) or failure_threshold <= 0:
                        issue(
                            f"{ibase}.probe.failure_threshold",
                            "必须是正整数",
                            hint=instance_id,
                        )
                    if (
                        _is_int(timeout_ms)
                        and timeout_ms > 0
                        and _is_int(retries)
                        and retries >= 0
                        and _is_int(success_threshold)
                        and success_threshold > 0
                        and _is_int(failure_threshold)
                        and failure_threshold > 0
                    ):
                        probe = ProbeConfig(
                            timeout_ms, retries, success_threshold, failure_threshold
                        )
                if (
                    isinstance(instance_id, str)
                    and instance_id
                    and role in VALID_ROLES
                    and _is_int(port)
                    and 1 <= port <= 65535
                    and probe is not None
                ):
                    instances.append(InstanceSpec(service_id, instance_id, role, port, probe))

            if primary_count != 1:
                issue(
                    f"{base}.instances",
                    f"每个服务必须恰有一个主实例，当前为 {primary_count}",
                    hint=service_id,
                )
            if policy and instances:
                if policy.min_healthy_replicas > len(instances):
                    issue(
                        f"{base}.policy.min_healthy_replicas",
                        f"不能超过实例数 {len(instances)}",
                        hint=service_id,
                    )
                service_specs.append(ServiceSpec(service_id, policy, tuple(instances)))

        faults_raw = data.get("faults", [])
        faults: list[FaultSpec] = []
        if not isinstance(faults_raw, list):
            issue("faults", "必须是数组")
            faults_raw = []
        faulted_instances: set[str] = set()
        for fault_index, fault_data in enumerate(faults_raw):
            base = f"faults[{fault_index}]"
            if not isinstance(fault_data, dict):
                issue(base, "故障项必须是对象")
                continue
            for key in sorted(set(fault_data) - FAULT_KEYS):
                issue(f"{base}.{key}", f"未知字段 {key!r}")
            instance_id = fault_data.get("instance_id")
            kind = fault_data.get("kind")
            note = fault_data.get("note", "")
            if not isinstance(instance_id, str) or instance_id not in known_instance_ids:
                issue(f"{base}.instance_id", "必须引用已存在的实例", hint=instance_id)
            elif instance_id in faulted_instances:
                issue(f"{base}.instance_id", f"实例 {instance_id} 存在重复故障项", hint=instance_id)
            if kind not in VALID_FAULT_KINDS:
                issue(
                    f"{base}.kind",
                    "必须是 unreachable、timeout 或 error",
                    hint=instance_id,
                )
            if not isinstance(note, str):
                issue(f"{base}.note", "必须是字符串", hint=instance_id)
            if (
                isinstance(instance_id, str)
                and instance_id in known_instance_ids
                and kind in VALID_FAULT_KINDS
                and isinstance(note, str)
            ):
                faults.append(FaultSpec(instance_id, kind, note))
                faulted_instances.add(instance_id)

        if issues:
            self._raise(issues, source_name)

        assert limits is not None
        assert isinstance(version, str)
        scene = Scene(
            scene_version=version,
            probe_limits=limits,
            services=tuple(service_specs),
            faults=tuple(faults),
            fingerprint="",
            raw_text=raw_text,
        )
        return replace(scene, fingerprint=compute_scene_fingerprint(scene))

    @staticmethod
    def _raise(issues: Iterable[ValidationIssue], source_name: str) -> None:
        issue_list = list(issues)
        joined = "; ".join(str(item) for item in issue_list)
        raise BusinessError(
            BusinessCode.E_SCENE,
            f"{source_name}: 场景校验失败（{len(issue_list)} 项）: {joined}",
            details={"path": source_name, "issues": [item.as_dict() for item in issue_list]},
            issues=issue_list,
        )
