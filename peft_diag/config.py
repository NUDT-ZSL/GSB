"""Configuration and deterministic resource degradation policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import config_error


@dataclass(frozen=True)
class ResourceLimits:
    max_task_memory_bytes: int = 64 * 1024 * 1024
    max_cache_entries: int = 256
    max_open_shards: int = 8
    max_recompute_tasks: int = 32

    def validate(self) -> None:
        for name, value in self.as_dict().items():
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise config_error(
                    f"{name} must be a positive integer",
                    field=name,
                    details={"value": value},
                )

    def as_dict(self) -> dict[str, int]:
        return {
            "max_task_memory_bytes": self.max_task_memory_bytes,
            "max_cache_entries": self.max_cache_entries,
            "max_open_shards": self.max_open_shards,
            "max_recompute_tasks": self.max_recompute_tasks,
        }

    @classmethod
    def from_mapping(cls, values: dict[str, Any] | None) -> "ResourceLimits":
        values = values or {}
        known = set(cls.__dataclass_fields__)
        unknown = sorted(set(values) - known)
        if unknown:
            raise config_error("unknown resource limit", field=unknown[0], details={"unknown": unknown})
        result = cls(**{key: values[key] for key in known if key in values})
        result.validate()
        return result


@dataclass
class DegradationLog:
    entries: list[dict[str, Any]] = field(default_factory=list)

    def record(
        self,
        *,
        reason: str,
        priority: int,
        action: str,
        task_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.entries.append(
            {
                "reason": reason,
                "priority": priority,
                "action": action,
                "task_id": task_id,
                "details": details or {},
            }
        )

    def as_list(self) -> list[dict[str, Any]]:
        return list(self.entries)


@dataclass
class EngineConfig:
    tasks_path: str
    catalog_path: str
    state_dir: str
    run_id: str = "primary"
    algorithm_version: str = "adam-peft-v1"
    snapshot_format: str = "v2"
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def validate(self) -> None:
        self.limits.validate()
        if not self.run_id or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for ch in self.run_id):
            raise config_error("run_id contains unsupported characters", field="run_id")
        if self.snapshot_format not in {"v1", "v2"}:
            raise config_error("snapshot_format must be v1 or v2", field="snapshot_format")
        if not self.algorithm_version:
            raise config_error("algorithm_version is required", field="algorithm_version")

