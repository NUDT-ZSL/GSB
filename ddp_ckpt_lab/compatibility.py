"""Explicit restore compatibility rules for evolving training configurations."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config import TrainingConfig
from .errors import BusinessError


IMMUTABLE_FIELDS = (
    "data_seed",
    "seed",
    "samples",
    "features",
    "hidden_dim",
    "workers",
    "batch_size",
    "sharding",
    "momentum",
)
RUNTIME_ONLY_FIELDS = ("train_steps", "checkpoint_every")
MIGRATABLE_FIELDS = ("learning_rate",)


@dataclass(frozen=True)
class CompatibilityResult:
    compatible: bool
    conflicts: tuple[dict[str, Any], ...]
    learning_rate_changed: bool
    migration_steps: tuple[str, ...]
    source_state_version: str
    target_state_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "compatible": self.compatible,
            "conflicts": list(self.conflicts),
            "learning_rate_changed": self.learning_rate_changed,
            "migration_steps": list(self.migration_steps),
            "source_state_version": self.source_state_version,
            "target_state_version": self.target_state_version,
            "immutable_fields": list(IMMUTABLE_FIELDS),
            "runtime_only_fields": list(RUNTIME_ONLY_FIELDS),
            "migratable_fields": list(MIGRATABLE_FIELDS),
        }


def assess_compatibility(
    checkpoint_config: TrainingConfig,
    requested_config: TrainingConfig,
    *,
    source_format_version: int,
    target_format_version: int,
) -> CompatibilityResult:
    conflicts = tuple(
        {
            "field": field_name,
            "checkpoint_value": getattr(checkpoint_config, field_name),
            "requested_value": getattr(requested_config, field_name),
        }
        for field_name in IMMUTABLE_FIELDS
        if getattr(checkpoint_config, field_name) != getattr(requested_config, field_name)
    )
    learning_rate_changed = (
        checkpoint_config.learning_rate != requested_config.learning_rate
    )
    steps = [
        "validate checkpoint shard digests and manifest content digest",
        "classify immutable/model/data fields before rebuilding runtime state",
        "discard any pre-existing runtime state object",
        "rebuild all four workers from checkpoint arrays and serialized RNG/cursors",
    ]
    if learning_rate_changed:
        steps.append(
            "replace learning_rate for future updates: "
            f"{checkpoint_config.learning_rate} -> {requested_config.learning_rate} "
            "(existing parameters and momentum are preserved)"
        )
    if source_format_version != target_format_version:
        steps.insert(
            1,
            f"migrate checkpoint format v{source_format_version} -> "
            f"v{target_format_version} with replace-rebuild semantics",
        )
    steps.append("recompute canonical v2 state digest after full replacement")
    return CompatibilityResult(
        compatible=not conflicts,
        conflicts=conflicts,
        learning_rate_changed=learning_rate_changed,
        migration_steps=tuple(steps),
        source_state_version=(
            f"format-v{source_format_version}/config-"
            f"{checkpoint_config.config_digest[:12]}"
        ),
        target_state_version=(
            f"format-v{target_format_version}/config-"
            f"{requested_config.config_digest[:12]}"
        ),
    )


def require_compatibility(
    checkpoint_config: TrainingConfig,
    requested_config: TrainingConfig,
    *,
    source_format_version: int,
    target_format_version: int,
) -> CompatibilityResult:
    result = assess_compatibility(
        checkpoint_config,
        requested_config,
        source_format_version=source_format_version,
        target_format_version=target_format_version,
    )
    if not result.compatible:
        raise BusinessError(
            "E_CONFIG",
            "检查点与请求的训练配置存在不可恢复冲突",
            conflicts=list(result.conflicts),
            source_config_digest=checkpoint_config.config_digest,
            requested_config_digest=requested_config.config_digest,
        )
    return result
