"""Parameter-change impact analysis and selective recomputation records."""

from __future__ import annotations

from typing import Any

import numpy as np

from .continuity import wrap_angle
from .models import BandCombination, NormalizedBlock, NormalizedRequest
from .rope import (
    active_channels_for_block,
    encode_block,
    frequency_signature,
    pair_signature,
)


def _channel_signature(block: NormalizedBlock, channel: int) -> tuple[Any, ...]:
    pair = pair_signature(block, channel)
    frequency = frequency_signature(block, channel)
    band = block.band_for_channel(channel)
    return (
        tuple(pair[1]),
        tuple(pair[2]),
        frequency,
        len(band.channels),
    )


def _match_blocks(
    old_request: NormalizedRequest, new_request: NormalizedRequest
) -> tuple[
    list[tuple[NormalizedBlock, NormalizedBlock]],
    list[NormalizedBlock],
    list[NormalizedBlock],
]:
    matches: list[tuple[NormalizedBlock, NormalizedBlock]] = []
    unmatched_old = list(old_request.blocks)
    unmatched_new = list(new_request.blocks)
    new_by_id = {block.block_id: block for block in new_request.blocks}
    for old_block in list(unmatched_old):
        new_block = new_by_id.get(old_block.block_id)
        if new_block is not None and new_block in unmatched_new:
            matches.append((old_block, new_block))
            unmatched_old.remove(old_block)
            unmatched_new.remove(new_block)
    old_by_tile = {
        (block.tile_set_id, block.tile_key): block for block in unmatched_old
    }
    for new_block in list(unmatched_new):
        key = (new_block.tile_set_id, new_block.tile_key)
        old_block = old_by_tile.get(key)
        if old_block is not None and old_block in unmatched_old:
            matches.append((old_block, new_block))
            unmatched_old.remove(old_block)
            unmatched_new.remove(new_block)
    return matches, unmatched_old, unmatched_new


def _same_geometry(old: NormalizedBlock, new: NormalizedBlock) -> bool:
    return (
        old.origin_m == new.origin_m
        and old.resolution_m == new.resolution_m
        and old.pixel_anchor == new.pixel_anchor
        and old.local_row_offset == new.local_row_offset
        and old.local_col_offset == new.local_col_offset
    )


def _combo_by_name(
    request: NormalizedRequest, name: str
) -> BandCombination | None:
    for combination in request.combinations:
        if combination.name == name:
            return combination
    return None


def _position_angle(
    encoding: Any, position_index: int, channel: int
) -> float | None:
    if channel not in encoding.active_channels:
        return None
    value = float(encoding.angles[position_index, channel])
    return value if np.isfinite(value) else None


def _position_value(
    encoding: Any, position_index: int, channel: int
) -> float | None:
    if channel not in encoding.active_channels:
        return None
    return float(encoding.encoded.reshape(-1, encoding.block.channels)[position_index, channel])


def _data_changed_channels(
    old: NormalizedBlock, new: NormalizedBlock
) -> set[int]:
    if old.data.shape != new.data.shape:
        return set(range(max(old.channels, new.channels)))
    difference_axes = np.any(old.data != new.data, axis=(0, 1))
    return {
        channel
        for channel, changed in enumerate(difference_axes.tolist())
        if changed
    }


def incremental_recompute(
    old_request: NormalizedRequest,
    new_request: NormalizedRequest,
) -> dict[str, Any]:
    matches, removed_blocks, added_blocks = _match_blocks(old_request, new_request)
    combination_reports: list[dict[str, Any]] = []
    matched_change_reports: list[dict[str, Any]] = []
    block_change_reports: list[dict[str, Any]] = []
    total_recomputed = 0
    all_preserved = True

    for old_block, new_block in matches:
        parameter_changes: list[str] = []
        if old_block.origin_m != new_block.origin_m:
            parameter_changes.append("origin")
        if old_block.resolution_m != new_block.resolution_m:
            parameter_changes.append("resolution")
        if old_block.pixel_anchor != new_block.pixel_anchor:
            parameter_changes.append("pixel_anchor")
        if (
            old_block.local_row_offset != new_block.local_row_offset
            or old_block.local_col_offset != new_block.local_col_offset
        ):
            parameter_changes.append("local_index_offset")
        changed_channels = {
            channel
            for channel in range(min(old_block.channels, new_block.channels))
            if _channel_signature(old_block, channel)
            != _channel_signature(new_block, channel)
        }
        if changed_channels:
            parameter_changes.append("channel_group_or_frequency_parameters")
        if old_block.schema_version != new_block.schema_version:
            parameter_changes.append("schema_version")
        data_changed_channels = _data_changed_channels(old_block, new_block)
        if data_changed_channels:
            parameter_changes.append("feature_tensor")
        geometry_changed = not _same_geometry(old_block, new_block) or (
            old_block.data.shape != new_block.data.shape
        )
        affected_channels = (
            set(range(max(old_block.channels, new_block.channels)))
            if geometry_changed
            else changed_channels | data_changed_channels
        )
        if old_block.block_id != new_block.block_id:
            parameter_changes.append("block_id_mapping")
        report = (
            {
                "old_block_id": old_block.block_id,
                "new_block_id": new_block.block_id,
                "schema_versions": [
                    old_block.schema_version,
                    new_block.schema_version,
                ],
                "parameter_changes": parameter_changes,
                "geometry_changed": geometry_changed,
                "affected_channels": sorted(affected_channels),
                "reason": (
                    "block parameters changed; all positions require angle recomputation"
                    if geometry_changed
                    else (
                        "frequency, grouping, or feature values changed for the listed channels"
                        if affected_channels
                        else "normalized semantics are unchanged"
                    )
                ),
            }
        )
        matched_change_reports.append(report)
        block_change_reports.append(report)

    for block in removed_blocks:
        block_change_reports.append(
            {
                "old_block_id": block.block_id,
                "new_block_id": None,
                "parameter_changes": ["block_removed"],
                "geometry_changed": True,
                "affected_channels": list(range(block.channels)),
                "reason": "block removed from the request",
            }
        )
    for block in added_blocks:
        block_change_reports.append(
            {
                "old_block_id": None,
                "new_block_id": block.block_id,
                "parameter_changes": ["block_added"],
                "geometry_changed": True,
                "affected_channels": list(range(block.channels)),
                "reason": "block added to the request",
            }
        )

    for combination in new_request.combinations:
        old_combination = _combo_by_name(old_request, combination.name)
        affected_entries: list[dict[str, Any]] = []
        unchanged_elements = 0
        unchanged_differences: list[dict[str, Any]] = []
        recomputed_channel_positions = 0

        for old_block, new_block, change_report in [
            (old, new, report)
            for (old, new), report in zip(matches, matched_change_reports)
        ]:
            old_encoding = (
                encode_block(old_block, old_combination)
                if old_combination is not None
                else None
            )
            new_encoding = encode_block(new_block, combination)
            old_active = (
                set(active_channels_for_block(old_block, old_combination))
                if old_combination is not None
                else set()
            )
            new_active = set(active_channels_for_block(new_block, combination))
            affected_channels = (
                set(change_report["affected_channels"])
                | (old_active - new_active)
                | (new_active - old_active)
            ) & (old_active | new_active)
            rows = min(old_block.rows, new_block.rows)
            cols = min(old_block.cols, new_block.cols)
            for row in range(rows):
                for col in range(cols):
                    if affected_channels:
                        old_index = row * old_block.cols + col
                        new_index = row * new_block.cols + col
                        channels = sorted(affected_channels)
                        old_angles = [
                            _position_angle(old_encoding, old_index, channel)
                            if old_encoding is not None
                            else None
                            for channel in channels
                        ]
                        new_angles = [
                            _position_angle(new_encoding, new_index, channel)
                            for channel in channels
                        ]
                        raw_deltas = [
                            (
                                None
                                if old_angle is None or new_angle is None
                                else new_angle - old_angle
                            )
                            for old_angle, new_angle in zip(old_angles, new_angles)
                        ]
                        wrapped_deltas = [
                            None if delta is None else wrap_angle(delta)
                            for delta in raw_deltas
                        ]
                        affected_entries.append(
                            {
                                "old_block_id": old_block.block_id,
                                "new_block_id": new_block.block_id,
                                "row": row,
                                "col": col,
                                "x_old": float(
                                    old_encoding.x.reshape(-1)[old_index]
                                )
                                if old_encoding is not None
                                else None,
                                "y_old": float(
                                    old_encoding.y.reshape(-1)[old_index]
                                )
                                if old_encoding is not None
                                else None,
                                "x_new": float(
                                    new_encoding.x.reshape(-1)[new_index]
                                ),
                                "y_new": float(
                                    new_encoding.y.reshape(-1)[new_index]
                                ),
                                "channels": channels,
                                "old_angles": old_angles,
                                "new_angles": new_angles,
                                "angle_deltas_raw": raw_deltas,
                                "angle_deltas_wrapped": wrapped_deltas,
                            }
                        )
                        recomputed_channel_positions += sum(
                            delta is not None for delta in raw_deltas
                        )

                    if old_encoding is None:
                        continue
                    unaffected_channels = (old_active & new_active) - affected_channels
                    old_index = row * old_block.cols + col
                    new_index = row * new_block.cols + col
                    for channel in sorted(unaffected_channels):
                        unchanged_elements += 1
                        old_value = _position_value(
                            old_encoding, old_index, channel
                        )
                        new_value = _position_value(
                            new_encoding, new_index, channel
                        )
                        if old_value != new_value:
                            unchanged_differences.append(
                                {
                                    "combination": combination.name,
                                    "old_block_id": old_block.block_id,
                                    "new_block_id": new_block.block_id,
                                    "row": row,
                                    "col": col,
                                    "channel": channel,
                                    "old_encoded": old_value,
                                    "new_encoded": new_value,
                                }
                            )

        for block in removed_blocks:
            old_encoding = (
                encode_block(block, old_combination)
                if old_combination is not None
                else None
            )
            if old_encoding is None:
                continue
            for position_index in range(old_encoding.x.size):
                channels = sorted(old_encoding.active_channels)
                affected_entries.append(
                    {
                        "old_block_id": block.block_id,
                        "new_block_id": None,
                        "row": int(old_encoding.row_indices[position_index]),
                        "col": int(old_encoding.col_indices[position_index]),
                        "x_old": float(old_encoding.x.reshape(-1)[position_index]),
                        "y_old": float(old_encoding.y.reshape(-1)[position_index]),
                        "x_new": None,
                        "y_new": None,
                        "channels": channels,
                        "old_angles": [
                            _position_angle(old_encoding, position_index, channel)
                            for channel in channels
                        ],
                        "new_angles": [None for _ in channels],
                        "angle_deltas_raw": [None for _ in channels],
                        "angle_deltas_wrapped": [None for _ in channels],
                    }
                )
                recomputed_channel_positions += len(channels)
        for block in added_blocks:
            new_encoding = encode_block(block, combination)
            for position_index in range(new_encoding.x.size):
                channels = sorted(new_encoding.active_channels)
                affected_entries.append(
                    {
                        "old_block_id": None,
                        "new_block_id": block.block_id,
                        "row": int(new_encoding.row_indices[position_index]),
                        "col": int(new_encoding.col_indices[position_index]),
                        "x_old": None,
                        "y_old": None,
                        "x_new": float(new_encoding.x.reshape(-1)[position_index]),
                        "y_new": float(new_encoding.y.reshape(-1)[position_index]),
                        "channels": channels,
                        "old_angles": [None for _ in channels],
                        "new_angles": [
                            _position_angle(new_encoding, position_index, channel)
                            for channel in channels
                        ],
                        "angle_deltas_raw": [None for _ in channels],
                        "angle_deltas_wrapped": [None for _ in channels],
                    }
                )
                recomputed_channel_positions += len(channels)

        preserved = not unchanged_differences
        all_preserved = all_preserved and preserved
        total_recomputed += recomputed_channel_positions
        combination_reports.append(
            {
                "combination": combination.name,
                "recomputed_position_count": len(affected_entries),
                "recomputed_channel_position_count": recomputed_channel_positions,
                "recomputed_positions": affected_entries,
                "unchanged_channel_positions_checked": unchanged_elements,
                "unaffected_encoding_preserved": preserved,
                "unaffected_encoding_differences": unchanged_differences,
                "basis": (
                    "Only blocks with changed normalized geometry, channel pairs, "
                    "frequency parameters, feature values, or add/remove status "
                    "contribute recomputed positions."
                ),
            }
        )

    return {
        "old_request_id": old_request.request_id,
        "new_request_id": new_request.request_id,
        "matched_block_pairs": [
            {
                "old_block_id": old.block_id,
                "new_block_id": new.block_id,
                "schema_versions": [old.schema_version, new.schema_version],
            }
            for old, new in matches
        ],
        "removed_block_ids": [block.block_id for block in removed_blocks],
        "added_block_ids": [block.block_id for block in added_blocks],
        "block_changes": block_change_reports,
        "combinations": combination_reports,
        "total_recomputed_channel_positions": total_recomputed,
        "unaffected_encoding_preserved": all_preserved,
    }
