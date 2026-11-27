"""Independent reference calculator for RoPE angles and encoded values."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .models import BandCombination, NormalizedBlock, NormalizedRequest


def _selected_bands(
    block: NormalizedBlock, combination: BandCombination
) -> list[tuple[Any, tuple[int, ...]]]:
    channel_filter = (
        set(combination.channel_filter)
        if combination.channel_filter is not None
        else None
    )
    result: list[tuple[Any, tuple[int, ...]]] = []
    by_name = {band.name: band for band in block.bands}
    for band_name in combination.band_names:
        band = by_name[band_name]
        channels = tuple(
            channel
            for channel in band.channels
            if channel_filter is None or channel in channel_filter
        )
        if channels:
            result.append((band, channels))
    return result


def _reference_theta(
    band: Any, channel: int, x: float, y: float
) -> float:
    local_index = band.channels.index(channel)
    pair_id = local_index // 2
    if band.pair_frequencies is None:
        frequency_x = band.axis[0]
        frequency_y = band.axis[1]
    else:
        frequency_x = float(band.pair_frequencies[pair_id, 0])
        frequency_y = float(band.pair_frequencies[pair_id, 1])
    exponent = math.pow(
        band.angle_base,
        -2.0 * pair_id / float(len(band.channels)),
    )
    return (
        band.frequency_scale
        * (frequency_x * x + frequency_y * y)
        * exponent
    )


def reference_block(
    block: NormalizedBlock, combination: BandCombination
) -> tuple[np.ndarray, np.ndarray, tuple[int, ...]]:
    x, y = block.positions()
    x_flat = x.reshape(-1)
    y_flat = y.reshape(-1)
    channel_count = block.channels
    angles = np.full((x_flat.size, channel_count), np.nan, dtype=np.float64)
    encoded = block.data.reshape(-1, channel_count).astype(np.float64).copy()
    selected = _selected_bands(block, combination)
    active_channels: list[int] = []
    for band, channels in selected:
        active_channels.extend(channels)
        for pair_id in range(len(band.channels) // 2):
            left_channel = band.channels[pair_id * 2]
            right_channel = left_channel + 1
            if left_channel not in channels:
                continue
            for position_index in range(x_flat.size):
                theta = _reference_theta(
                    band,
                    left_channel,
                    float(x_flat[position_index]),
                    float(y_flat[position_index]),
                )
                cosine = math.cos(theta)
                sine = math.sin(theta)
                left_value = float(encoded[position_index, left_channel])
                right_value = float(encoded[position_index, right_channel])
                encoded[position_index, left_channel] = (
                    left_value * cosine - right_value * sine
                )
                encoded[position_index, right_channel] = (
                    left_value * sine + right_value * cosine
                )
                angles[position_index, left_channel] = theta
                angles[position_index, right_channel] = theta
    return angles, encoded.reshape(block.rows, block.cols, channel_count), tuple(
        sorted(active_channels)
    )


def verify_against_reference(
    request: NormalizedRequest,
    production_encodings: list[Any],
    tolerance: float,
) -> dict[str, Any]:
    all_mismatches: list[dict[str, Any]] = []
    per_combination: dict[str, Any] = {}
    checked_positions = 0
    checked_angles = 0
    checked_encoded = 0
    max_angle_error = 0.0
    max_encoded_error = 0.0
    for combination in request.combinations:
        combination_mismatches: list[dict[str, Any]] = []
        for encoding in production_encodings:
            if encoding.combination != combination.name:
                continue
            block = encoding.block
            reference_angles, reference_encoded, reference_active = reference_block(
                block, combination
            )
            if tuple(encoding.active_channels) != reference_active:
                combination_mismatches.append(
                    {
                        "combination": combination.name,
                        "schema_version": block.schema_version,
                        "block_id": block.block_id,
                        "kind": "active_channel_selection",
                        "production": list(encoding.active_channels),
                        "reference": list(reference_active),
                    }
                )
            production_values = encoding.encoded.reshape(-1, block.channels)
            reference_values = reference_encoded.reshape(-1, block.channels)
            for position_index in range(encoding.x.size):
                checked_positions += 1
                row = int(encoding.row_indices[position_index])
                col = int(encoding.col_indices[position_index])
                x = float(encoding.x.reshape(-1)[position_index])
                y = float(encoding.y.reshape(-1)[position_index])
                for channel in range(block.channels):
                    production_angle = float(encoding.angles[position_index, channel])
                    reference_angle = float(reference_angles[position_index, channel])
                    angle_finite = math.isfinite(production_angle) and math.isfinite(
                        reference_angle
                    )
                    if angle_finite:
                        checked_angles += 1
                        angle_error = abs(production_angle - reference_angle)
                        max_angle_error = max(max_angle_error, angle_error)
                    else:
                        angle_error = None if (
                            not math.isfinite(production_angle)
                            and not math.isfinite(reference_angle)
                        ) else math.inf
                    production_value = float(production_values[position_index, channel])
                    reference_value = float(reference_values[position_index, channel])
                    checked_encoded += 1
                    encoded_error = abs(production_value - reference_value)
                    max_encoded_error = max(max_encoded_error, encoded_error)
                    angle_failed = angle_error is not None and angle_error > tolerance
                    encoded_failed = encoded_error > tolerance
                    if angle_failed or encoded_failed:
                        combination_mismatches.append(
                            {
                                "combination": combination.name,
                                "schema_version": block.schema_version,
                                "block_id": block.block_id,
                                "row": row,
                                "col": col,
                                "x": x,
                                "y": y,
                                "channel": channel,
                                "angle_production": production_angle
                                if math.isfinite(production_angle)
                                else None,
                                "angle_reference": reference_angle
                                if math.isfinite(reference_angle)
                                else None,
                                "angle_error": angle_error,
                                "encoded_production": production_value,
                                "encoded_reference": reference_value,
                                "encoded_error": encoded_error,
                                "failed_checks": [
                                    name
                                    for name, failed in (
                                        ("angle", angle_failed),
                                        ("encoded_feature", encoded_failed),
                                    )
                                    if failed
                                ],
                            }
                        )
        per_combination[combination.name] = {
            "mismatch_count": len(combination_mismatches),
            "consistent": not combination_mismatches,
            "mismatches": combination_mismatches,
        }
        all_mismatches.extend(combination_mismatches)
    return {
        "tolerance": tolerance,
        "checked_combinations": len(request.combinations),
        "checked_positions": checked_positions,
        "checked_angle_elements": checked_angles,
        "checked_encoded_elements": checked_encoded,
        "mismatch_count": len(all_mismatches),
        "consistent": not all_mismatches,
        "max_angle_error": max_angle_error,
        "max_encoded_error": max_encoded_error,
        "mismatches": all_mismatches,
        "combination_reports": per_combination,
    }
