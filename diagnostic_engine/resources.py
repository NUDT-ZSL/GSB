"""Resource limits and deterministic block-size degradation."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .errors import ErrorCode, DiagnosticError
from .models import ClipTask


@dataclass(frozen=True)
class ResourceLimits:
    max_blocks_per_clip: int = 64
    max_resident_bytes: int = 16 * 1024 * 1024
    max_propagation_states: int = 262_144
    policy: str = "degrade"  # "degrade" or "reject"

    def __post_init__(self) -> None:
        for name in (
            "max_blocks_per_clip",
            "max_resident_bytes",
            "max_propagation_states",
        ):
            if not isinstance(getattr(self, name), int) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.policy not in ("degrade", "reject"):
            raise ValueError("policy must be 'degrade' or 'reject'")


@dataclass(frozen=True)
class ResourcePlan:
    requested_block_size: int
    effective_block_size: int
    block_count: int
    estimated_resident_bytes: int
    max_intermediate_states_per_block: int
    degradations: list[str] = field(default_factory=list)
    degraded: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "requested_block_size": self.requested_block_size,
            "effective_block_size": self.effective_block_size,
            "block_count": self.block_count,
            "estimated_resident_bytes": self.estimated_resident_bytes,
            "max_intermediate_states_per_block": self.max_intermediate_states_per_block,
            "degradations": self.degradations,
            "degraded": self.degraded,
        }


def plan_resources(task: ClipTask, limits: ResourceLimits) -> ResourcePlan:
    elements = task.frame_elements
    uint8_frame_bytes = elements
    int16_state_bytes = elements * 2
    fixed_resident_bytes = uint8_frame_bytes + int16_state_bytes

    if limits.max_resident_bytes < fixed_resident_bytes + uint8_frame_bytes:
        raise DiagnosticError(
            ErrorCode.RESOURCE_LIMIT,
            "resource limit cannot hold even one frame and propagation state",
            clip_id=task.clip_id,
            field="max_resident_bytes",
            details={
                "limit": limits.max_resident_bytes,
                "minimum_required": fixed_resident_bytes + uint8_frame_bytes,
                "category": "resident_memory",
            },
        )

    memory_block_frames = max(
        1,
        (limits.max_resident_bytes - fixed_resident_bytes) // uint8_frame_bytes,
    )
    state_block_frames = max(
        0, limits.max_propagation_states // task.propagation_steps
    )
    if state_block_frames < 1:
        raise DiagnosticError(
            ErrorCode.RESOURCE_LIMIT,
            "propagation_steps exceeds max_propagation_states for one frame",
            clip_id=task.clip_id,
            field="propagation_steps",
            details={
                "propagation_steps": task.propagation_steps,
                "max_propagation_states": limits.max_propagation_states,
                "category": "propagation_states",
            },
        )

    effective = task.block_size
    degradations: list[str] = []
    if effective > memory_block_frames:
        if limits.policy == "reject":
            raise DiagnosticError(
                ErrorCode.RESOURCE_LIMIT,
                "block memory residency exceeds limit",
                clip_id=task.clip_id,
                field="block_size",
                details={
                    "category": "resident_memory",
                    "requested_block_size": task.block_size,
                    "maximum_block_size": memory_block_frames,
                    "max_resident_bytes": limits.max_resident_bytes,
                },
            )
        effective = memory_block_frames
        degradations.append("resident_memory")

    if effective > state_block_frames:
        if limits.policy == "reject":
            raise DiagnosticError(
                ErrorCode.RESOURCE_LIMIT,
                "block propagation intermediate state count exceeds limit",
                clip_id=task.clip_id,
                field="block_size",
                details={
                    "category": "propagation_states",
                    "requested_block_size": task.block_size,
                    "maximum_block_size": state_block_frames,
                    "max_propagation_states": limits.max_propagation_states,
                },
            )
        effective = state_block_frames
        if "propagation_states" not in degradations:
            degradations.append("propagation_states")

    block_count = math.ceil(task.total_frames / effective)
    if block_count > limits.max_blocks_per_clip:
        raise DiagnosticError(
            ErrorCode.RESOURCE_LIMIT,
            "clip block count exceeds max_blocks_per_clip",
            clip_id=task.clip_id,
            field="max_blocks_per_clip",
            details={
                "category": "block_count",
                "block_count": block_count,
                "effective_block_size": effective,
                "limit": limits.max_blocks_per_clip,
            },
        )

    last_block_frames = task.total_frames - (block_count - 1) * effective
    peak_block_frames = effective
    resident_bytes = (
        fixed_resident_bytes + peak_block_frames * uint8_frame_bytes
    )
    max_states = peak_block_frames * task.propagation_steps
    return ResourcePlan(
        requested_block_size=task.block_size,
        effective_block_size=effective,
        block_count=block_count,
        estimated_resident_bytes=resident_bytes,
        max_intermediate_states_per_block=max_states,
        degradations=degradations,
        degraded=bool(degradations) or effective != task.block_size,
    )
