"""Unified intermediate representation and configuration value objects."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

V1 = "v1"
V2 = "v2"

# Stable phase names; they also feed the per-instance scheduling sort key.
STAGE_PARSE_V1 = "parse_v1"
STAGE_PARSE_V2 = "parse_v2"
STAGE_CONVERT = "convert"
STAGE_FUSE = "fuse"
STAGE_SNAPSHOT = "snapshot"


@dataclass(frozen=True)
class InstanceRecord:
    """Canonical, format-independent representation of one instance mask."""

    instance_id: str
    scene_id: str
    semantic_class: str
    width: int
    height: int
    runs: tuple[tuple[int, int], ...]
    source_version: str
    confidence: float
    is_crowd: bool
    palette_name: str | None
    palette_rgb: tuple[int, int, int] | None
    pixel_count: int
    source_flags: dict[str, bool] = field(default_factory=dict)
    cache_key: str | None = None

    @property
    def area(self) -> int:
        return self.pixel_count

    def to_json_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["runs"] = [list(run) for run in self.runs]
        return data

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> "InstanceRecord":
        data = dict(data)
        data["runs"] = tuple((int(start), int(length)) for start, length in data["runs"])
        if data.get("palette_rgb") is not None:
            data["palette_rgb"] = tuple(data["palette_rgb"])
        data["source_flags"] = dict(data.get("source_flags") or {})
        return cls(**data)

    def with_cache_key(self, cache_key: str) -> "InstanceRecord":
        return replace(self, cache_key=cache_key)


@dataclass(frozen=True)
class FailureRecord:
    instance_id: str
    stage: str
    code: str
    exception_type: str
    message: str
    sort_key: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_id": self.instance_id,
            "stage": self.stage,
            "code": self.code,
            "exception_type": self.exception_type,
            "message": self.message,
            "sort_key": self.sort_key,
            "details": self.details,
        }


@dataclass(frozen=True)
class PipelineConfig:
    workers: int
    rle_compression: int
    retention_policy: str
    min_coverage: float
    iou_threshold: float
    default_confidence: float
    default_crowd: bool
    snapshot_commit_interval: int
    boundary_iou_threshold: float = 0.7
    fault_injection: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> "PipelineConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        required = {
            "workers",
            "rle_compression",
            "retention_policy",
            "min_coverage",
            "iou_threshold",
            "default_confidence",
            "default_crowd",
            "snapshot_commit_interval",
        }
        missing = required - raw.keys()
        if missing:
            raise ValueError(f"missing config keys: {sorted(missing)}")
        if raw["rle_compression"] not in (0, 1):
            raise ValueError("rle_compression must be 0 (pairs) or 1 (packed)")
        if raw["retention_policy"] not in ("keep_all", "drop_below_coverage"):
            raise ValueError("retention_policy must be keep_all or drop_below_coverage")
        return cls(
            workers=int(raw["workers"]),
            rle_compression=int(raw["rle_compression"]),
            retention_policy=str(raw["retention_policy"]),
            min_coverage=float(raw["min_coverage"]),
            iou_threshold=float(raw["iou_threshold"]),
            default_confidence=float(raw["default_confidence"]),
            default_crowd=bool(raw["default_crowd"]),
            snapshot_commit_interval=int(raw["snapshot_commit_interval"]),
            boundary_iou_threshold=float(raw.get("boundary_iou_threshold", raw["iou_threshold"])),
            fault_injection=dict(raw.get("fault_injection") or {}),
        )

    def without_faults(self) -> "PipelineConfig":
        return replace(self, fault_injection={})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
