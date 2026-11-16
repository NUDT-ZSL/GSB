"""Immutable domain models shared by the loader, compiler and engine."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

DRIFT_MISSING = "missing"
DRIFT_EXTRA = "extra"
DRIFT_VALUE_MISMATCH = "value_mismatch"
DRIFT_ARTIFACT_INVALID = "artifact_invalid"

STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"

POLICY_SKIP = "skip_downstream"
POLICY_CONTINUE = "continue_downstream"
VALID_POLICIES = (POLICY_SKIP, POLICY_CONTINUE)


@dataclass(frozen=True)
class ConvergencePolicy:
    """Per-resource convergence policy.

    ``on_failure`` decides whether downstream resources are skipped when this
    resource fails. ``rollback_on_failure`` requests explicit rollback of the
    already-converged upstream resources.
    """

    on_failure: str = POLICY_SKIP
    rollback_on_failure: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "on_failure": self.on_failure,
            "rollback_on_failure": self.rollback_on_failure,
        }


@dataclass(frozen=True)
class ResourceDeclaration:
    """One resource inside a declaration group version."""

    key: str
    properties: dict[str, Any]
    references: tuple[str, ...]
    policy: ConvergencePolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "properties": dict(self.properties),
            "references": list(self.references),
            "policy": self.policy.to_dict(),
        }


@dataclass(frozen=True)
class DeclarationVersion:
    """An immutable, versioned snapshot of one declaration group."""

    group: str
    version: int
    depends_on: dict[str, int]
    resources: tuple[ResourceDeclaration, ...]
    source_file: str

    def resource_map(self) -> dict[str, ResourceDeclaration]:
        return {resource.key: resource for resource in self.resources}

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "version": self.version,
            "depends_on": dict(self.depends_on),
            "resources": [resource.to_dict() for resource in self.resources],
            "source_file": self.source_file,
        }


@dataclass
class Artifact:
    """Resource-level immutable compilation product.

    The artifact version only advances when the resource input fingerprint
    changes. It can additionally be marked invalid by upstream change
    propagation without advancing its version.
    """

    resource_key: str
    artifact_version: int
    input_fingerprint: str
    references: tuple[str, ...]
    properties: dict[str, Any]
    invalid: bool = False
    invalid_reason: str = ""
    invalidated_by: str = ""
    invalidation_path: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_key": self.resource_key,
            "artifact_version": self.artifact_version,
            "input_fingerprint": self.input_fingerprint,
            "references": list(self.references),
            "properties": dict(self.properties),
            "invalid": self.invalid,
            "invalid_reason": self.invalid_reason,
            "invalidated_by": self.invalidated_by,
            "invalidation_path": list(self.invalidation_path),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Artifact":
        return cls(
            resource_key=data["resource_key"],
            artifact_version=data["artifact_version"],
            input_fingerprint=data["input_fingerprint"],
            references=tuple(data.get("references", [])),
            properties=dict(data.get("properties", {})),
            invalid=data.get("invalid", False),
            invalid_reason=data.get("invalid_reason", ""),
            invalidated_by=data.get("invalidated_by", ""),
            invalidation_path=tuple(data.get("invalidation_path", [])),
        )

    def clone_clean(self) -> "Artifact":
        return replace(
            self,
            invalid=False,
            invalid_reason="",
            invalidated_by="",
            invalidation_path=(),
        )


@dataclass(frozen=True)
class Drift:
    """One property/resource-level drift between desired and actual state."""

    resource_key: str
    drift_type: str
    property_name: str
    expected: Any
    actual: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_key": self.resource_key,
            "drift_type": self.drift_type,
            "property_name": self.property_name,
            "expected": self.expected,
            "actual": self.actual,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Drift":
        return cls(
            resource_key=data["resource_key"],
            drift_type=data["drift_type"],
            property_name=data.get("property_name", ""),
            expected=data.get("expected"),
            actual=data.get("actual"),
        )


@dataclass(frozen=True)
class ConvergenceAction:
    """A planned unit of convergence work for one resource."""

    resource_key: str
    drift_type: str
    expected: Any
    current: Any
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_key": self.resource_key,
            "drift_type": self.drift_type,
            "expected": self.expected,
            "current": self.current,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConvergenceAction":
        return cls(
            resource_key=data["resource_key"],
            drift_type=data["drift_type"],
            expected=data.get("expected"),
            current=data.get("current"),
            reasons=tuple(data.get("reasons", [])),
        )


@dataclass
class TraceEntry:
    """One deterministic convergence trace record."""

    order: int
    resource_key: str
    status: str
    code: str
    reason: str
    triggered_by: str
    drift_type: str
    expected: Any
    current: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "order": self.order,
            "resource_key": self.resource_key,
            "status": self.status,
            "code": self.code,
            "reason": self.reason,
            "triggered_by": self.triggered_by,
            "drift_type": self.drift_type,
            "expected": self.expected,
            "current": self.current,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TraceEntry":
        return cls(
            order=data["order"],
            resource_key=data["resource_key"],
            status=data["status"],
            code=data["code"],
            reason=data.get("reason", ""),
            triggered_by=data.get("triggered_by", ""),
            drift_type=data.get("drift_type", ""),
            expected=data.get("expected"),
            current=data.get("current"),
        )
