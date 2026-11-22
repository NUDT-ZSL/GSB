"""Resource estimation and deterministic limit decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import ResourceLimits
from .errors import BusinessError, ErrorCode
from .model import SceneParameters


@dataclass(frozen=True)
class ResourcePlan:
    estimated_memory_bytes: int
    memory_limit_bytes: int
    requested_parallel_tasks: int
    effective_parallel_tasks: int
    max_cache_entries: int
    notices: tuple[dict[str, Any], ...] = ()


def estimate_render_memory_bytes(scene: SceneParameters) -> int:
    """Conservative estimate for per-view layers plus fusion surfaces."""

    pixels = scene.canvas_width * scene.canvas_height
    view_count = len(scene.views)
    # float64 RGB + mask + margin for every view, then normalized weights,
    # float panorama, and uint8 panorama.
    per_view = pixels * ((3 * 8) + 1 + 8)
    fusion = pixels * ((3 * 8) + 8 + 8 + 3)
    return int(view_count * per_view + fusion + 4096 * max(1, view_count))


def plan_resources(
    scene: SceneParameters,
    limits: ResourceLimits,
    requested_parallel_tasks: int,
) -> ResourcePlan:
    estimated = estimate_render_memory_bytes(scene)
    if estimated > limits.max_memory_bytes:
        raise BusinessError(
            ErrorCode.RESOURCE_LIMIT,
            "estimated panorama memory exceeds the configured limit",
            stage="resource",
            field_path="resources.max_memory_bytes",
            details={
                "estimated_memory_bytes": estimated,
                "limit_bytes": limits.max_memory_bytes,
                "requested_parallel_tasks": requested_parallel_tasks,
            },
        )
    notices: list[dict[str, Any]] = []
    effective_tasks = max(1, min(requested_parallel_tasks, len(scene.views)))
    if requested_parallel_tasks > limits.max_parallel_tasks:
        if limits.on_limit != "degrade":
            raise BusinessError(
                ErrorCode.RESOURCE_LIMIT,
                "parallel resampling task request exceeds the configured limit",
                stage="resource",
                field_path="resources.max_parallel_tasks",
                details={
                    "requested_parallel_tasks": requested_parallel_tasks,
                    "max_parallel_tasks": limits.max_parallel_tasks,
                },
            )
        notices.append(
            {
                "stage": "resource",
                "code": ErrorCode.RESOURCE_LIMIT,
                "category": "parallelism_reduced",
                "upstream_code": ErrorCode.RESOURCE_LIMIT,
                "details": {
                    "requested_parallel_tasks": requested_parallel_tasks,
                    "max_parallel_tasks": limits.max_parallel_tasks,
                    "effective_parallel_tasks": 1,
                    "mode": "deterministic_serial_fallback",
                },
            }
        )
        effective_tasks = 1
    return ResourcePlan(
        estimated_memory_bytes=estimated,
        memory_limit_bytes=limits.max_memory_bytes,
        requested_parallel_tasks=requested_parallel_tasks,
        effective_parallel_tasks=effective_tasks,
        max_cache_entries=limits.max_cache_entries,
        notices=tuple(notices),
    )

