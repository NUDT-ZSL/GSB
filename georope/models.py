"""Internal normalized models used by the encoder."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class FrequencyBand:
    name: str
    channels: tuple[int, ...]
    angle_base: float
    frequency_scale: float
    axis: tuple[float, float]
    pair_frequencies: np.ndarray | None
    source: dict[str, Any] = field(default_factory=dict)

    @property
    def pair_count(self) -> int:
        return len(self.channels) // 2

    @property
    def pair_exponents(self) -> np.ndarray:
        pair_ids = np.arange(self.pair_count, dtype=np.float64)
        return np.power(self.angle_base, (-2.0 * pair_ids / len(self.channels)))

    def pair_frequency_vectors(self) -> np.ndarray:
        if self.pair_frequencies is not None:
            return self.pair_frequencies
        vectors = np.empty((self.pair_count, 2), dtype=np.float64)
        vectors[:, 0] = self.axis[0]
        vectors[:, 1] = self.axis[1]
        return vectors

    def angles(self, x: np.ndarray, y: np.ndarray) -> np.ndarray:
        x_array = np.asarray(x, dtype=np.float64).reshape(-1)
        y_array = np.asarray(y, dtype=np.float64).reshape(-1)
        if x_array.shape != y_array.shape:
            raise ValueError("x and y arrays must have the same shape")
        vectors = self.pair_frequency_vectors()
        exponents = self.pair_exponents
        result = np.empty((x_array.size, len(self.channels)), dtype=np.float64)
        for pair_id in range(self.pair_count):
            phase = (
                self.frequency_scale
                * (
                    vectors[pair_id, 0] * x_array
                    + vectors[pair_id, 1] * y_array
                )
                * exponents[pair_id]
            )
            left_channel = pair_id * 2
            right_channel = left_channel + 1
            result[:, left_channel] = phase
            result[:, right_channel] = phase
        return result

    def pair_map(self) -> dict[int, int]:
        return {
            int(channel): pair_id
            for pair_id in range(self.pair_count)
            for channel in self.channels[pair_id * 2 : pair_id * 2 + 2]
        }

    def summary(self) -> dict[str, Any]:
        vectors = self.pair_frequency_vectors()
        return {
            "name": self.name,
            "channels": list(self.channels),
            "pairs": [
                [int(self.channels[pair_id * 2]), int(self.channels[pair_id * 2 + 1])]
                for pair_id in range(self.pair_count)
            ],
            "angle_base": self.angle_base,
            "frequency_scale": self.frequency_scale,
            "axis": list(self.axis),
            "pair_frequency_vectors": vectors.tolist(),
            "effective_pair_frequency_vectors": (
                vectors * self.frequency_scale
            ).tolist(),
            "pair_exponents": self.pair_exponents.tolist(),
            "source": self.source,
        }


@dataclass
class NormalizedBlock:
    block_id: str
    schema_version: str
    tile_set_id: str
    tile_key: str
    data: np.ndarray
    local_row_offset: int
    local_col_offset: int
    origin_m: tuple[float, float]
    resolution_m: tuple[float, float]
    raw_origin: tuple[float, float]
    origin_unit: str
    pixel_anchor: str
    bands: list[FrequencyBand]
    block_source: dict[str, Any] = field(default_factory=dict)

    @property
    def shape(self) -> tuple[int, int, int]:
        return (
            int(self.data.shape[0]),
            int(self.data.shape[1]),
            int(self.data.shape[2]),
        )

    @property
    def rows(self) -> int:
        return self.shape[0]

    @property
    def cols(self) -> int:
        return self.shape[1]

    @property
    def channels(self) -> int:
        return self.shape[2]

    @property
    def local_row_indices(self) -> np.ndarray:
        return np.arange(self.local_row_offset, self.local_row_offset + self.rows)

    @property
    def local_col_indices(self) -> np.ndarray:
        return np.arange(self.local_col_offset, self.local_col_offset + self.cols)

    def axis_positions(self) -> tuple[np.ndarray, np.ndarray]:
        row_shift = 0.5 if self.pixel_anchor == "center" else 0.0
        col_shift = 0.5 if self.pixel_anchor == "center" else 0.0
        y = self.origin_m[1] + (
            self.local_row_indices.astype(np.float64) + row_shift
        ) * self.resolution_m[1]
        x = self.origin_m[0] + (
            self.local_col_indices.astype(np.float64) + col_shift
        ) * self.resolution_m[0]
        return x, y

    def positions(self) -> tuple[np.ndarray, np.ndarray]:
        x_axis, y_axis = self.axis_positions()
        x, y = np.meshgrid(x_axis, y_axis)
        return x, y

    def extent_m(self) -> tuple[float, float, float, float]:
        xmin = self.origin_m[0] + self.local_col_offset * self.resolution_m[0]
        xmax = xmin + self.cols * self.resolution_m[0]
        ymin = self.origin_m[1] + self.local_row_offset * self.resolution_m[1]
        ymax = ymin + self.rows * self.resolution_m[1]
        return xmin, xmax, ymin, ymax

    def band_for_channel(self, channel: int) -> FrequencyBand:
        for band in self.bands:
            if channel in band.channels:
                return band
        raise KeyError(channel)

    def summary(self) -> dict[str, Any]:
        extent = self.extent_m()
        converted = self.origin_unit != "meters"
        return {
            "block_id": self.block_id,
            "schema_version": self.schema_version,
            "tile_set_id": self.tile_set_id,
            "tile_key": self.tile_key,
            "shape": list(self.shape),
            "local_row_range": [
                self.local_row_offset,
                self.local_row_offset + self.rows - 1,
            ],
            "local_col_range": [
                self.local_col_offset,
                self.local_col_offset + self.cols - 1,
            ],
            "raw_origin": list(self.raw_origin),
            "origin_unit": self.origin_unit,
            "origin_was_converted": converted,
            "geographic_origin_m": list(self.origin_m),
            "resolution_m": list(self.resolution_m),
            "pixel_anchor": self.pixel_anchor,
            "extent_m": list(extent),
            "channel_groups": [band.summary() for band in self.bands],
            "source": self.block_source,
        }


@dataclass(frozen=True)
class BandCombination:
    name: str
    band_names: tuple[str, ...]
    channel_filter: tuple[int, ...] | None = None
    source: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bands": list(self.band_names),
            "channel_filter": list(self.channel_filter)
            if self.channel_filter is not None
            else None,
            "source": self.source,
        }


@dataclass
class NormalizedRequest:
    request_id: str
    blocks: list[NormalizedBlock]
    combinations: list[BandCombination]
    tolerance: float
    phase_threshold: float
    pixel_anchor: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def schema_versions(self) -> list[str]:
        return sorted({block.schema_version for block in self.blocks})

    def blocks_for_version(self, version: str) -> list[NormalizedBlock]:
        return [
            block for block in self.blocks if block.schema_version == version
        ]

    def blocks_for_set(self, version: str, tile_set_id: str) -> list[NormalizedBlock]:
        return [
            block
            for block in self.blocks
            if block.schema_version == version
            and block.tile_set_id == tile_set_id
        ]

    def combination(self, name: str) -> BandCombination:
        for combination in self.combinations:
            if combination.name == name:
                return combination
        raise KeyError(name)

    def summary(self) -> dict[str, Any]:
        versions: dict[str, Any] = {}
        for version in self.schema_versions:
            version_blocks = self.blocks_for_version(version)
            versions[version] = {
                "block_count": len(version_blocks),
                "position_count": sum(
                    block.rows * block.cols for block in version_blocks
                ),
                "channel_count": sorted(
                    {block.channels for block in version_blocks}
                ),
                "tile_sets": sorted(
                    {block.tile_set_id for block in version_blocks}
                ),
                "origin_units": sorted(
                    {block.origin_unit for block in version_blocks}
                ),
                "origin_conversion_blocks": [
                    block.block_id
                    for block in version_blocks
                    if block.origin_unit != "meters"
                ],
                "blocks": [block.summary() for block in version_blocks],
            }
        return {
            "request_id": self.request_id,
            "coordinate_model": {
                "geographic_unit": "meters",
                "position_anchor": {
                    "center": "pixel_center",
                    "corner": "pixel_corner",
                }.get(self.pixel_anchor, self.pixel_anchor),
                "origin_conversion": "origin_m = origin_raw * resolution_m",
                "channel_model": "canonical 0-based channel indices",
                "angle_formula": (
                    "theta = frequency_scale * (fx*x + fy*y) * "
                    "angle_base^(-2*pair_ordinal/channel_group_size)"
                ),
            },
            "schema_versions": versions,
            "band_combinations": [
                combination.summary() for combination in self.combinations
            ],
            "tolerance": self.tolerance,
            "phase_threshold": self.phase_threshold,
        }
