"""Immutable scene model plus mutable runtime model."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Iterable

VALID_ROLES = ("primary", "standby")
VALID_MODES = ("auto", "manual")
VALID_FAULT_KINDS = ("unreachable", "timeout", "error")
VALID_HEALTH_STATES = ("unknown", "healthy", "unhealthy")


@dataclass(frozen=True)
class ProbeConfig:
    timeout_ms: int
    retries: int
    success_threshold: int
    failure_threshold: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class InstanceSpec:
    service_id: str
    instance_id: str
    role: str
    port: int
    probe: ProbeConfig

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "instance_id": self.instance_id,
            "role": self.role,
            "port": self.port,
            "probe": self.probe.as_dict(),
        }


@dataclass(frozen=True)
class PolicySpec:
    service_id: str
    mode: str
    cooldown_ms: int
    min_healthy_replicas: int
    version: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ServiceSpec:
    service_id: str
    policy: PolicySpec
    instances: tuple[InstanceSpec, ...]


@dataclass(frozen=True)
class FaultSpec:
    instance_id: str
    kind: str
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"instance_id": self.instance_id, "kind": self.kind}
        if self.note:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class ProbeLimits:
    global_concurrency: int
    service_quota: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class Scene:
    scene_version: str
    probe_limits: ProbeLimits
    services: tuple[ServiceSpec, ...]
    faults: tuple[FaultSpec, ...]
    fingerprint: str
    raw_text: str = field(default="", compare=False, repr=False)

    def instances(self) -> tuple[InstanceSpec, ...]:
        return tuple(
            instance
            for service in sorted(self.services, key=lambda item: item.service_id)
            for instance in sorted(service.instances, key=lambda item: item.instance_id)
        )

    def service(self, service_id: str) -> ServiceSpec:
        for service in self.services:
            if service.service_id == service_id:
                return service
        raise KeyError(service_id)

    def instance(self, instance_id: str) -> InstanceSpec:
        for instance in self.instances():
            if instance.instance_id == instance_id:
                return instance
        raise KeyError(instance_id)

    def faults_by_instance(self) -> dict[str, str]:
        return {fault.instance_id: fault.kind for fault in self.faults}

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "scene_version": self.scene_version,
            "probe_limits": self.probe_limits.as_dict(),
            "services": [
                {
                    "service_id": service.service_id,
                    "policy": service.policy.as_dict(),
                    "instances": [
                        instance.as_dict()
                        for instance in sorted(service.instances, key=lambda item: item.instance_id)
                    ],
                }
                for service in sorted(self.services, key=lambda item: item.service_id)
            ],
            "faults": [
                fault.as_dict()
                for fault in sorted(self.faults, key=lambda item: item.instance_id)
            ],
        }

    def config_section(self) -> dict[str, Any]:
        payload = self.canonical_payload()
        return {
            "scene_version": payload["scene_version"],
            "probe_limits": payload["probe_limits"],
            "services": payload["services"],
        }

    def with_port_overrides(self, overrides: dict[str, int]) -> "Scene":
        if not overrides:
            return self
        unknown = sorted(set(overrides) - {item.instance_id for item in self.instances()})
        if unknown:
            raise KeyError(f"unknown instance ids: {', '.join(unknown)}")
        services: list[ServiceSpec] = []
        for service in self.services:
            instances = tuple(
                replace(instance, port=overrides.get(instance.instance_id, instance.port))
                for instance in service.instances
            )
            services.append(replace(service, instances=instances))
        seen_ports: dict[int, str] = {}
        for service in services:
            for instance in service.instances:
                if not 1 <= instance.port <= 65535:
                    raise ValueError(
                        f"实例 {instance.instance_id} 的端口 {instance.port} 超出 1..65535"
                    )
                if instance.port in seen_ports:
                    raise ValueError(
                        f"端口 {instance.port} 被 {seen_ports[instance.port]} 和 "
                        f"{instance.instance_id} 重复使用"
                    )
                seen_ports[instance.port] = instance.instance_id
        candidate = Scene(
            scene_version=self.scene_version,
            probe_limits=self.probe_limits,
            services=tuple(services),
            faults=self.faults,
            fingerprint="",
            raw_text=self.raw_text,
        )
        return replace(candidate, fingerprint=compute_scene_fingerprint(candidate))


def compute_scene_fingerprint(scene: Scene) -> str:
    encoded = json.dumps(
        scene.config_section(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class InstanceRuntime:
    service_id: str
    instance_id: str
    role: str
    port: int
    version: int
    isolated: bool
    health_state: str
    consecutive_successes: int
    consecutive_failures: int
    probe: ProbeConfig
    requires_reprobe: bool = False
    updated_at_epoch_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "instance_id": self.instance_id,
            "role": self.role,
            "port": self.port,
            "version": self.version,
            "isolated": self.isolated,
            "health_state": self.health_state,
            "consecutive_successes": self.consecutive_successes,
            "consecutive_failures": self.consecutive_failures,
            "probe": self.probe.as_dict(),
            "requires_reprobe": self.requires_reprobe,
            "updated_at_epoch_ms": self.updated_at_epoch_ms,
        }


@dataclass
class ServiceRuntime:
    policy: PolicySpec
    cooldown_started_epoch_ms: int | None = None
    last_switch_target: str | None = None
    switching: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.policy.service_id,
            "mode": self.policy.mode,
            "cooldown_ms": self.policy.cooldown_ms,
            "min_healthy_replicas": self.policy.min_healthy_replicas,
            "policy_version": self.policy.version,
            "cooldown_started_epoch_ms": self.cooldown_started_epoch_ms,
            "last_switch_target": self.last_switch_target,
            "switching": self.switching,
        }


@dataclass(frozen=True)
class ProbeAttempt:
    run_id: str
    round_no: int
    service_id: str
    instance_id: str
    attempt_no: int
    conclusion: str
    detail: str
    started_mono_ms: int
    ended_mono_ms: int
    created_epoch_ms: int
    fault_kind: str | None = None

    @property
    def duration_ms(self) -> int:
        return max(0, self.ended_mono_ms - self.started_mono_ms)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["duration_ms"] = self.duration_ms
        return payload


@dataclass(frozen=True)
class ProbeResult:
    service_id: str
    instance_id: str
    attempts: tuple[ProbeAttempt, ...]
    final_conclusion: str

    @property
    def success(self) -> bool:
        return self.final_conclusion == "success"

    def as_dict(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "instance_id": self.instance_id,
            "attempts": [attempt.as_dict() for attempt in self.attempts],
            "final_conclusion": self.final_conclusion,
        }


@dataclass(frozen=True)
class ProbeBatch:
    results: tuple[ProbeResult, ...]
    peak_global: int
    peak_by_service: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "results": [result.as_dict() for result in self.results],
            "peak_global": self.peak_global,
            "peak_by_service": dict(sorted(self.peak_by_service.items())),
        }


@dataclass(frozen=True)
class StateTransition:
    run_id: str
    round_no: int
    service_id: str
    instance_id: str
    from_state: str
    to_state: str
    reason: str
    at_mono_ms: int
    at_epoch_ms: int

    def trace_key(self) -> dict[str, str]:
        return {
            "service_id": self.service_id,
            "instance_id": self.instance_id,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class BusinessEvent:
    run_id: str
    round_no: int
    code: str
    service_id: str | None
    instance_id: str | None
    message: str
    details: dict[str, Any]
    at_epoch_ms: int

    def trace_key(self) -> dict[str, Any]:
        stable_details: dict[str, Any] = {}
        for key in sorted(self.details):
            if key == "attempts":
                stable_details[key] = [
                    {
                        "attempt_no": item.get("attempt_no"),
                        "conclusion": item.get("conclusion"),
                    }
                    for item in self.details[key]
                ]
            elif key in {
                "trigger_reason",
                "target",
                "source",
                "policy_version",
                "deviations",
            }:
                stable_details[key] = self.details[key]
        return {
            "code": self.code,
            "service_id": self.service_id,
            "instance_id": self.instance_id,
            "details": stable_details,
        }


@dataclass(frozen=True)
class SwitchDecision:
    request_id: str
    run_id: str
    service_id: str
    code: str
    source_instance: str | None
    target_instance: str | None
    trigger_reason: str | None
    policy_version: str
    switched: bool
    started_epoch_ms: int
    ended_epoch_ms: int | None
    details: dict[str, Any] = field(default_factory=dict)

    def trace_key(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "code": self.code,
            "source_instance": self.source_instance,
            "target_instance": self.target_instance,
            "trigger_reason": self.trigger_reason,
            "policy_version": self.policy_version,
            "details": {
                key: self.details[key]
                for key in sorted(self.details)
                if key
                in {
                    "deviations",
                    "reason",
                    "remaining_ms",
                    "cooldown_started_epoch_ms",
                    "cooldown_ms",
                }
            },
        }


@dataclass(frozen=True)
class CycleResult:
    run_id: str
    round_no: int
    started_epoch_ms: int
    probe_batch: ProbeBatch
    transitions: tuple[StateTransition, ...]
    switch_decisions: tuple[SwitchDecision, ...]
    business_events: tuple[BusinessEvent, ...]
    snapshot_id: int | None
    runtime_faults: dict[str, str]

    @property
    def codes(self) -> list[str]:
        return list(
            dict.fromkeys(
                [event.code for event in self.business_events]
                + [decision.code for decision in self.switch_decisions]
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "round_no": self.round_no,
            "started_epoch_ms": self.started_epoch_ms,
            "codes": self.codes,
            "probe_batch": self.probe_batch.as_dict(),
            "transitions": [transition.trace_key() for transition in self.transitions],
            "switch_decisions": [decision.trace_key() for decision in self.switch_decisions],
            "business_events": [event.trace_key() for event in self.business_events],
            "snapshot_id": self.snapshot_id,
            "runtime_faults": dict(sorted(self.runtime_faults.items())),
        }


@dataclass(frozen=True)
class RecoveryReport:
    scene_version_match: bool
    snapshot_id: int | None
    full_replace: bool
    incomplete_services: tuple[str, ...]
    requires_reprobe_services: tuple[str, ...]
    messages: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scene_version_match": self.scene_version_match,
            "snapshot_id": self.snapshot_id,
            "full_replace": self.full_replace,
            "incomplete_services": list(self.incomplete_services),
            "requires_reprobe_services": list(self.requires_reprobe_services),
            "messages": list(self.messages),
        }


def sorted_runtime(instances: Iterable[InstanceRuntime]) -> list[InstanceRuntime]:
    return sorted(instances, key=lambda item: (item.service_id, item.instance_id))
