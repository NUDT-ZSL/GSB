from __future__ import annotations

from typing import Any

from .config import LayoutConfig
from .util import payload_hash, sha256_json


ARTIFACT_VERSIONS = {
    "detect": 1,
    "roles": 1,
    "columns": 1,
    "order": 1,
}


def stage_parameters(config: LayoutConfig, effective: dict[str, Any]) -> dict[str, dict[str, Any]]:
    analysis = config.analysis
    return {
        "detect": {
            "binary_threshold": analysis.binary_threshold,
            "min_block_width": analysis.min_block_width,
            "min_block_height": analysis.min_block_height,
            "projection_analysis_window": analysis.projection_analysis_window,
            "resolution_step": effective["detection_step"],
        },
        "roles": {
            "title_height_ratio_range": list(analysis.title_height_ratio_range),
            "body_height_ratio_range": list(analysis.body_height_ratio_range),
            "image_ink_density_threshold": analysis.image_ink_density_threshold,
            "text_ink_density_range": list(analysis.text_ink_density_range),
            "projection_periodicity_threshold": analysis.projection_periodicity_threshold,
            "role_priority": list(analysis.role_priority),
            "header_band_ratio": analysis.header_band_ratio,
            "footer_band_ratio": analysis.footer_band_ratio,
        },
        "columns": {
            "enabled": effective["columns_enabled"],
            "gap_threshold": config.columns.gap_threshold,
            "overlap_threshold": config.columns.overlap_threshold,
            "spanning_width_ratio": config.columns.spanning_width_ratio,
        },
        "order": {
            "rule": effective["reading_rule"],
            "region_band_tolerance": config.reading.region_band_tolerance,
        },
    }


def derive_cache_keys(
    *,
    image_hash: str,
    config: LayoutConfig,
    effective: dict[str, Any],
) -> dict[str, str]:
    parameters = stage_parameters(config, effective)
    detect_key = sha256_json(
        {
            "stage": "detect",
            "artifact_version": ARTIFACT_VERSIONS["detect"],
            "image_sha256": image_hash,
            "parameters": parameters["detect"],
        }
    )
    roles_key = sha256_json(
        {
            "stage": "roles",
            "artifact_version": ARTIFACT_VERSIONS["roles"],
            "upstream_key": detect_key,
            "parameters": parameters["roles"],
        }
    )
    columns_key = sha256_json(
        {
            "stage": "columns",
            "artifact_version": ARTIFACT_VERSIONS["columns"],
            "upstream_key": roles_key,
            "parameters": parameters["columns"],
        }
    )
    order_key = sha256_json(
        {
            "stage": "order",
            "artifact_version": ARTIFACT_VERSIONS["order"],
            "upstream_key": columns_key,
            "parameters": parameters["order"],
        }
    )
    return {
        "detect": detect_key,
        "roles": roles_key,
        "columns": columns_key,
        "order": order_key,
    }


def artifact_payload_hash(payload: Any) -> str:
    return payload_hash(payload)
