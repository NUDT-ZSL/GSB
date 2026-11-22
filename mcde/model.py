from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .util import canonical_json, sha256_json

CONFIG_FIELDS = ("image", "replicas", "resources", "timeout_ms", "feature_flag")
POSITIVE_FIELDS = frozenset({"replicas", "resources", "timeout_ms"})


@dataclass(frozen=True)
class AppConfig:
    name: str
    image: str
    replicas: int
    resources: int
    timeout_ms: int
    feature_flag: bool

    def value(self, field_name: str) -> Any:
        return getattr(self, field_name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "image": self.image,
            "replicas": self.replicas,
            "resources": self.resources,
            "timeout_ms": self.timeout_ms,
            "feature_flag": self.feature_flag,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AppConfig":
        return cls(
            name=value["name"],
            image=value["image"],
            replicas=value["replicas"],
            resources=value["resources"],
            timeout_ms=value["timeout_ms"],
            feature_flag=value["feature_flag"],
        )


@dataclass(frozen=True)
class RegionOverlay:
    region: str
    allow_diff_overrides: bool
    overrides: dict[str, dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "region": self.region,
            "allow_diff_overrides": self.allow_diff_overrides,
            "overrides": self.overrides,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RegionOverlay":
        return cls(
            region=value["region"],
            allow_diff_overrides=value["allow_diff_overrides"],
            overrides=value["overrides"],
        )


@dataclass(frozen=True)
class ClusterOverlay:
    cluster: str
    region: str
    overrides: dict[str, dict[str, Any]]
    replicas_limit: int | None
    resource_quota: int | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster": self.cluster,
            "region": self.region,
            "overrides": self.overrides,
            "replicas_limit": self.replicas_limit,
            "resource_quota": self.resource_quota,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClusterOverlay":
        return cls(
            cluster=value["cluster"],
            region=value["region"],
            overrides=value["overrides"],
            replicas_limit=value.get("replicas_limit"),
            resource_quota=value.get("resource_quota"),
        )


@dataclass(frozen=True)
class ClusterMember:
    cluster: str
    region: str
    healthy: bool
    max_replicas: int
    max_resources: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster": self.cluster,
            "region": self.region,
            "healthy": self.healthy,
            "max_replicas": self.max_replicas,
            "max_resources": self.max_resources,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClusterMember":
        return cls(
            cluster=value["cluster"],
            region=value["region"],
            healthy=value["healthy"],
            max_replicas=value["max_replicas"],
            max_resources=value["max_resources"],
        )


@dataclass(frozen=True)
class AdmissionRules:
    require_approval_for_diff: bool
    approval_ttl_ms: int
    window_start_ms: int
    window_end_ms: int
    window_period_ms: int | None
    max_parallel: int
    round_budget_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "require_approval_for_diff": self.require_approval_for_diff,
            "approval_ttl_ms": self.approval_ttl_ms,
            "change_window": {
                "start_ms": self.window_start_ms,
                "end_ms": self.window_end_ms,
                "period_ms": self.window_period_ms,
            },
            "max_parallel": self.max_parallel,
            "round_budget_ms": self.round_budget_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AdmissionRules":
        window = value["change_window"]
        return cls(
            require_approval_for_diff=value["require_approval_for_diff"],
            approval_ttl_ms=value["approval_ttl_ms"],
            window_start_ms=window["start_ms"],
            window_end_ms=window["end_ms"],
            window_period_ms=window.get("period_ms"),
            max_parallel=value["max_parallel"],
            round_budget_ms=value["round_budget_ms"],
        )


@dataclass(frozen=True)
class Approval:
    approval_id: str
    subject: str
    scope_region: str
    scope_cluster: str
    value: Any
    issued_ms: int
    valid_until_ms: int

    def expires_ms(self, ttl_ms: int) -> int:
        return min(self.valid_until_ms, self.issued_ms + ttl_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.approval_id,
            "subject": self.subject,
            "scope": {
                "region": self.scope_region,
                "cluster": self.scope_cluster,
            },
            "value": self.value,
            "issued_ms": self.issued_ms,
            "valid_until_ms": self.valid_until_ms,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Approval":
        scope = value.get("scope", {})
        return cls(
            approval_id=value["id"],
            subject=value["subject"],
            scope_region=scope.get("region", "*"),
            scope_cluster=scope.get("cluster", "*"),
            value=value.get("value"),
            issued_ms=value["issued_ms"],
            valid_until_ms=value["valid_until_ms"],
        )


@dataclass(frozen=True)
class ClockStep:
    event: str
    advance_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {"event": self.event, "advance_ms": self.advance_ms}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClockStep":
        return cls(event=value["event"], advance_ms=value["advance_ms"])


@dataclass(frozen=True)
class ClockScript:
    start_ms: int
    steps: tuple[ClockStep, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_ms": self.start_ms,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ClockScript":
        return cls(
            start_ms=value["start_ms"],
            steps=tuple(ClockStep.from_dict(item) for item in value["steps"]),
        )


@dataclass(frozen=True)
class Bundle:
    apps: dict[str, AppConfig]
    regions: dict[str, RegionOverlay]
    cluster_overlays: dict[str, ClusterOverlay]
    inventory: dict[str, ClusterMember]
    rules: AdmissionRules
    approvals: dict[str, Approval]
    clock: ClockScript
    documents: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "apps": [self.apps[key].to_dict() for key in sorted(self.apps)],
            "regions": [
                self.regions[key].to_dict() for key in sorted(self.regions)
            ],
            "cluster_overlays": [
                self.cluster_overlays[key].to_dict()
                for key in sorted(self.cluster_overlays)
            ],
            "inventory": [
                self.inventory[key].to_dict() for key in sorted(self.inventory)
            ],
            "rules": self.rules.to_dict(),
            "approvals": [
                self.approvals[key].to_dict() for key in sorted(self.approvals)
            ],
            "clock": self.clock.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Bundle":
        return cls(
            apps={
                item["name"]: AppConfig.from_dict(item)
                for item in value["apps"]
            },
            regions={
                item["region"]: RegionOverlay.from_dict(item)
                for item in value["regions"]
            },
            cluster_overlays={
                item["cluster"]: ClusterOverlay.from_dict(item)
                for item in value["cluster_overlays"]
            },
            inventory={
                item["cluster"]: ClusterMember.from_dict(item)
                for item in value["inventory"]
            },
            rules=AdmissionRules.from_dict(value["rules"]),
            approvals={
                item["id"]: Approval.from_dict(item)
                for item in value["approvals"]
            },
            clock=ClockScript.from_dict(value["clock"]),
        )

    def digest(self) -> str:
        return sha256_json(self.to_dict())

    def canonical(self) -> str:
        return canonical_json(self.to_dict())

