from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .config import WorkbenchConfig
from .errors import WorkbenchError
from .pattern import PatternSynthesizer


@dataclass(frozen=True)
class TileSpec:
    row: int
    col: int
    origin_x: int
    origin_y: int
    width: int
    height: int

    @property
    def end_x(self) -> int:
        return self.origin_x + self.width

    @property
    def end_y(self) -> int:
        return self.origin_y + self.height

    @property
    def pixel_count(self) -> int:
        return self.width * self.height

    @property
    def name(self) -> str:
        return f"r{self.row:03d}_c{self.col:03d}"

    def to_dict(self) -> dict[str, int]:
        return {
            "row": self.row,
            "col": self.col,
            "origin_x": self.origin_x,
            "origin_y": self.origin_y,
            "width": self.width,
            "height": self.height,
            "pixel_count": self.pixel_count,
        }


@dataclass(frozen=True)
class GlobalStats:
    minimum: float
    maximum: float
    pixel_count: int

    @property
    def span(self) -> float:
        return self.maximum - self.minimum

    def digest(self) -> str:
        payload = json.dumps(
            {
                "minimum": float.hex(self.minimum),
                "maximum": float.hex(self.maximum),
                "pixel_count": self.pixel_count,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "minimum": self.minimum,
            "maximum": self.maximum,
            "pixel_count": self.pixel_count,
            "minimum_hex": float.hex(self.minimum),
            "maximum_hex": float.hex(self.maximum),
            "sha256": self.digest(),
        }

    @classmethod
    def from_dict(cls, data: object) -> "GlobalStats | None":
        if not isinstance(data, dict):
            return None
        try:
            minimum = float.fromhex(str(data["minimum_hex"]))
            maximum = float.fromhex(str(data["maximum_hex"]))
            pixel_count = int(data["pixel_count"])
            claimed_digest = str(data["sha256"])
        except (KeyError, TypeError, ValueError):
            return None
        stats = cls(minimum=minimum, maximum=maximum, pixel_count=pixel_count)
        if not (np.isfinite(minimum) and np.isfinite(maximum)):
            return None
        if pixel_count <= 0 or maximum < minimum:
            return None
        if stats.digest() != claimed_digest:
            return None
        return stats


@dataclass(frozen=True)
class TileDiagnostics:
    local_normalization: np.ndarray
    local_gradient: np.ndarray
    local_phase: np.ndarray


class ReliefRenderer:
    """One source of truth for full-canvas and tile rendering."""

    STATS_CHUNK_ROWS = 64

    def __init__(
        self,
        config: WorkbenchConfig,
        synthesizer: PatternSynthesizer | None = None,
    ) -> None:
        self.config = config
        self.synthesizer = synthesizer or PatternSynthesizer(config)
        self._specs = self._build_specs()

    def _build_specs(self) -> tuple[TileSpec, ...]:
        specs: list[TileSpec] = []
        for row in range(self.config.grid_rows):
            for col in range(self.config.grid_cols):
                specs.append(
                    TileSpec(
                        row=row,
                        col=col,
                        origin_x=col * self.config.tile_width,
                        origin_y=row * self.config.tile_height,
                        width=self.config.tile_width,
                        height=self.config.tile_height,
                    )
                )
        return tuple(specs)

    @property
    def specs(self) -> tuple[TileSpec, ...]:
        return self._specs

    def spec(self, row: int, col: int) -> TileSpec:
        if not (0 <= row < self.config.grid_rows):
            raise WorkbenchError(
                "E_TILE_INDEX",
                "tile row is outside the grid",
                {"row": row, "grid_rows": self.config.grid_rows},
            )
        if not (0 <= col < self.config.grid_cols):
            raise WorkbenchError(
                "E_TILE_INDEX",
                "tile column is outside the grid",
                {"col": col, "grid_cols": self.config.grid_cols},
            )
        return self._specs[row * self.config.grid_cols + col]

    def spec_for_pixel(self, x: int, y: int) -> TileSpec:
        if not (0 <= x < self.config.width and 0 <= y < self.config.height):
            raise WorkbenchError(
                "E_PIXEL_INDEX",
                "pixel coordinate is outside the canvas",
                {"x": x, "y": y},
            )
        return self.spec(y // self.config.tile_height, x // self.config.tile_width)

    def _height_patch(
        self, x0: int, x1: int, y0: int, y1: int
    ) -> np.ndarray:
        return self.synthesizer.height_patch(x0, x1, y0, y1)

    def raw_light_patch(
        self, x0: int, x1: int, y0: int, y1: int
    ) -> np.ndarray:
        """Return unnormalized directional-light values for a global rectangle."""

        if x1 <= x0 or y1 <= y0:
            raise ValueError("empty render rectangle")
        extended_x0 = max(0, x0 - 1)
        extended_x1 = min(self.config.width, x1 + 1)
        extended_y0 = max(0, y0 - 1)
        extended_y1 = min(self.config.height, y1 + 1)
        height = self._height_patch(
            extended_x0, extended_x1, extended_y0, extended_y1
        )

        xs = np.arange(x0, x1, dtype=np.int64)
        ys = np.arange(y0, y1, dtype=np.int64)
        row_indices = ys - extended_y0
        column_indices = xs - extended_x0
        left_indices = np.clip(xs - 1, 0, self.config.width - 1) - extended_x0
        right_indices = np.clip(xs + 1, 0, self.config.width - 1) - extended_x0
        up_indices = np.clip(ys - 1, 0, self.config.height - 1) - extended_y0
        down_indices = np.clip(ys + 1, 0, self.config.height - 1) - extended_y0

        left = height[np.ix_(row_indices, left_indices)]
        right = height[np.ix_(row_indices, right_indices)]
        up = height[np.ix_(up_indices, column_indices)]
        down = height[np.ix_(down_indices, column_indices)]

        denom_x = (
            (xs > 0).astype(np.float64) + (xs < self.config.width - 1).astype(np.float64)
        )
        denom_y = (
            (ys > 0).astype(np.float64) + (ys < self.config.height - 1).astype(np.float64)
        )
        gradient_x = np.zeros((ys.size, xs.size), dtype=np.float64)
        gradient_y = np.zeros_like(gradient_x)
        np.divide(
            right - left,
            denom_x[None, :],
            out=gradient_x,
            where=denom_x[None, :] > 0.0,
        )
        np.divide(
            down - up,
            denom_y[:, None],
            out=gradient_y,
            where=denom_y[:, None] > 0.0,
        )

        lx, ly, lz = self.config.light_direction
        return lz - self.config.relief_strength * (
            gradient_x * lx + gradient_y * ly
        )

    def raw_light_local_gradient(self, spec: TileSpec) -> np.ndarray:
        """Diagnostic variant: gradients clipped at each tile boundary."""

        height = self._height_patch(
            spec.origin_x, spec.end_x, spec.origin_y, spec.end_y
        )
        xs = np.arange(spec.width)
        ys = np.arange(spec.height)
        left = height[:, np.clip(xs - 1, 0, spec.width - 1)]
        right = height[:, np.clip(xs + 1, 0, spec.width - 1)]
        up = height[np.clip(ys - 1, 0, spec.height - 1), :]
        down = height[np.clip(ys + 1, 0, spec.height - 1), :]
        denom_x = (xs > 0).astype(np.float64) + (
            xs < spec.width - 1
        ).astype(np.float64)
        denom_y = (ys > 0).astype(np.float64) + (
            ys < spec.height - 1
        ).astype(np.float64)
        gradient_x = np.zeros_like(height)
        gradient_y = np.zeros_like(height)
        np.divide(
            right - left,
            denom_x[None, :],
            out=gradient_x,
            where=denom_x[None, :] > 0.0,
        )
        np.divide(
            down - up,
            denom_y[:, None],
            out=gradient_y,
            where=denom_y[:, None] > 0.0,
        )
        lx, ly, lz = self.config.light_direction
        return lz - self.config.relief_strength * (
            gradient_x * lx + gradient_y * ly
        )

    def raw_light_local_phase(self, spec: TileSpec) -> np.ndarray:
        """Diagnostic variant: noise phases restart at the tile origin."""

        height = self.synthesizer.height(
            np.arange(spec.width, dtype=np.float64),
            np.arange(spec.height, dtype=np.float64),
        )
        xs = np.arange(spec.width)
        ys = np.arange(spec.height)
        left = height[:, np.clip(xs - 1, 0, spec.width - 1)]
        right = height[:, np.clip(xs + 1, 0, spec.width - 1)]
        up = height[np.clip(ys - 1, 0, spec.height - 1), :]
        down = height[np.clip(ys + 1, 0, spec.height - 1), :]
        denom_x = (xs > 0).astype(np.float64) + (
            xs < spec.width - 1
        ).astype(np.float64)
        denom_y = (ys > 0).astype(np.float64) + (
            ys < spec.height - 1
        ).astype(np.float64)
        gradient_x = np.zeros_like(height)
        gradient_y = np.zeros_like(height)
        np.divide(
            right - left,
            denom_x[None, :],
            out=gradient_x,
            where=denom_x[None, :] > 0.0,
        )
        np.divide(
            down - up,
            denom_y[:, None],
            out=gradient_y,
            where=denom_y[:, None] > 0.0,
        )
        lx, ly, lz = self.config.light_direction
        return lz - self.config.relief_strength * (
            gradient_x * lx + gradient_y * ly
        )

    @staticmethod
    def quantize(raw: np.ndarray, stats: GlobalStats) -> np.ndarray:
        if stats.span <= 0.0:
            return np.zeros(raw.shape, dtype=np.uint8)
        normalized = (raw - stats.minimum) * (255.0 / stats.span)
        quantized = np.floor(normalized + 0.5)
        return np.clip(quantized, 0.0, 255.0).astype(np.uint8)

    def compute_global_stats(
        self,
        should_stop: Callable[[], bool] | None = None,
    ) -> GlobalStats:
        minimum = np.inf
        maximum = -np.inf
        for y0 in range(0, self.config.height, self.STATS_CHUNK_ROWS):
            if should_stop is not None and should_stop():
                raise WorkbenchError(
                    "E_INTERRUPTED",
                    "rendering was interrupted while computing global statistics",
                    exit_code=130,
                )
            y1 = min(self.config.height, y0 + self.STATS_CHUNK_ROWS)
            raw = self.raw_light_patch(0, self.config.width, y0, y1)
            minimum = min(minimum, float(np.min(raw)))
            maximum = max(maximum, float(np.max(raw)))
        return GlobalStats(
            minimum=minimum,
            maximum=maximum,
            pixel_count=self.config.width * self.config.height,
        )

    def render_tile(self, spec: TileSpec, stats: GlobalStats) -> np.ndarray:
        raw = self.raw_light_patch(spec.origin_x, spec.end_x, spec.origin_y, spec.end_y)
        return self.quantize(raw, stats)

    def render_full(self, stats: GlobalStats) -> np.ndarray:
        raw = self.raw_light_patch(
            0, self.config.width, 0, self.config.height
        )
        return self.quantize(raw, stats)

    def full_raw_span(self) -> tuple[float, float]:
        raw = self.raw_light_patch(
            0, self.config.width, 0, self.config.height
        )
        return float(np.min(raw)), float(np.max(raw))

    def diagnostics(self, spec: TileSpec, stats: GlobalStats) -> TileDiagnostics:
        raw_global = self.raw_light_patch(
            spec.origin_x, spec.end_x, spec.origin_y, spec.end_y
        )
        local_min = float(np.min(raw_global))
        local_max = float(np.max(raw_global))
        local_stats = GlobalStats(local_min, local_max, raw_global.size)
        return TileDiagnostics(
            local_normalization=self.quantize(raw_global, local_stats),
            local_gradient=self.quantize(
                self.raw_light_local_gradient(spec), stats
            ),
            local_phase=self.quantize(self.raw_light_local_phase(spec), stats),
        )
