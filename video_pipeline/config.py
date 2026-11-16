"""Configuration parsing, validation, and semantic hashing."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError


@dataclass(frozen=True)
class WindowSpec:
    window_id: str
    class_name: str
    width: int
    height: int
    step: int
    global_index: int


@dataclass(frozen=True)
class DetectionConfig:
    windows: tuple[WindowSpec, ...]
    target_colors: dict[str, tuple[int, int, int]]
    color_tolerance: int
    color_ratio_threshold: float
    edge_pixel_threshold: float
    edge_density_threshold: float
    color_weight: float
    edge_weight: float
    detection_threshold: float
    nms_iou_threshold: float
    source: dict[str, Any]

    @property
    def class_names(self) -> tuple[str, ...]:
        return tuple(sorted({window.class_name for window in self.windows}))

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "windows": [
                {
                    "id": window.window_id,
                    "class_name": window.class_name,
                    "width": window.width,
                    "height": window.height,
                    "step": window.step,
                }
                for window in self.windows
            ],
            "target_colors": {key: list(value) for key, value in sorted(self.target_colors.items())},
            "color_tolerance": self.color_tolerance,
            "color_ratio_threshold": self.color_ratio_threshold,
            "edge_pixel_threshold": self.edge_pixel_threshold,
            "edge_density_threshold": self.edge_density_threshold,
            "fusion_weights": {"color": self.color_weight, "edge": self.edge_weight},
            "detection_threshold": self.detection_threshold,
            "nms_iou_threshold": self.nms_iou_threshold,
        }


@dataclass(frozen=True)
class RouteRule:
    name: str
    priority: int
    frame_budget: int


@dataclass(frozen=True)
class RouteConfigIssue:
    route: str
    message: str


@dataclass(frozen=True)
class MatchingConfig:
    max_workers: int
    total_frame_budget: int
    iou_match_threshold: float
    candidate_pool_capacity: dict[str, int]
    route_rules: dict[str, RouteRule]
    default_priority: int
    default_frame_budget: int
    snapshot_every_frames: int
    route_issues: tuple[RouteConfigIssue, ...]
    source: dict[str, Any]

    def rule_for(self, route: str) -> RouteRule:
        return self.route_rules.get(
            route,
            RouteRule(
                name=route,
                priority=self.default_priority,
                frame_budget=self.default_frame_budget,
            ),
        )

    def semantic_payload(self) -> dict[str, Any]:
        return {
            "total_frame_budget": self.total_frame_budget,
            "iou_match_threshold": self.iou_match_threshold,
            "candidate_pool_capacity": dict(sorted(self.candidate_pool_capacity.items())),
            "routes": [
                {
                    "name": rule.name,
                    "priority": rule.priority,
                    "frame_budget": rule.frame_budget,
                }
                for rule in sorted(self.route_rules.values(), key=lambda item: item.name)
            ],
            "default_priority": self.default_priority,
            "default_frame_budget": self.default_frame_budget,
        }


def load_detection_config(path: str | Path) -> DetectionConfig:
    raw = _load_json_object(path)
    errors: list[str] = []

    windows_raw = raw.get("windows")
    windows: list[WindowSpec] = []
    if not isinstance(windows_raw, list) or not windows_raw:
        errors.append("windows must be a non-empty list")
        windows_raw = []

    seen_ids: set[str] = set()
    for index, item in enumerate(windows_raw):
        prefix = f"windows[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        class_name = item.get("class_name", item.get("class"))
        width = item.get("width")
        height = item.get("height")
        step = item.get("step")
        window_id = item.get("id", f"window-{index}")
        if not isinstance(class_name, str) or not class_name.strip():
            errors.append(f"{prefix}.class_name must be a non-empty string")
            class_name = ""
        if not _positive_int(width):
            errors.append(f"{prefix}.width must be a positive integer")
            width = 0
        if not _positive_int(height):
            errors.append(f"{prefix}.height must be a positive integer")
            height = 0
        if not _positive_int(step):
            errors.append(f"{prefix}.step must be a positive integer")
            step = 0
        if not isinstance(window_id, str) or not window_id:
            errors.append(f"{prefix}.id must be a non-empty string")
            window_id = f"window-{index}"
        if window_id in seen_ids:
            errors.append(f"{prefix}.id duplicates {window_id!r}")
        seen_ids.add(window_id)
        if class_name and _positive_int(width) and _positive_int(height) and _positive_int(step):
            windows.append(WindowSpec(window_id, class_name, width, height, step, index))

    target_colors_raw = raw.get("target_colors")
    target_colors: dict[str, tuple[int, int, int]] = {}
    if not isinstance(target_colors_raw, dict) or not target_colors_raw:
        errors.append("target_colors must be a non-empty object")
        target_colors_raw = {}
    for class_name, color in target_colors_raw.items():
        if not isinstance(class_name, str) or not class_name:
            errors.append("target_colors contains an empty class name")
            continue
        parsed = _color(color)
        if parsed is None:
            errors.append(f"target_colors.{class_name} must contain three integers in [0, 255]")
        else:
            target_colors[class_name] = parsed
    for window in windows:
        if window.class_name not in target_colors:
            errors.append(f"target_colors is missing class {window.class_name!r}")

    color_tolerance = _bounded_int(raw.get("color_tolerance"), "color_tolerance", 0, 255, errors)
    color_ratio_threshold = _bounded_number(
        raw.get("color_ratio_threshold"), "color_ratio_threshold", 0.0, 1.0, errors
    )
    edge_pixel_threshold = _bounded_number(
        raw.get("edge_pixel_threshold"), "edge_pixel_threshold", 0.0, 2550.0, errors
    )
    edge_density_threshold = _bounded_number(
        raw.get("edge_density_threshold"), "edge_density_threshold", 0.0, 1.0, errors
    )

    weights_raw = raw.get("fusion_weights")
    color_weight = 0.0
    edge_weight = 0.0
    if not isinstance(weights_raw, dict):
        errors.append("fusion_weights must be an object with color and edge weights")
    else:
        color_weight = _nonnegative_number(weights_raw.get("color"), "fusion_weights.color", errors)
        edge_weight = _nonnegative_number(weights_raw.get("edge"), "fusion_weights.edge", errors)
        if color_weight + edge_weight <= 0.0:
            errors.append("fusion_weights must have a positive sum")

    detection_threshold = _bounded_number(
        raw.get("detection_threshold"), "detection_threshold", 0.0, 1.0, errors
    )
    nms_iou_threshold = _bounded_number(
        raw.get("nms_iou_threshold"), "nms_iou_threshold", 0.0, 1.0, errors
    )

    if errors:
        raise ConfigError(errors)
    return DetectionConfig(
        windows=tuple(windows),
        target_colors=target_colors,
        color_tolerance=color_tolerance,
        color_ratio_threshold=color_ratio_threshold,
        edge_pixel_threshold=edge_pixel_threshold,
        edge_density_threshold=edge_density_threshold,
        color_weight=color_weight,
        edge_weight=edge_weight,
        detection_threshold=detection_threshold,
        nms_iou_threshold=nms_iou_threshold,
        source=raw,
    )


def load_matching_config(path: str | Path) -> MatchingConfig:
    raw = _load_json_object(path)
    errors: list[str] = []

    max_workers = _positive_int_value(raw.get("max_workers"), "max_workers", errors)
    total_frame_budget = _nonnegative_int_value(
        raw.get("total_frame_budget"), "total_frame_budget", errors
    )
    iou_match_threshold = _bounded_number(
        raw.get("iou_match_threshold"), "iou_match_threshold", 0.0, 1.0, errors
    )

    capacity_raw = raw.get("candidate_pool_capacity")
    capacities: dict[str, int] = {}
    if not isinstance(capacity_raw, dict) or not capacity_raw:
        errors.append("candidate_pool_capacity must be a non-empty object")
    else:
        for class_name, value in capacity_raw.items():
            if not isinstance(class_name, str) or not class_name:
                errors.append("candidate_pool_capacity contains an empty class name")
            elif not _positive_int(value):
                errors.append(f"candidate_pool_capacity.{class_name} must be a positive integer")
            else:
                capacities[class_name] = int(value)

    default_priority = 0
    default_frame_budget = total_frame_budget
    default_raw = raw.get("default_route")
    if default_raw is not None:
        if not isinstance(default_raw, dict):
            errors.append("default_route must be an object when provided")
        else:
            default_priority = _int_value(
                default_raw.get("priority", 0), "default_route.priority", errors
            )
            default_frame_budget = _nonnegative_int_value(
                default_raw.get("frame_budget", total_frame_budget),
                "default_route.frame_budget",
                errors,
            )

    routes_raw = raw.get("routes", [])
    route_issues: list[RouteConfigIssue] = []
    route_rules: dict[str, RouteRule] = {}
    if not isinstance(routes_raw, list):
        errors.append("routes must be a list when provided")
        routes_raw = []
    for index, item in enumerate(routes_raw):
        location = f"routes[{index}]"
        if not isinstance(item, dict):
            route_issues.append(RouteConfigIssue(location, "route entry must be an object"))
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            route_issues.append(RouteConfigIssue(location, "route name must be a non-empty string"))
            continue
        name = name.strip().replace("\\", "/")
        priority = item.get("priority")
        frame_budget = item.get("frame_budget")
        local_errors: list[str] = []
        if not _is_int(priority):
            local_errors.append("priority must be an integer")
        if not _is_int(frame_budget) or int(frame_budget) < 0:
            local_errors.append("frame_budget must be a non-negative integer")
        if local_errors:
            route_issues.append(RouteConfigIssue(name, "; ".join(local_errors)))
            continue
        if name in route_rules:
            route_issues.append(RouteConfigIssue(name, "duplicate route entry ignored"))
            continue
        route_rules[name] = RouteRule(name, int(priority), int(frame_budget))

    snapshot_every_frames = 1
    if "snapshot_every_frames" in raw:
        snapshot_every_frames = _positive_int_value(
            raw.get("snapshot_every_frames"), "snapshot_every_frames", errors
        )

    if errors:
        raise ConfigError(errors)
    return MatchingConfig(
        max_workers=max_workers,
        total_frame_budget=total_frame_budget,
        iou_match_threshold=iou_match_threshold,
        candidate_pool_capacity=capacities,
        route_rules=route_rules,
        default_priority=default_priority,
        default_frame_budget=default_frame_budget,
        snapshot_every_frames=snapshot_every_frames,
        route_issues=tuple(route_issues),
        source=raw,
    )


def validate_config_combination(detection: DetectionConfig, matching: MatchingConfig) -> None:
    errors: list[str] = []
    missing = [name for name in detection.class_names if name not in matching.candidate_pool_capacity]
    if missing:
        errors.append(
            "candidate_pool_capacity is missing detector classes: " + ", ".join(missing)
        )
    if errors:
        raise ConfigError(errors)


def semantic_config_hash(detection: DetectionConfig, matching: MatchingConfig) -> str:
    payload = {
        "detection": detection.semantic_payload(),
        "matching": matching.semantic_payload(),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_json_object(path: str | Path) -> dict[str, Any]:
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError([f"cannot read JSON config {path}: {exc}"]) from exc
    if not isinstance(payload, dict):
        raise ConfigError([f"config {path} must contain a JSON object"])
    return payload


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any) -> bool:
    return _is_int(value) and value > 0


def _positive_int_value(value: Any, name: str, errors: list[str]) -> int:
    if not _positive_int(value):
        errors.append(f"{name} must be a positive integer")
        return 1
    return int(value)


def _nonnegative_int_value(value: Any, name: str, errors: list[str]) -> int:
    if not _is_int(value) or value < 0:
        errors.append(f"{name} must be a non-negative integer")
        return 0
    return int(value)


def _int_value(value: Any, name: str, errors: list[str]) -> int:
    if not _is_int(value):
        errors.append(f"{name} must be an integer")
        return 0
    return int(value)


def _bounded_int(
    value: Any, name: str, minimum: int, maximum: int, errors: list[str]
) -> int:
    if not _is_int(value) or not minimum <= int(value) <= maximum:
        errors.append(f"{name} must be an integer in [{minimum}, {maximum}]")
        return minimum
    return int(value)


def _bounded_number(
    value: Any, name: str, minimum: float, maximum: float, errors: list[str]
) -> float:
    if not _is_number(value) or not minimum <= float(value) <= maximum:
        errors.append(f"{name} must be a finite number in [{minimum}, {maximum}]")
        return minimum
    return float(value)


def _nonnegative_number(value: Any, name: str, errors: list[str]) -> float:
    if not _is_number(value) or float(value) < 0.0:
        errors.append(f"{name} must be a finite non-negative number")
        return 0.0
    return float(value)


def _color(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    if any(not _is_int(component) or not 0 <= component <= 255 for component in value):
        return None
    return tuple(int(component) for component in value)  # type: ignore[return-value]
