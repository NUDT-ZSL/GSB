"""Configuration loading and defaults for local operation."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ParseDefaults:
    canvas_width: int = 320
    canvas_height: int = 180
    feather_radius: float = 8.0
    color_space: str = "linear_rgb"
    exposure_mode: str = "linear"
    overlap_weight: float = 1.0
    interpolation: str = "bilinear"
    weight_tolerance: float = 1e-9


@dataclass(frozen=True)
class RenderSettings:
    interpolation: str = "bilinear"
    weight_tolerance: float = 1e-9
    strict_validation: bool = True
    require_full_coverage: bool = False
    requested_parallel_tasks: int = 4


@dataclass(frozen=True)
class ResourceLimits:
    max_memory_bytes: int = 256 * 1024 * 1024
    max_cache_entries: int = 128
    max_parallel_tasks: int = 4
    on_limit: str = "degrade"


@dataclass(frozen=True)
class AppConfig:
    parse_defaults: ParseDefaults = field(default_factory=ParseDefaults)
    render: RenderSettings = field(default_factory=RenderSettings)
    resources: ResourceLimits = field(default_factory=ResourceLimits)
    cache_dir: str = "artifacts/cache"
    snapshots_dir: str = "artifacts/snapshots"
    source_path: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "parse_defaults": self.parse_defaults.__dict__,
            "render": self.render.__dict__,
            "resources": self.resources.__dict__,
            "cache_dir": self.cache_dir,
            "snapshots_dir": self.snapshots_dir,
        }


def _nested(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    return value if isinstance(value, dict) else {}


def load_config(path: str | Path | None) -> AppConfig:
    if path is None:
        return AppConfig()
    source = Path(path)
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("config root must be a JSON object")
    parse_data = _nested(data, "parse_defaults")
    render_data = _nested(data, "render")
    resource_data = _nested(data, "resources")
    default_parse = ParseDefaults()
    default_render = RenderSettings()
    default_resources = ResourceLimits()
    parse_defaults = ParseDefaults(
        canvas_width=int(parse_data.get("canvas_width", default_parse.canvas_width)),
        canvas_height=int(parse_data.get("canvas_height", default_parse.canvas_height)),
        feather_radius=float(parse_data.get("feather_radius", default_parse.feather_radius)),
        color_space=str(parse_data.get("color_space", default_parse.color_space)),
        exposure_mode=str(parse_data.get("exposure_mode", default_parse.exposure_mode)),
        overlap_weight=float(parse_data.get("overlap_weight", default_parse.overlap_weight)),
        interpolation=str(parse_data.get("interpolation", default_parse.interpolation)),
        weight_tolerance=float(parse_data.get("weight_tolerance", default_parse.weight_tolerance)),
    )
    render = RenderSettings(
        interpolation=str(render_data.get("interpolation", default_render.interpolation)),
        weight_tolerance=float(render_data.get("weight_tolerance", default_render.weight_tolerance)),
        strict_validation=bool(render_data.get("strict_validation", default_render.strict_validation)),
        require_full_coverage=bool(
            render_data.get("require_full_coverage", default_render.require_full_coverage)
        ),
        requested_parallel_tasks=int(
            render_data.get("requested_parallel_tasks", default_render.requested_parallel_tasks)
        ),
    )
    resources = ResourceLimits(
        max_memory_bytes=int(
            resource_data.get("max_memory_bytes", default_resources.max_memory_bytes)
        ),
        max_cache_entries=int(
            resource_data.get("max_cache_entries", default_resources.max_cache_entries)
        ),
        max_parallel_tasks=int(
            resource_data.get("max_parallel_tasks", default_resources.max_parallel_tasks)
        ),
        on_limit=str(resource_data.get("on_limit", default_resources.on_limit)),
    )
    return AppConfig(
        parse_defaults=parse_defaults,
        render=render,
        resources=resources,
        cache_dir=str(data.get("cache_dir", "artifacts/cache")),
        snapshots_dir=str(data.get("snapshots_dir", "artifacts/snapshots")),
        source_path=str(source.resolve()),
    )

