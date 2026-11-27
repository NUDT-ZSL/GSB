"""Cross-version and cross-combination consistency checks."""

from __future__ import annotations

from typing import Any
from itertools import combinations

import numpy as np

from .continuity import wrap_angle
from .models import NormalizedRequest
from .rope import (
    BlockEncoding,
    encode_block,
    frequency_signature,
    pair_signature,
)


def _logical_key(
    encoding: BlockEncoding, position_index: int
) -> tuple[Any, ...]:
    return (
        encoding.block.tile_set_id,
        encoding.block.tile_key,
        int(encoding.row_indices[position_index]),
        int(encoding.col_indices[position_index]),
    )


def _position_payload(
    encoding: BlockEncoding,
    position_index: int,
    block: Any | None = None,
) -> dict[str, Any]:
    source_block = encoding.block if block is None else block
    return {
        "schema_version": source_block.schema_version,
        "block_id": source_block.block_id,
        "tile_set_id": source_block.tile_set_id,
        "tile_key": source_block.tile_key,
        "row": int(encoding.row_indices[position_index]),
        "col": int(encoding.col_indices[position_index]),
        "x": float(encoding.x.reshape(-1)[position_index]),
        "y": float(encoding.y.reshape(-1)[position_index]),
        "origin_unit": source_block.origin_unit,
        "channel_group_expressions": source_block.block_source.get(
            "group_selector_kinds", {}
        ),
    }


def _encoding_position_map(
    encodings: list[BlockEncoding],
) -> dict[tuple[Any, ...], tuple[BlockEncoding, int]]:
    result: dict[tuple[Any, ...], tuple[BlockEncoding, int]] = {}
    for encoding in encodings:
        for position_index in range(encoding.x.size):
            result[_logical_key(encoding, position_index)] = (encoding, position_index)
    return result


def _channel_source(
    encoding: BlockEncoding, channel: int, coordinate_drift: bool = False
) -> dict[str, Any]:
    block = encoding.block
    result = {
        "schema_version": block.schema_version,
        "block_id": block.block_id,
        "origin_unit": block.origin_unit,
        "group_selector_kinds": block.block_source.get("group_selector_kinds", {}),
    }
    if not coordinate_drift:
        try:
            band = block.band_for_channel(channel)
            result["group"] = band.name
            result["pair"] = list(pair_signature(block, channel)[1])
            result["pair_frequency_vector"] = list(
                band.pair_frequency_vectors()[band.channels.index(channel) // 2]
            )
        except KeyError:
            pass
    return result


def cross_version_comparison(
    request: NormalizedRequest,
) -> dict[str, Any]:
    pair_reports: list[dict[str, Any]] = []
    all_differences: list[dict[str, Any]] = []
    coordinate_drifts: list[dict[str, Any]] = []
    group_drifts: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    versions = request.schema_versions
    for combination in request.combinations:
        version_maps: dict[str, dict[tuple[Any, ...], tuple[BlockEncoding, int]]] = {}
        version_encodings: dict[str, list[BlockEncoding]] = {}
        for version in versions:
            encodings = [
                encode_block(block, combination)
                for block in request.blocks_for_version(version)
            ]
            version_encodings[version] = encodings
            version_maps[version] = _encoding_position_map(encodings)
        for version_a, version_b in combinations(versions, 2):
            map_a = version_maps[version_a]
            map_b = version_maps[version_b]
            common_keys = sorted(set(map_a) & set(map_b), key=str)
            matched_geo_only = 0
            matched_position_order = 0
            pair_differences: list[dict[str, Any]] = []
            pair_coordinate_drifts: list[dict[str, Any]] = []
            pair_group_drifts: list[dict[str, Any]] = []
            pair_unmatched = [
                {
                    "combination": combination.name,
                    "versions": [version_a, version_b],
                    "position": list(key),
                    "missing_in": version_b,
                    "reason": "logical_position_missing",
                }
                for key in set(map_a) - set(map_b)
            ] + [
                {
                    "combination": combination.name,
                    "versions": [version_a, version_b],
                    "position": list(key),
                    "missing_in": version_a,
                    "reason": "logical_position_missing",
                }
                for key in set(map_b) - set(map_a)
            ]

            if not common_keys:
                geo_map_a: dict[tuple[int, int], tuple[BlockEncoding, int]] = {}
                geo_map_b: dict[tuple[int, int], tuple[BlockEncoding, int]] = {}
                for encoding in version_encodings[version_a]:
                    for position_index in range(encoding.x.size):
                        key = (
                            int(
                                round(
                                    float(encoding.x.reshape(-1)[position_index])
                                    / max(request.tolerance, 1e-12)
                                )
                            ),
                            int(
                                round(
                                    float(encoding.y.reshape(-1)[position_index])
                                    / max(request.tolerance, 1e-12)
                                )
                            ),
                        )
                        geo_map_a[key] = (encoding, position_index)
                for encoding in version_encodings[version_b]:
                    for position_index in range(encoding.x.size):
                        key = (
                            int(
                                round(
                                    float(encoding.x.reshape(-1)[position_index])
                                    / max(request.tolerance, 1e-12)
                                )
                            ),
                            int(
                                round(
                                    float(encoding.y.reshape(-1)[position_index])
                                    / max(request.tolerance, 1e-12)
                                )
                            ),
                        )
                        geo_map_b[key] = (encoding, position_index)
                geo_common = set(geo_map_a) & set(geo_map_b)
                matched_geo_only = len(geo_common)
                common_work = [
                    (
                        geo_map_a[key][0],
                        geo_map_a[key][1],
                        geo_map_b[key][0],
                        geo_map_b[key][1],
                    )
                    for key in sorted(geo_common)
                ]
                if not common_work:
                    ordered_a = [
                        (encoding, position_index)
                        for encoding in version_encodings[version_a]
                        for position_index in range(encoding.x.size)
                    ]
                    ordered_b = [
                        (encoding, position_index)
                        for encoding in version_encodings[version_b]
                        for position_index in range(encoding.x.size)
                    ]
                    if len(ordered_a) == len(ordered_b):
                        common_work = [
                            (
                                left[0],
                                left[1],
                                right[0],
                                right[1],
                            )
                            for left, right in zip(ordered_a, ordered_b)
                        ]
                        matched_position_order = len(common_work)
                if common_work:
                    pair_unmatched = []
            else:
                common_work = [
                    (
                        map_a[key][0],
                        map_a[key][1],
                        map_b[key][0],
                        map_b[key][1],
                    )
                    for key in common_keys
                ]

            for encoding_a, index_a, encoding_b, index_b in common_work:
                x_a = float(encoding_a.x.reshape(-1)[index_a])
                y_a = float(encoding_a.y.reshape(-1)[index_a])
                x_b = float(encoding_b.x.reshape(-1)[index_b])
                y_b = float(encoding_b.y.reshape(-1)[index_b])
                common_channels = sorted(
                    set(encoding_a.active_channels) & set(encoding_b.active_channels)
                )
                if (
                    abs(x_a - x_b) > request.tolerance
                    or abs(y_a - y_b) > request.tolerance
                ):
                    pixel_versions = []
                    if encoding_a.block.origin_unit == "pixels":
                        pixel_versions.append(version_a)
                    if encoding_b.block.origin_unit == "pixels":
                        pixel_versions.append(version_b)
                    drift = {
                        "combination": combination.name,
                        "versions": [version_a, version_b],
                        "blocks": [
                            encoding_a.block.block_id,
                            encoding_b.block.block_id,
                        ],
                        "position_a": _position_payload(encoding_a, index_a),
                        "position_b": _position_payload(encoding_b, index_b),
                        "coordinate_delta": [x_b - x_a, y_b - y_a],
                        "channels": common_channels,
                        "classification": "coordinate_unit_conversion",
                        "source_versions": pixel_versions
                        or [version_a, version_b],
                        "source_blocks": [
                            encoding_a.block.block_id,
                            encoding_b.block.block_id,
                        ],
                        "reason": (
                            "same logical raster index normalizes to different "
                            "geographic coordinates"
                        ),
                    }
                    pair_coordinate_drifts.append(drift)
                    coordinate_drifts.append(drift)
                    continue

                active_union = sorted(
                    set(encoding_a.active_channels) | set(encoding_b.active_channels)
                )
                for channel in active_union:
                    active_a = channel in encoding_a.active_channels
                    active_b = channel in encoding_b.active_channels
                    if not active_a or not active_b:
                        difference = {
                            "combination": combination.name,
                            "versions": [version_a, version_b],
                            "position_a": _position_payload(encoding_a, index_a),
                            "position_b": _position_payload(encoding_b, index_b),
                            "channel": channel,
                            "classification": "active_channel_mismatch",
                            "source_versions": [
                                version
                                for version, active in (
                                    (version_a, active_a),
                                    (version_b, active_b),
                                )
                                if not active
                            ],
                            "reason": "channel is active in only one schema version",
                        }
                        pair_differences.append(difference)
                        all_differences.append(difference)
                        continue
                    theta_a = float(encoding_a.angles[index_a, channel])
                    theta_b = float(encoding_b.angles[index_b, channel])
                    value_a = float(encoding_a.encoded.reshape(-1, encoding_a.block.channels)[index_a, channel])
                    value_b = float(encoding_b.encoded.reshape(-1, encoding_b.block.channels)[index_b, channel])
                    angle_delta_raw = theta_b - theta_a
                    angle_delta_wrapped = wrap_angle(angle_delta_raw)
                    encoded_delta = value_b - value_a
                    group_a = pair_signature(encoding_a.block, channel)
                    group_b = pair_signature(encoding_b.block, channel)
                    frequency_a = frequency_signature(encoding_a.block, channel)
                    frequency_b = frequency_signature(encoding_b.block, channel)
                    classifications: list[str] = []
                    if group_a != group_b:
                        classifications.append("channel_group_expression")
                    if frequency_a != frequency_b:
                        classifications.append("frequency_parameter")
                    if not classifications:
                        classifications.append("numeric_or_feature_drift")
                    angle_failed = abs(angle_delta_wrapped) > request.tolerance
                    encoded_failed = abs(encoded_delta) > request.tolerance
                    if angle_failed or encoded_failed:
                        difference = {
                            "combination": combination.name,
                            "versions": [version_a, version_b],
                            "position_a": _position_payload(encoding_a, index_a),
                            "position_b": _position_payload(encoding_b, index_b),
                            "channel": channel,
                            "angle_a": theta_a,
                            "angle_b": theta_b,
                            "angle_delta_raw": angle_delta_raw,
                            "angle_delta_wrapped": angle_delta_wrapped,
                            "encoded_a": value_a,
                            "encoded_b": value_b,
                            "encoded_delta": encoded_delta,
                            "classification": classifications[0]
                            if len(classifications) == 1
                            else classifications,
                            "source_versions": [version_a, version_b],
                            "channel_sources": [
                                _channel_source(encoding_a, channel),
                                _channel_source(encoding_b, channel),
                            ],
                            "reason": "angle or encoded feature exceeds tolerance",
                        }
                        pair_differences.append(difference)
                        all_differences.append(difference)
                        if "channel_group_expression" in classifications:
                            pair_group_drifts.append(difference)
                            group_drifts.append(difference)

            pair_reports.append(
                {
                    "combination": combination.name,
                    "versions": [version_a, version_b],
                    "matched_by_logical_key": len(common_keys),
                    "matched_by_geographic_position": matched_geo_only,
                    "matched_by_position_order": matched_position_order,
                    "checked_positions": len(common_work),
                    "difference_count": len(pair_differences),
                    "coordinate_unit_drift_count": len(pair_coordinate_drifts),
                    "channel_group_drift_count": len(pair_group_drifts),
                    "unmatched_position_count": len(pair_unmatched),
                    "differences": pair_differences,
                    "coordinate_unit_drift": pair_coordinate_drifts,
                    "channel_group_drift": pair_group_drifts,
                    "unmatched_positions": pair_unmatched,
                    "consistent": not pair_differences
                    and not pair_coordinate_drifts,
                }
            )
            unmatched.extend(pair_unmatched)

    return {
        "schema_versions": versions,
        "combination_pairs": len(pair_reports),
        "pair_reports": pair_reports,
        "difference_count": len(all_differences),
        "differences": all_differences,
        "coordinate_unit_drift": coordinate_drifts,
        "channel_group_drift": group_drifts,
        "unmatched_positions": unmatched,
        "consistent": not all_differences
        and not coordinate_drifts
        and not unmatched,
    }


def cross_combination_comparison(
    request: NormalizedRequest,
) -> dict[str, Any]:
    combinations = request.combinations
    pair_reports: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    for left_index, left_combination in enumerate(combinations):
        for right_combination in combinations[left_index + 1 :]:
            checked = 0
            pair_differences: list[dict[str, Any]] = []
            for version in request.schema_versions:
                for block in request.blocks_for_version(version):
                    left_encoding = encode_block(block, left_combination)
                    right_encoding = encode_block(block, right_combination)
                    overlap_channels = sorted(
                        set(left_encoding.active_channels)
                        & set(right_encoding.active_channels)
                    )
                    if not overlap_channels:
                        continue
                    left_angles = left_encoding.angles
                    right_angles = right_encoding.angles
                    left_values = left_encoding.encoded.reshape(
                        -1, block.channels
                    )
                    right_values = right_encoding.encoded.reshape(
                        -1, block.channels
                    )
                    for position_index in range(left_encoding.x.size):
                        for channel in overlap_channels:
                            checked += 1
                            angle_delta = wrap_angle(
                                float(right_angles[position_index, channel])
                                - float(left_angles[position_index, channel])
                            )
                            encoded_delta = float(
                                right_values[position_index, channel]
                                - left_values[position_index, channel]
                            )
                            if (
                                abs(angle_delta) > request.tolerance
                                or abs(encoded_delta) > request.tolerance
                            ):
                                difference = {
                                    "combinations": [
                                        left_combination.name,
                                        right_combination.name,
                                    ],
                                    "schema_version": version,
                                    "block_id": block.block_id,
                                    "row": int(left_encoding.row_indices[position_index]),
                                    "col": int(left_encoding.col_indices[position_index]),
                                    "x": float(
                                        left_encoding.x.reshape(-1)[position_index]
                                    ),
                                    "y": float(
                                        left_encoding.y.reshape(-1)[position_index]
                                    ),
                                    "channel": channel,
                                    "angle_left": float(
                                        left_angles[position_index, channel]
                                    ),
                                    "angle_right": float(
                                        right_angles[position_index, channel]
                                    ),
                                    "angle_delta_wrapped": angle_delta,
                                    "encoded_left": float(
                                        left_values[position_index, channel]
                                    ),
                                    "encoded_right": float(
                                        right_values[position_index, channel]
                                    ),
                                    "encoded_delta": encoded_delta,
                                    "source_bands": [
                                        block.band_for_channel(channel).name,
                                        block.band_for_channel(channel).name,
                                    ],
                                }
                                pair_differences.append(difference)
                                differences.append(difference)
            pair_reports.append(
                {
                    "combinations": [
                        left_combination.name,
                        right_combination.name,
                    ],
                    "checked_channel_positions": checked,
                    "difference_count": len(pair_differences),
                    "differences": pair_differences,
                    "consistent": not pair_differences,
                }
            )
    return {
        "pair_count": len(pair_reports),
        "checked_channel_positions": sum(
            report["checked_channel_positions"] for report in pair_reports
        ),
        "difference_count": len(differences),
        "differences": differences,
        "pair_reports": pair_reports,
        "consistent": not differences,
    }
