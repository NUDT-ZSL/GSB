"""Seam continuity diagnostics for independently encoded feature blocks."""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from .models import BandCombination, NormalizedBlock, NormalizedRequest
from .rope import active_channels_for_block, channel_angle


def wrap_angle(value: float) -> float:
    return float((value + math.pi) % (2.0 * math.pi) - math.pi)


def _deduplicate_axis(values: list[float], tolerance: float) -> list[float]:
    result: list[float] = []
    for value in sorted(values):
        if not result or abs(value - result[-1]) > tolerance:
            result.append(value)
        else:
            result[-1] = (result[-1] + value) / 2.0
    return result


def _adjacent_pair(
    left: NormalizedBlock, right: NormalizedBlock, tolerance: float
) -> tuple[str, float, tuple[float, float]] | None:
    left_extent = left.extent_m()
    right_extent = right.extent_m()
    if (
        abs(left_extent[1] - right_extent[0]) <= tolerance
        and min(left_extent[3], right_extent[3])
        - max(left_extent[2], right_extent[2])
        > tolerance
    ):
        seam = (left_extent[1] + right_extent[0]) / 2.0
        return (
            "vertical",
            seam,
            (
                max(left_extent[2], right_extent[2]),
                min(left_extent[3], right_extent[3]),
            ),
        )
    if (
        abs(left_extent[0] - right_extent[1]) <= tolerance
        and min(left_extent[3], right_extent[3])
        - max(left_extent[2], right_extent[2])
        > tolerance
    ):
        seam = (left_extent[0] + right_extent[1]) / 2.0
        return (
            "vertical",
            seam,
            (
                max(left_extent[2], right_extent[2]),
                min(left_extent[3], right_extent[3]),
            ),
        )
    if (
        abs(left_extent[3] - right_extent[2]) <= tolerance
        and min(left_extent[1], right_extent[1])
        - max(left_extent[0], right_extent[0])
        > tolerance
    ):
        seam = (left_extent[3] + right_extent[2]) / 2.0
        return (
            "horizontal",
            seam,
            (
                max(left_extent[0], right_extent[0]),
                min(left_extent[1], right_extent[1]),
            ),
        )
    if (
        abs(left_extent[2] - right_extent[3]) <= tolerance
        and min(left_extent[1], right_extent[1])
        - max(left_extent[0], right_extent[0])
        > tolerance
    ):
        seam = (left_extent[2] + right_extent[3]) / 2.0
        return (
            "horizontal",
            seam,
            (
                max(left_extent[0], right_extent[0]),
                min(left_extent[1], right_extent[1]),
            ),
        )
    return None


def _line_values(
    left: NormalizedBlock,
    right: NormalizedBlock,
    axis: str,
    span: tuple[float, float],
    tolerance: float,
) -> list[float]:
    if axis == "vertical":
        left_axis, _ = left.axis_positions()
        right_axis, _ = right.axis_positions()
        values = [
            float(value)
            for value in left.axis_positions()[1]
            if span[0] - tolerance <= value <= span[1] + tolerance
        ]
        values.extend(
            float(value)
            for value in right.axis_positions()[1]
            if span[0] - tolerance <= value <= span[1] + tolerance
        )
    else:
        values = [
            float(value)
            for value in left.axis_positions()[0]
            if span[0] - tolerance <= value <= span[1] + tolerance
        ]
        values.extend(
            float(value)
            for value in right.axis_positions()[0]
            if span[0] - tolerance <= value <= span[1] + tolerance
        )
    return _deduplicate_axis(values, tolerance)


def _angle_in_frame(
    block: NormalizedBlock,
    channel: int,
    x: float,
    y: float,
    local: bool,
) -> float:
    if local:
        x = x - block.origin_m[0]
        y = y - block.origin_m[1]
    return channel_angle(block, channel, x, y)


def seam_report_for_pair(
    left: NormalizedBlock,
    right: NormalizedBlock,
    combination: BandCombination,
    threshold: float,
    tolerance: float,
) -> dict[str, Any] | None:
    adjacency = _adjacent_pair(left, right, tolerance)
    if adjacency is None:
        return None
    axis, seam_coordinate, line_span = adjacency
    active_left = set(active_channels_for_block(left, combination))
    active_right = set(active_channels_for_block(right, combination))
    common_channels = sorted(active_left & active_right)
    if not common_channels:
        return {
            "seam_id": (
                f"{left.schema_version}:{left.tile_set_id}:"
                f"{left.block_id}|{right.block_id}:{axis}"
            ),
            "schema_version": left.schema_version,
            "tile_set_id": left.tile_set_id,
            "blocks": [left.block_id, right.block_id],
            "axis": axis,
            "seam_geo": float(seam_coordinate),
            "line_span_geo": list(line_span),
            "threshold": threshold,
            "status": "no_common_active_channels",
            "checked_locations": 0,
            "local_phase_break_positions": [],
            "global_discontinuity_positions": [],
            "max_global_phase_difference": 0.0,
            "max_local_phase_difference": 0.0,
            "basis": "combination has no overlapping active channels",
        }

    line_values = _line_values(left, right, axis, line_span, tolerance)
    break_positions: list[dict[str, Any]] = []
    global_mismatches: list[dict[str, Any]] = []
    sample_lines: list[dict[str, Any]] = []
    max_global = 0.0
    max_local = 0.0
    checked = 0
    for line_value in line_values:
        if axis == "vertical":
            x = seam_coordinate
            y = line_value
        else:
            x = line_value
            y = seam_coordinate
        for channel in common_channels:
            theta_left_global = _angle_in_frame(left, channel, x, y, False)
            theta_right_global = _angle_in_frame(right, channel, x, y, False)
            theta_left_local = _angle_in_frame(left, channel, x, y, True)
            theta_right_local = _angle_in_frame(right, channel, x, y, True)
            global_difference = wrap_angle(
                theta_right_global - theta_left_global
            )
            local_difference = wrap_angle(theta_right_local - theta_left_local)
            max_global = max(max_global, abs(global_difference))
            max_local = max(max_local, abs(local_difference))
            checked += 1
            position = {
                "axis": axis,
                "line_geo": float(line_value),
                "seam_geo": float(seam_coordinate),
                "channel": channel,
                "left_block": left.block_id,
                "right_block": right.block_id,
                "theta_left_global": theta_left_global,
                "theta_right_global": theta_right_global,
                "global_phase_difference": global_difference,
                "theta_left_local": theta_left_local,
                "theta_right_local": theta_right_local,
                "local_phase_difference": local_difference,
            }
            if len(sample_lines) < 8:
                sample_lines.append(position)
            if abs(local_difference) > threshold:
                break_positions.append(
                    {
                        **position,
                        "reason": "block_local_coordinate_phase_reset",
                    }
                )
            if abs(global_difference) > threshold:
                global_mismatches.append(
                    {
                        **position,
                        "reason": "global_frequency_parameter_or_pixel_phase_discontinuity",
                    }
                )

    global_continuous = not global_mismatches
    local_continuous = not break_positions
    if not global_continuous:
        status = "global_phase_discontinuity"
    elif not local_continuous:
        status = "continuous_when_using_global_coordinates"
    else:
        status = "continuous"
    return {
        "seam_id": (
            f"{left.schema_version}:{left.tile_set_id}:"
            f"{left.block_id}|{right.block_id}:{axis}"
        ),
        "schema_version": left.schema_version,
        "tile_set_id": left.tile_set_id,
        "blocks": [left.block_id, right.block_id],
        "axis": axis,
        "seam_geo": float(seam_coordinate),
        "line_span_geo": list(line_span),
        "line_positions": line_values,
        "threshold": threshold,
        "status": status,
        "checked_locations": checked,
        "global_continuous": global_continuous,
        "local_coordinate_continuous": local_continuous,
        "max_global_phase_difference": max_global,
        "max_local_phase_difference": max_local,
        "local_phase_break_positions": break_positions,
        "global_discontinuity_positions": global_mismatches,
        "sample_positions": sample_lines,
        "basis": (
            "Both blocks are evaluated at the same global seam coordinates; "
            "local_phase_difference substitutes each block's own origin-relative "
            "coordinate and therefore exposes the reset that would occur if each "
            "team encoded its block independently."
        ),
    }


def continuity_report(
    request: NormalizedRequest,
    combination: BandCombination,
) -> dict[str, Any]:
    reports: list[dict[str, Any]] = []
    version_summaries: dict[str, Any] = {}
    for version in request.schema_versions:
        tile_sets = sorted(
            {block.tile_set_id for block in request.blocks_for_version(version)}
        )
        version_reports: list[dict[str, Any]] = []
        for tile_set_id in tile_sets:
            blocks = request.blocks_for_set(version, tile_set_id)
            for left_index, left in enumerate(blocks):
                for right in blocks[left_index + 1 :]:
                    report = seam_report_for_pair(
                        left,
                        right,
                        combination,
                        request.phase_threshold,
                        request.tolerance,
                    )
                    if report is not None:
                        reports.append(report)
                        version_reports.append(report)
        local_break_count = sum(
            len(report.get("local_phase_break_positions", []))
            for report in version_reports
        )
        global_break_count = sum(
            len(report.get("global_discontinuity_positions", []))
            for report in version_reports
        )
        version_summaries[version] = {
            "combination": combination.name,
            "seams_checked": len(version_reports),
            "seams_with_local_phase_breaks": sum(
                bool(report.get("local_phase_break_positions"))
                for report in version_reports
            ),
            "local_phase_break_location_count": local_break_count,
            "global_discontinuity_location_count": global_break_count,
        }
    return {
        "combination": combination.name,
        "threshold": request.phase_threshold,
        "tolerance": request.tolerance,
        "seams_checked": len(reports),
        "seams_with_local_phase_breaks": sum(
            bool(report.get("local_phase_break_positions")) for report in reports
        ),
        "seams_with_global_discontinuity": sum(
            bool(report.get("global_discontinuity_positions")) for report in reports
        ),
        "version_summaries": version_summaries,
        "reports": reports,
    }
