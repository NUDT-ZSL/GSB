"""Rotary position encoding over normalized geospatial feature blocks."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import BusinessError
from .jsonutil import stable_hash
from .models import BandCombination, NormalizedBlock, NormalizedRequest


@dataclass
class ActiveBand:
    band: Any
    channels: tuple[int, ...]


@dataclass
class BlockEncoding:
    combination: str
    block: NormalizedBlock
    x: np.ndarray
    y: np.ndarray
    row_indices: np.ndarray
    col_indices: np.ndarray
    active_channels: tuple[int, ...]
    active_bands: tuple[ActiveBand, ...]
    angles: np.ndarray
    encoded: np.ndarray

    def positions(self) -> list[dict[str, Any]]:
        x_flat = self.x.reshape(-1)
        y_flat = self.y.reshape(-1)
        rows = np.repeat(self.row_indices, self.block.cols)
        cols = np.tile(self.col_indices, self.block.rows)
        return [
            {
                "row": int(row),
                "col": int(col),
                "x": float(x_value),
                "y": float(y_value),
            }
            for row, col, x_value, y_value in zip(rows, cols, x_flat, y_flat)
        ]

    def angle_values(self) -> list[list[float | None]]:
        return [
            [
                None if not np.isfinite(angle) else float(angle)
                for angle in position_angles
            ]
            for position_angles in self.angles
        ]

    def encoded_values(self) -> list[list[float]]:
        return [
            [
                [float(value) for value in channel]
                for channel in position_row
            ]
            for position_row in self.encoded
        ]

    def record(self) -> dict[str, Any]:
        return {
            "combination": self.combination,
            "schema_version": self.block.schema_version,
            "block_id": self.block.block_id,
            "tile_set_id": self.block.tile_set_id,
            "tile_key": self.block.tile_key,
            "shape": list(self.block.shape),
            "active_channels": list(self.active_channels),
            "positions": self.positions(),
            "angles": self.angle_values(),
            "encoded_features": self.encoded_values(),
        }


def active_bands_for_block(
    block: NormalizedBlock, combination: BandCombination
) -> tuple[ActiveBand, ...]:
    by_name = {band.name: band for band in block.bands}
    active: list[ActiveBand] = []
    filter_set = set(combination.channel_filter) if combination.channel_filter else None
    for band_name in combination.band_names:
        band = by_name[band_name]
        channels = band.channels
        if filter_set is not None:
            channels = tuple(channel for channel in channels if channel in filter_set)
        if channels:
            active.append(ActiveBand(band=band, channels=channels))
    return tuple(active)


def active_channels_for_block(
    block: NormalizedBlock, combination: BandCombination
) -> tuple[int, ...]:
    channels = sorted(
        channel
        for active in active_bands_for_block(block, combination)
        for channel in active.channels
    )
    return tuple(channels)


def channel_angle(block: NormalizedBlock, channel: int, x: float, y: float) -> float:
    band = block.band_for_channel(channel)
    local_index = band.channels.index(channel)
    values = band.angles(
        np.asarray([x], dtype=np.float64),
        np.asarray([y], dtype=np.float64),
    )
    return float(values[0, local_index])


def pair_signature(block: NormalizedBlock, channel: int) -> tuple[Any, ...]:
    band = block.band_for_channel(channel)
    local_index = band.channels.index(channel)
    pair_id = local_index // 2
    pair_channels = band.channels[pair_id * 2 : pair_id * 2 + 2]
    return band.name, pair_channels, band.channels


def frequency_signature(block: NormalizedBlock, channel: int) -> tuple[Any, ...]:
    band = block.band_for_channel(channel)
    vectors = band.pair_frequency_vectors()
    pair_id = band.channels.index(channel) // 2
    return (
        band.angle_base,
        band.frequency_scale,
        band.axis,
        tuple(float(item) for item in vectors[pair_id]),
        len(band.channels),
    )


def encode_block(
    block: NormalizedBlock, combination: BandCombination
) -> BlockEncoding:
    x, y = block.positions()
    row_indices = np.repeat(block.local_row_indices, block.cols)
    col_indices = np.tile(block.local_col_indices, block.rows)
    x_flat = x.reshape(-1)
    y_flat = y.reshape(-1)
    active_bands = active_bands_for_block(block, combination)
    active_channels = tuple(
        sorted(channel for active in active_bands for channel in active.channels)
    )
    angles = np.full((x_flat.size, block.channels), np.nan, dtype=np.float64)
    encoded = block.data.reshape(-1, block.channels).copy()

    for active in active_bands:
        band = active.band
        band_angles = band.angles(x_flat, y_flat)
        pair_vectors = band.pair_frequency_vectors()
        for pair_id in range(band.pair_count):
            left_channel = band.channels[pair_id * 2]
            right_channel = band.channels[pair_id * 2 + 1]
            if left_channel not in active.channels:
                continue
            theta = band_angles[:, pair_id * 2]
            left_values = encoded[:, left_channel].copy()
            right_values = encoded[:, right_channel].copy()
            cosine = np.cos(theta)
            sine = np.sin(theta)
            encoded[:, left_channel] = left_values * cosine - right_values * sine
            encoded[:, right_channel] = left_values * sine + right_values * cosine
            angles[:, left_channel] = theta
            angles[:, right_channel] = theta
            if not np.all(np.isfinite(pair_vectors[pair_id])):
                raise BusinessError(
                    "FREQUENCY_PARAMETER_INVALID",
                    "frequency vector contains non-finite values",
                    [{"block_id": block.block_id}],
                )

    return BlockEncoding(
        combination=combination.name,
        block=block,
        x=x,
        y=y,
        row_indices=row_indices,
        col_indices=col_indices,
        active_channels=active_channels,
        active_bands=active_bands,
        angles=angles,
        encoded=encoded.reshape(block.rows, block.cols, block.channels),
    )


def effective_summary(
    encoding: BlockEncoding, tolerance: float, phase_threshold: float
) -> dict[str, Any]:
    active_angles = np.asarray(
        [
            [
                encoding.angles[position_index, channel]
                for channel in encoding.active_channels
            ]
            for position_index in range(encoding.angles.shape[0])
        ],
        dtype=np.float64,
    )
    finite = active_angles[np.isfinite(active_angles)]
    encoded_hash = stable_hash(
        {
            "shape": list(encoding.encoded.shape),
            "active_channels": list(encoding.active_channels),
            "values": encoding.encoded[:, :, list(encoding.active_channels)],
        }
    )
    return {
        "combination": encoding.combination,
        "schema_version": encoding.block.schema_version,
        "block_id": encoding.block.block_id,
        "active_channels": list(encoding.active_channels),
        "position_count": int(encoding.x.size),
        "angle_count": int(finite.size),
        "angle_min": float(finite.min()) if finite.size else None,
        "angle_max": float(finite.max()) if finite.size else None,
        "angle_mean": float(finite.mean()) if finite.size else None,
        "encoded_hash": encoded_hash,
        "tolerance": tolerance,
        "phase_threshold": phase_threshold,
    }


def build_field(
    blocks: list[NormalizedBlock],
    combination: BandCombination,
    version: str,
    tile_set_id: str,
    tolerance: float,
    phase_threshold: float,
) -> dict[str, Any]:
    if not blocks:
        raise BusinessError(
            "EMPTY_TILE_SET",
            "no blocks are available for field construction",
            [{"schema_version": version, "tile_set_id": tile_set_id}],
        )
    encodings = [encode_block(block, combination) for block in blocks]
    resolutions = [
        (
            encoding.block.resolution_m[0],
            encoding.block.resolution_m[1],
        )
        for encoding in encodings
    ]
    base_resolution = resolutions[0]
    for index, resolution in enumerate(resolutions[1:], start=1):
        if not (
            math.isclose(resolution[0], base_resolution[0], abs_tol=tolerance)
            and math.isclose(resolution[1], base_resolution[1], abs_tol=tolerance)
        ):
            raise BusinessError(
                "STITCH_RESOLUTION_MISMATCH",
                "adjacent blocks use different resolutions and cannot form one raster field",
                [
                    {
                        "schema_version": version,
                        "tile_set_id": tile_set_id,
                        "blocks": [encodings[0].block.block_id, encodings[index].block.block_id],
                        "resolutions_m": [list(base_resolution), list(resolution)],
                    }
                ],
            )

    all_x = np.concatenate([encoding.x.reshape(-1) for encoding in encodings])
    all_y = np.concatenate([encoding.y.reshape(-1) for encoding in encodings])
    column_span = int(round((float(all_x.max()) - float(all_x.min())) / base_resolution[0]))
    row_span = int(round((float(all_y.max()) - float(all_y.min())) / base_resolution[1]))
    columns = column_span + 1
    rows = row_span + 1
    channel_count = encodings[0].block.channels
    encoded_field = np.full((rows, columns, channel_count), np.nan, dtype=np.float64)
    angle_field = np.full((rows, columns, channel_count), np.nan, dtype=np.float64)
    occupied: dict[tuple[int, int], str] = {}
    block_offsets: dict[str, dict[str, Any]] = {}

    for encoding in encodings:
        x_flat = encoding.x.reshape(-1)
        y_flat = encoding.y.reshape(-1)
        col_positions = np.rint((x_flat - float(all_x.min())) / base_resolution[0]).astype(int)
        row_positions = np.rint((y_flat - float(all_y.min())) / base_resolution[1]).astype(int)
        if np.any(col_positions < 0) or np.any(col_positions >= columns):
            raise BusinessError(
                "STITCH_POSITION_CONFLICT",
                "block position lies outside the reconstructed field",
                [{"block_id": encoding.block.block_id}],
            )
        if np.any(row_positions < 0) or np.any(row_positions >= rows):
            raise BusinessError(
                "STITCH_POSITION_CONFLICT",
                "block position lies outside the reconstructed field",
                [{"block_id": encoding.block.block_id}],
            )
        local_values = encoding.encoded.reshape(-1, channel_count)
        local_angles = encoding.angles
        for position_index, (row_index, col_index) in enumerate(
            zip(row_positions, col_positions)
        ):
            key = (int(row_index), int(col_index))
            if key in occupied:
                raise BusinessError(
                    "STITCH_POSITION_CONFLICT",
                    "two blocks contain the same geographic raster position",
                    [
                        {
                            "schema_version": version,
                            "tile_set_id": tile_set_id,
                            "blocks": [occupied[key], encoding.block.block_id],
                            "grid_index": [key[0], key[1]],
                            "geographic_position": [
                                float(x_flat[position_index]),
                                float(y_flat[position_index]),
                            ],
                        }
                    ],
                )
            occupied[key] = encoding.block.block_id
            encoded_field[row_index, col_index, :] = local_values[position_index, :]
            angle_field[row_index, col_index, :] = local_angles[position_index, :]
        block_offsets[encoding.block.block_id] = {
            "row_start": int(row_positions.min()),
            "row_end": int(row_positions.max()),
            "col_start": int(col_positions.min()),
            "col_end": int(col_positions.max()),
        }

    def nullable(values: np.ndarray) -> Any:
        return [
            [
                [
                    None if not np.isfinite(value) else float(value)
                    for value in channel_values
                ]
                for channel_values in row_values
            ]
            for row_values in values
        ]

    position_records = []
    for encoding in encodings:
        for position in encoding.positions():
            row_index = int(
                round((position["y"] - float(all_y.min())) / base_resolution[1])
            )
            col_index = int(
                round((position["x"] - float(all_x.min())) / base_resolution[0])
            )
            position_records.append(
                {
                    "block_id": encoding.block.block_id,
                    "row": position["row"],
                    "col": position["col"],
                    "field_row": row_index,
                    "field_col": col_index,
                    "x": position["x"],
                    "y": position["y"],
                }
            )
    position_records.sort(key=lambda item: (item["field_row"], item["field_col"]))
    summaries = [
        effective_summary(encoding, tolerance, phase_threshold)
        for encoding in encodings
    ]
    field_hash = stable_hash(
        {
            "origin": [float(all_x.min()), float(all_y.min())],
            "resolution": list(base_resolution),
            "shape": [rows, columns, channel_count],
            "values": encoded_field,
            "angles": angle_field,
        }
    )
    return {
        "combination": combination.name,
        "schema_version": version,
        "tile_set_id": tile_set_id,
        "blocks": [encoding.block.block_id for encoding in encodings],
        "shape": [rows, columns, channel_count],
        "origin": [float(all_x.min()), float(all_y.min())],
        "resolution": list(base_resolution),
        "extent_m": [
            float(all_x.min()) - base_resolution[0] * 0.5,
            float(all_x.max()) + base_resolution[0] * 0.5,
            float(all_y.min()) - base_resolution[1] * 0.5,
            float(all_y.max()) + base_resolution[1] * 0.5,
        ],
        "active_channels": sorted(
            set(channel for encoding in encodings for channel in encoding.active_channels)
        ),
        "positions": position_records,
        "rotation_angles": nullable(angle_field),
        "encoded_features": nullable(encoded_field),
        "raw_encoded_field": encoded_field,
        "raw_angle_field": angle_field,
        "block_encodings": encodings,
        "block_offsets": block_offsets,
        "block_summaries": summaries,
        "field_hash": field_hash,
    }


def encode_request_fields(
    request: NormalizedRequest,
    include_tensors: bool = True,
) -> dict[str, Any]:
    fields: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    all_block_encodings: list[BlockEncoding] = []
    version_summaries: dict[str, Any] = {}
    for combination in request.combinations:
        for version in request.schema_versions:
            tile_sets = sorted(
                {
                    block.tile_set_id for block in request.blocks_for_version(version)
                }
            )
            version_summaries.setdefault(version, {})[combination.name] = []
            for tile_set_id in tile_sets:
                blocks = request.blocks_for_set(version, tile_set_id)
                field = build_field(
                    blocks,
                    combination,
                    version,
                    tile_set_id,
                    request.tolerance,
                    request.phase_threshold,
                )
                encodings = field.pop("block_encodings")
                all_block_encodings.extend(encodings)
                field.pop("raw_encoded_field")
                field.pop("raw_angle_field")
                if not include_tensors:
                    field.pop("encoded_features", None)
                    field.pop("rotation_angles", None)
                records.extend(encoding.record() for encoding in encodings)
                field_summary = {
                    "schema_version": version,
                    "combination": combination.name,
                    "tile_set_id": tile_set_id,
                    "shape": field["shape"],
                    "origin": field["origin"],
                    "resolution": field["resolution"],
                    "field_hash": field["field_hash"],
                    "active_channels": field["active_channels"],
                    "blocks": field["blocks"],
                    "block_summaries": field["block_summaries"],
                }
                field["summary"] = field_summary
                fields.append(field)
                version_summaries[version][combination.name].append(field_summary)
    return {
        "fields": fields,
        "rotation_angle_records": records,
        "version_combination_summaries": version_summaries,
        "_block_encodings": all_block_encodings,
    }
