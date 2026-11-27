"""Hand-constructed acceptance samples for seam and cross-schema checks."""

from __future__ import annotations

from typing import Any

from .compare import cross_version_comparison
from .continuity import continuity_report
from .normalize import normalize_request
from .rope import encode_request_fields


def _seam_payload() -> dict[str, Any]:
    left_data = [
        [
            [0.25, -0.5],
            [0.75, 0.125],
            [-0.375, 0.875],
        ]
    ]
    right_data = [
        [
            [0.5, -0.25],
            [-0.625, 0.375],
        ]
    ]
    band = {
        "name": "manual_band",
        "channel_range": [0, 2],
    }
    return {
        "request_id": "manual-cross-block-seam",
        "tolerance": 1e-9,
        "phase_threshold": 1e-6,
        "pixel_anchor": "corner",
        "blocks": [
            {
                "schema_version": "v1",
                "block_id": "manual-left",
                "tile_set_id": "manual-seam",
                "tile_key": "manual-left",
                "origin_x": 0.0,
                "origin_y": 0.0,
                "origin_unit": "meters",
                "pixel_size": [2.0, 2.0],
                "shape": [1, 3, 2],
                "features": left_data,
                "channel_ranges": [band],
                "frequency_bands": [
                    {
                        "name": "manual_band",
                        "angle_base": 16.0,
                        "frequency_scale": 0.5,
                        "frequency_axis": [1.0, 0.0],
                    }
                ],
            },
            {
                "schema_version": "v1",
                "block_id": "manual-right",
                "tile_set_id": "manual-seam",
                "tile_key": "manual-right",
                "origin_x": 6.0,
                "origin_y": 0.0,
                "origin_unit": "meters",
                "pixel_size": [2.0, 2.0],
                "shape": [1, 2, 2],
                "features": right_data,
                "channel_ranges": [band],
                "frequency_bands": [
                    {
                        "name": "manual_band",
                        "angle_base": 16.0,
                        "frequency_scale": 0.5,
                        "frequency_axis": [1.0, 0.0],
                    }
                ],
            },
        ],
        "band_combinations": [{"name": "manual_all", "bands": ["manual_band"]}],
    }


def _same_geo_payload() -> dict[str, Any]:
    data = [[[0.125, -0.75, 0.5, 0.25]]]
    common = {
        "tile_set_id": "manual-geo",
        "tile_key": "manual-cell",
        "resolution": [4.0, 4.0],
        "shape": [1, 1, 4],
        "tensor": data,
    }
    frequency_parameters = {
        "angle_base": 100.0,
        "frequency_scale": 0.125,
        "axis": [1.0, 0.5],
    }
    return {
        "request_id": "manual-same-geo-different-parameters",
        "tolerance": 1e-9,
        "phase_threshold": 1e-6,
        "pixel_anchor": "center",
        "blocks": [
            {
                **common,
                "schema_version": "v1",
                "block_id": "manual-v1",
                "origin_x": 3.0,
                "origin_y": 2.0,
                "origin_unit": "meters",
                "pixel_size": [4.0, 4.0],
                "channel_ranges": [{"name": "manual_band", "channel_range": [0, 4]}],
                "frequency_bands": [
                    {"name": "manual_band", **frequency_parameters}
                ],
            },
            {
                **common,
                "schema_version": "v2",
                "block_id": "manual-v2",
                "geo_origin": [0.75, 0.5],
                "coord_unit": "pixels",
                "channel_groups": [
                    {
                        "name": "manual_band",
                        "indices": [0, 1, 2, 3],
                        "rope": {
                            "base": 100.0,
                            "freq_scale": 0.125,
                            "axis": [1.0, 0.5],
                        },
                    }
                ],
            },
            {
                **common,
                "schema_version": "v3",
                "block_id": "manual-v3",
                "geo_origin": {
                    "east": 3.0,
                    "north": 2.0,
                    "unit": "meters",
                },
                "bands": [
                    {
                        "name": "manual_band",
                        "channels": {
                            "start": 0,
                            "end": 4,
                            "inclusive_end": False,
                        },
                        "rope_base": 100.0,
                        "frequency": {"scale": 0.125, "axis": [1.0, 0.5]},
                    }
                ],
            },
        ],
        "band_combinations": [{"name": "manual_all", "bands": ["manual_band"]}],
    }


def run_manual_cases() -> dict[str, Any]:
    seam_request = normalize_request(_seam_payload())
    seam_reports = [
        continuity_report(seam_request, combination)
        for combination in seam_request.combinations
    ]
    seam_report = seam_reports[0]["reports"][0]
    seam_checks = {
        "request_id": seam_request.request_id,
        "global_phase_continuous": seam_report["global_continuous"],
        "global_discontinuity_location_count": len(
            seam_report["global_discontinuity_positions"]
        ),
        "local_phase_break_detected": bool(
            seam_report["local_phase_break_positions"]
        ),
        "local_phase_break_location_count": len(
            seam_report["local_phase_break_positions"]
        ),
        "max_global_phase_difference": seam_report[
            "max_global_phase_difference"
        ],
        "max_local_phase_difference": seam_report[
            "max_local_phase_difference"
        ],
        "seam_id": seam_report["seam_id"],
        "basis": seam_report["basis"],
        "sample_positions": seam_report["sample_positions"],
    }
    seam_passed = (
        seam_checks["global_phase_continuous"]
        and seam_checks["local_phase_break_detected"]
    )

    same_geo_request = normalize_request(_same_geo_payload())
    field_result = encode_request_fields(same_geo_request, include_tensors=True)
    field_result.pop("_block_encodings", None)
    cross_version = cross_version_comparison(same_geo_request)
    same_geo_checks = {
        "request_id": same_geo_request.request_id,
        "normalized_origins_m": {
            block.block_id: list(block.origin_m) for block in same_geo_request.blocks
        },
        "cross_version_consistent": cross_version["consistent"],
        "difference_count": cross_version["difference_count"],
        "coordinate_unit_drift_count": len(
            cross_version["coordinate_unit_drift"]
        ),
        "channel_group_drift_count": len(cross_version["channel_group_drift"]),
        "field_hashes": [
            field["field_hash"] for field in field_result["fields"]
        ],
        "differences": cross_version["differences"],
    }
    same_geo_passed = (
        same_geo_checks["cross_version_consistent"]
        and same_geo_checks["coordinate_unit_drift_count"] == 0
        and same_geo_checks["channel_group_drift_count"] == 0
    )
    return {
        "cross_block_boundary": seam_checks,
        "same_geographic_position_different_parameters": same_geo_checks,
        "passed": bool(seam_passed and same_geo_passed),
    }
