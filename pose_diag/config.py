"""Validated runtime configuration and region geometry."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .io_utils import read_json


@dataclass(frozen=True)
class PipelineConfig:
    quantization_decimals: int = 2
    refine_radius: int = 2
    peak_threshold: float = 0.02
    peak_neighborhood_radius: int = 1
    region_rows: int = 5
    region_cols: int = 5
    center_region: tuple[int, int] = (2, 2)
    latency_budget_ms: float = 500.0
    max_scene_pixels: int = 1_000_000
    max_peak_memory_mb: float = 256.0
    max_parallel_previews: int = 2
    scoring: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PipelineConfig:
        region_grid = raw.get("region_grid", [5, 5])
        if not isinstance(region_grid, (list, tuple)) or len(region_grid) != 2:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "region_grid must contain [rows, cols]",
                field="region_grid",
            )
        center = raw.get("center_region", [2, 2])
        if not isinstance(center, (list, tuple)) or len(center) != 2:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "center_region must contain [row, col]",
                field="center_region",
            )
        config = cls(
            quantization_decimals=int(raw.get("quantization_decimals", 2)),
            refine_radius=int(raw.get("refine_radius", 2)),
            peak_threshold=float(raw.get("peak_threshold", 0.02)),
            peak_neighborhood_radius=int(raw.get("peak_neighborhood_radius", 1)),
            region_rows=int(region_grid[0]),
            region_cols=int(region_grid[1]),
            center_region=(int(center[0]), int(center[1])),
            latency_budget_ms=float(raw.get("latency_budget_ms", 500.0)),
            max_scene_pixels=int(raw.get("max_scene_pixels", 1_000_000)),
            max_peak_memory_mb=float(raw.get("max_peak_memory_mb", 256.0)),
            max_parallel_previews=int(raw.get("max_parallel_previews", 2)),
            scoring=dict(raw.get("scoring", {})),
        )
        config.validate()
        return config

    @classmethod
    def load(cls, path: str | Path) -> PipelineConfig:
        return cls.from_dict(read_json(path))

    def validate(self) -> None:
        if not 0 <= self.quantization_decimals <= 6:
            raise BusinessError(
                "E_QUANT_INVALID",
                "quantization_decimals must be between 0 and 6",
                field="quantization_decimals",
                details={"value": self.quantization_decimals},
            )
        if self.refine_radius < 1:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "refine_radius must be at least 1",
                field="refine_radius",
            )
        if self.peak_neighborhood_radius < 1:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "peak_neighborhood_radius must be at least 1",
                field="peak_neighborhood_radius",
            )
        if self.peak_threshold < 0:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "peak_threshold must be non-negative",
                field="peak_threshold",
            )
        if self.region_rows < 1 or self.region_cols < 1:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "region grid dimensions must be positive",
                field="region_grid",
            )
        row, col = self.center_region
        if not (0 <= row < self.region_rows and 0 <= col < self.region_cols):
            raise BusinessError(
                "E_CONFIG_INVALID",
                "center_region is outside the region grid",
                field="center_region",
            )
        if self.latency_budget_ms <= 0:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "latency_budget_ms must be positive",
                field="latency_budget_ms",
            )
        if self.max_scene_pixels < 1 or self.max_peak_memory_mb <= 0:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "resource limits must be positive",
                field="resource_limits",
            )
        if self.max_parallel_previews < 1:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "max_parallel_previews must be positive",
                field="max_parallel_previews",
            )
        self.validate_scoring(self.scoring)

    @staticmethod
    def validate_scoring(scoring: dict[str, Any]) -> None:
        threshold = scoring.get("match_threshold")
        if not isinstance(threshold, (int, float)) or threshold <= 0:
            raise BusinessError(
                "E_MATCH_THRESHOLD",
                "match_threshold must be positive",
                field="match_threshold",
                details={"value": threshold},
            )
        for field_name in ("intensity_weight", "coverage_weight", "consistency_weight"):
            value = scoring.get(field_name)
            if not isinstance(value, (int, float)) or value < 0:
                raise BusinessError(
                    "E_CONFIG_INVALID",
                    f"{field_name} must be non-negative",
                    field=field_name,
                )
        decimals = scoring.get("score_decimals", 9)
        if not isinstance(decimals, int) or not 0 <= decimals <= 12:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "score_decimals must be an integer between 0 and 12",
                field="score_decimals",
            )

    def region_bbox(
        self, row: int, col: int, canvas_shape: tuple[int, int, int]
    ) -> tuple[int, int, int, int]:
        if not (0 <= row < self.region_rows and 0 <= col < self.region_cols):
            raise BusinessError(
                "E_REGION_INVALID",
                "region index is outside the configured grid",
                field="region",
                details={"row": row, "col": col},
            )
        height, width = int(canvas_shape[0]), int(canvas_shape[1])
        x0 = (col * width) // self.region_cols
        x1 = ((col + 1) * width) // self.region_cols
        y0 = (row * height) // self.region_rows
        y1 = ((row + 1) * height) // self.region_rows
        return x0, y0, x1, y1

