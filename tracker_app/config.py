from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .errors import TrackingError
from .jsonio import read_json


DEFAULT_CONFIG: dict[str, Any] = {
    "generator": {
        "sequence_count": 2,
        "frames": 40,
        "targets_per_frame": 3,
        "canvas": [640, 360],
        "seed": 1103,
        "occlusion_intervals": [[9, 11], [23, 25]],
        "disappearance_intervals": [[16, 17], [31, 32]],
        "crossing_frame": 20,
        "revision": 1,
    },
    "tracker": {
        "iou_weight": 0.62,
        "appearance_weight": 0.38,
        "match_gate": 0.64,
        "lost_after_misses": 1,
        "terminate_after_misses": 6,
        "velocity_window": 2,
        "appearance_decimals": 6,
        "cost_decimals": 10,
    },
    "gap": {
        "max_gap_frames": 3,
        "velocity_extrapolation": True,
        "parallel_gap_completion": True,
        "manual_completion_plans": [],
    },
    "resources": {
        "max_detections_per_frame": 12,
        "max_appearance_cache_entries": 48,
        "max_parallel_gap_tracks": 4,
        "policy": "degrade",
    },
    "validation": {
        "allow_out_of_bounds": False,
        "require_frame_zero": True,
        "require_contiguous_frames": True,
    },
    "paths": {
        "runtime_dir": "runtime",
        "output_dir": "artifacts/demo/output",
        "data_dir": "artifacts/demo/data",
        "fixture_dir": "artifacts/demo/fixtures",
    },
}


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, dict)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        config = deepcopy(DEFAULT_CONFIG)
    else:
        loaded = read_json(path)
        if not isinstance(loaded, dict):
            raise TrackingError("E_INVALID_INPUT", "configuration root must be an object")
        config = _deep_merge(DEFAULT_CONFIG, loaded)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    try:
        gen = config["generator"]
        tracker = config["tracker"]
        gap = config["gap"]
        resources = config["resources"]
        validation = config["validation"]

        if int(gen["sequence_count"]) < 1:
            raise ValueError("sequence_count must be >= 1")
        if int(gen["frames"]) < 1:
            raise ValueError("frames must be >= 1")
        if int(gen["targets_per_frame"]) < 1:
            raise ValueError("targets_per_frame must be >= 1")
        canvas = gen["canvas"]
        if len(canvas) != 2 or float(canvas[0]) <= 0 or float(canvas[1]) <= 0:
            raise ValueError("canvas must be [positive_width, positive_height]")
        for field in ("occlusion_intervals", "disappearance_intervals"):
            intervals = gen[field]
            if not isinstance(intervals, list):
                raise ValueError(f"generator.{field} must be a list")
            for interval in intervals:
                if len(interval) != 2 or int(interval[0]) < 0 or int(interval[1]) < int(interval[0]):
                    raise ValueError(f"invalid interval in generator.{field}: {interval}")

        iou_weight = float(tracker["iou_weight"])
        appearance_weight = float(tracker["appearance_weight"])
        if iou_weight < 0 or appearance_weight < 0:
            raise ValueError("matching weights must be non-negative")
        if abs(iou_weight + appearance_weight - 1.0) > 1e-9:
            raise ValueError("matching weights must sum to 1")
        if int(tracker["lost_after_misses"]) < 1:
            raise ValueError("lost_after_misses must be >= 1")
        if int(tracker["terminate_after_misses"]) <= int(tracker["lost_after_misses"]):
            raise ValueError("terminate_after_misses must exceed lost_after_misses")
        if not 0 < float(tracker["match_gate"]) <= 1:
            raise ValueError("match_gate must be in (0, 1]")
        if int(gap["max_gap_frames"]) < 1:
            raise ValueError("max_gap_frames must be >= 1")
        if int(resources["max_detections_per_frame"]) < 1:
            raise ValueError("max_detections_per_frame must be >= 1")
        if int(resources["max_appearance_cache_entries"]) < 1:
            raise ValueError("max_appearance_cache_entries must be >= 1")
        if int(resources["max_parallel_gap_tracks"]) < 1:
            raise ValueError("max_parallel_gap_tracks must be >= 1")
        if resources["policy"] not in {"degrade", "error"}:
            raise ValueError("resources.policy must be 'degrade' or 'error'")
        if not isinstance(validation["allow_out_of_bounds"], bool):
            raise ValueError("allow_out_of_bounds must be boolean")
    except (KeyError, TypeError, ValueError) as exc:
        raise TrackingError("E_INVALID_INPUT", f"invalid configuration: {exc}") from exc


def resolve_paths(base_dir: str | Path, config: dict[str, Any]) -> dict[str, Path]:
    base = Path(base_dir)
    return {
        "runtime": (base / config["paths"]["runtime_dir"]).resolve(),
        "output": (base / config["paths"]["output_dir"]).resolve(),
        "data": (base / config["paths"]["data_dir"]).resolve(),
        "fixtures": (base / config["paths"]["fixture_dir"]).resolve(),
    }
