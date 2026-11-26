"""Configurable resource limits and deterministic enforcement helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import DiagnosticError, E_RESOURCE_LIMIT


SUPPORTED_DEGRADE_POLICIES = {"error", "truncate_batch"}
DEGRADE_POLICY_ALIASES = {
    "fail": "error",
    "reject": "error",
    "truncate": "truncate_batch",
}


@dataclass(frozen=True)
class ResourceLimits:
    max_batch_docs: int = 1024
    max_postings_bytes: int = 8 * 1024 * 1024
    max_comparisons: int = 5_000_000
    degrade_policy: str = "error"

    @classmethod
    def from_mapping(cls, value: Any) -> "ResourceLimits":
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValueError("resource_limits must be an object")
        policy = value.get("degrade_policy", cls.degrade_policy)
        if isinstance(policy, str):
            policy = DEGRADE_POLICY_ALIASES.get(policy, policy)
        result = cls(
            max_batch_docs=value.get("max_batch_docs", cls.max_batch_docs),
            max_postings_bytes=value.get(
                "max_postings_bytes", cls.max_postings_bytes
            ),
            max_comparisons=value.get("max_comparisons", cls.max_comparisons),
            degrade_policy=policy,
        )
        result.validate()
        return result

    def validate(self) -> None:
        for name in ("max_batch_docs", "max_postings_bytes", "max_comparisons"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.degrade_policy not in SUPPORTED_DEGRADE_POLICIES:
            raise ValueError(
                f"degrade_policy must be one of {sorted(SUPPORTED_DEGRADE_POLICIES)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "degrade_policy": self.degrade_policy,
            "max_batch_docs": self.max_batch_docs,
            "max_comparisons": self.max_comparisons,
            "max_postings_bytes": self.max_postings_bytes,
        }


class ResourceGuard:
    """Tracks one operation's comparisons and discloses all enforcement actions."""

    def __init__(self, limits: ResourceLimits):
        self.limits = limits
        self.comparisons = 0
        self.actions: list[dict[str, Any]] = []

    def limit_batch_docs(self, count: int, *, batch_no: int) -> int:
        if count <= self.limits.max_batch_docs:
            return count
        if self.limits.degrade_policy == "truncate_batch":
            self.actions.append(
                {
                    "action": "truncate_batch",
                    "batch_no": batch_no,
                    "original_docs": count,
                    "kept_docs": self.limits.max_batch_docs,
                }
            )
            return self.limits.max_batch_docs
        raise DiagnosticError(
            E_RESOURCE_LIMIT,
            "batch document limit exceeded",
            {
                "batch_no": batch_no,
                "actual": count,
                "limit": self.limits.max_batch_docs,
            },
        )

    def add_comparisons(self, count: int = 1) -> None:
        self.comparisons += count
        if self.comparisons > self.limits.max_comparisons:
            raise DiagnosticError(
                E_RESOURCE_LIMIT,
                "sort comparison limit exceeded",
                {
                    "actual": self.comparisons,
                    "limit": self.limits.max_comparisons,
                },
            )

    def check_postings_size(self, size: int) -> None:
        if size > self.limits.max_postings_bytes:
            raise DiagnosticError(
                E_RESOURCE_LIMIT,
                "resident postings memory limit exceeded",
                {"actual": size, "limit": self.limits.max_postings_bytes},
            )

    def report(self) -> dict[str, Any]:
        return {
            "comparisons": self.comparisons,
            "actions": list(self.actions),
            "limits": self.limits.to_dict(),
        }
