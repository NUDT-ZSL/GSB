"""Configuration loading, validation, normalization, and fingerprinting."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import fingerprint, read_json


@dataclass(frozen=True)
class ConfigIssue:
    field: str
    message: str


@dataclass(frozen=True)
class LoadedConfig:
    raw: dict[str, Any]
    config: dict[str, Any] | None
    issues: tuple[ConfigIssue, ...]
    hash: str | None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _section(raw: dict[str, Any], key: str, issues: list[ConfigIssue]) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        issues.append(ConfigIssue(key, "must be an object"))
        return {}
    return value


def _require_int(
    value: Any,
    field: str,
    issues: list[ConfigIssue],
    *,
    minimum: int | None = None,
) -> None:
    if not _is_int(value):
        issues.append(ConfigIssue(field, "must be an integer"))
        return
    if minimum is not None and value < minimum:
        issues.append(ConfigIssue(field, f"must be >= {minimum}"))


def _require_number(
    value: Any,
    field: str,
    issues: list[ConfigIssue],
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    minimum_exclusive: bool = False,
) -> None:
    if not _is_number(value):
        issues.append(ConfigIssue(field, "must be a finite number"))
        return
    number = float(value)
    if minimum is not None:
        if minimum_exclusive and number <= minimum:
            issues.append(ConfigIssue(field, f"must be > {minimum}"))
        elif not minimum_exclusive and number < minimum:
            issues.append(ConfigIssue(field, f"must be >= {minimum}"))
    if maximum is not None and number > maximum:
        issues.append(ConfigIssue(field, f"must be <= {maximum}"))


def validate_config(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, tuple[ConfigIssue, ...]]:
    issues: list[ConfigIssue] = []
    if not isinstance(raw, dict):
        return None, (ConfigIssue("$", "configuration root must be an object"),)

    schema_version = raw.get("schema_version")
    _require_int(schema_version, "schema_version", issues, minimum=1)
    if _is_int(schema_version) and schema_version != 1:
        issues.append(ConfigIssue("schema_version", "unsupported schema; expected 1"))

    seed = raw.get("seed")
    _require_int(seed, "seed", issues, minimum=0)

    feature = _section(raw, "feature_detection", issues)
    detector = feature.get("detector", "chromatic_blob_v1")
    if detector != "chromatic_blob_v1":
        issues.append(ConfigIssue("feature_detection.detector", "must be 'chromatic_blob_v1'"))
    _require_number(
        feature.get("threshold"),
        "feature_detection.threshold",
        issues,
        minimum=0.0,
        maximum=1.0,
        minimum_exclusive=True,
    )
    _require_int(
        feature.get("max_features_per_view"),
        "feature_detection.max_features_per_view",
        issues,
        minimum=1,
    )
    _require_int(
        feature.get("min_distance_px"),
        "feature_detection.min_distance_px",
        issues,
        minimum=1,
    )
    _require_int(
        feature.get("descriptor_radius"),
        "feature_detection.descriptor_radius",
        issues,
        minimum=2,
    )

    matching = _section(raw, "view_matching", issues)
    _require_number(
        matching.get("descriptor_max_distance"),
        "view_matching.descriptor_max_distance",
        issues,
        minimum=0.0,
        minimum_exclusive=True,
    )
    _require_number(
        matching.get("ratio_test"),
        "view_matching.ratio_test",
        issues,
        minimum=0.0,
        maximum=1.0,
        minimum_exclusive=True,
    )
    _require_int(
        matching.get("min_inliers"),
        "view_matching.min_inliers",
        issues,
        minimum=1,
    )
    loop_pairs_raw = matching.get("loop_pairs", [])
    loop_pairs: list[list[int]] = []
    if not isinstance(loop_pairs_raw, list):
        issues.append(ConfigIssue("view_matching.loop_pairs", "must be an array"))
    else:
        seen_pairs: set[tuple[int, int]] = set()
        for position, pair in enumerate(loop_pairs_raw):
            field = f"view_matching.loop_pairs[{position}]"
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(_is_int(item) and item >= 1 for item in pair)
            ):
                issues.append(ConfigIssue(field, "must contain two positive view indexes"))
                continue
            canonical_pair = (min(pair), max(pair))
            if canonical_pair[0] == canonical_pair[1]:
                issues.append(ConfigIssue(field, "view indexes must be distinct"))
                continue
            if canonical_pair in seen_pairs:
                issues.append(ConfigIssue(field, "duplicate loop pair"))
                continue
            seen_pairs.add(canonical_pair)
            loop_pairs.append([canonical_pair[0], canonical_pair[1]])

    triangulation = _section(raw, "triangulation", issues)
    _require_number(
        triangulation.get("min_parallax_deg"),
        "triangulation.min_parallax_deg",
        issues,
        minimum=0.0,
        maximum=180.0,
        minimum_exclusive=True,
    )
    _require_number(
        triangulation.get("max_reprojection_error_px"),
        "triangulation.max_reprojection_error_px",
        issues,
        minimum=0.0,
        minimum_exclusive=True,
    )
    _require_number(
        triangulation.get("min_depth"),
        "triangulation.min_depth",
        issues,
        minimum=0.0,
        minimum_exclusive=True,
    )
    _require_number(
        triangulation.get("max_depth"),
        "triangulation.max_depth",
        issues,
        minimum=0.0,
        minimum_exclusive=True,
    )
    if (
        _is_number(triangulation.get("min_depth"))
        and _is_number(triangulation.get("max_depth"))
        and float(triangulation["min_depth"]) >= float(triangulation["max_depth"])
    ):
        issues.append(ConfigIssue("triangulation.max_depth", "must be greater than min_depth"))

    depth = _section(raw, "depth_map", issues)
    _require_int(depth.get("width"), "depth_map.width", issues, minimum=1)
    _require_int(depth.get("height"), "depth_map.height", issues, minimum=1)
    _require_int(
        depth.get("quantization_levels"),
        "depth_map.quantization_levels",
        issues,
        minimum=2,
    )
    _require_int(
        depth.get("smoothing_iterations"),
        "depth_map.smoothing_iterations",
        issues,
        minimum=0,
    )
    _require_number(
        depth.get("smoothing_weight"),
        "depth_map.smoothing_weight",
        issues,
        minimum=0.0,
        maximum=1.0,
    )

    pointcloud = _section(raw, "pointcloud", issues)
    _require_number(
        pointcloud.get("voxel_size"),
        "pointcloud.voxel_size",
        issues,
        minimum=0.0,
        minimum_exclusive=True,
    )

    executor = _section(raw, "executor", issues)
    _require_int(executor.get("max_workers"), "executor.max_workers", issues, minimum=1)

    snapshot = _section(raw, "snapshot", issues)
    _require_int(
        snapshot.get("commit_interval_views"),
        "snapshot.commit_interval_views",
        issues,
        minimum=1,
    )
    _require_int(snapshot.get("version"), "snapshot.version", issues, minimum=1)

    if issues:
        return None, tuple(issues)

    normalized = {
        "schema_version": 1,
        "seed": int(seed),
        "feature_detection": {
            "detector": "chromatic_blob_v1",
            "threshold": float(feature["threshold"]),
            "max_features_per_view": int(feature["max_features_per_view"]),
            "min_distance_px": int(feature["min_distance_px"]),
            "descriptor_radius": int(feature["descriptor_radius"]),
        },
        "view_matching": {
            "descriptor_max_distance": float(matching["descriptor_max_distance"]),
            "ratio_test": float(matching["ratio_test"]),
            "min_inliers": int(matching["min_inliers"]),
            "loop_pairs": sorted(loop_pairs),
        },
        "triangulation": {
            "min_parallax_deg": float(triangulation["min_parallax_deg"]),
            "max_reprojection_error_px": float(
                triangulation["max_reprojection_error_px"]
            ),
            "min_depth": float(triangulation["min_depth"]),
            "max_depth": float(triangulation["max_depth"]),
        },
        "depth_map": {
            "width": int(depth["width"]),
            "height": int(depth["height"]),
            "quantization_levels": int(depth["quantization_levels"]),
            "smoothing_iterations": int(depth["smoothing_iterations"]),
            "smoothing_weight": float(depth["smoothing_weight"]),
        },
        "pointcloud": {"voxel_size": float(pointcloud["voxel_size"])},
        "executor": {"max_workers": int(executor["max_workers"])},
        "snapshot": {
            "commit_interval_views": int(snapshot["commit_interval_views"]),
            "version": int(snapshot["version"]),
        },
    }
    return normalized, tuple()


def load_and_validate_config(path: Path) -> LoadedConfig:
    raw = read_json(path)
    config, issues = validate_config(raw)
    config_hash = config_fingerprint(config) if config is not None else None
    return LoadedConfig(
        raw=copy.deepcopy(raw),
        config=config,
        issues=issues,
        hash=config_hash,
    )


def config_fingerprint(config: dict[str, Any]) -> str:
    return fingerprint("recon-config-v1", config)


def semantic_config_fingerprint(config: dict[str, Any]) -> str:
    """Fingerprint only parameters that can change reconstruction content.

    Worker count and snapshot commit cadence are execution controls, not
    geometry inputs. Keeping them out of this fingerprint lets serial and
    concurrent runs produce byte-identical result sections and point clouds.
    """
    semantic = copy.deepcopy(config)
    semantic.pop("executor", None)
    snapshot = semantic.get("snapshot")
    if isinstance(snapshot, dict):
        snapshot.pop("commit_interval_views", None)
    return fingerprint("recon-semantic-config-v1", semantic)


def parameter_fingerprint(name: str, params: Any) -> str:
    return fingerprint(f"param-{name}-v1", params)
