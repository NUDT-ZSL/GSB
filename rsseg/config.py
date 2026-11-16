from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import InputError
from .util import read_json, sha256_json


FEATURE_NAMES = ("ndvi", "ndwi", "edge_density", "slope", "brightness", "elevation")
CLASS_IDS = (1, 2, 3, 4)
FUSION_STRATEGIES = ("distance_weighted_vote", "uniform_vote")


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise InputError("E_CONFIG_INVALID", "config", f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InputError("E_CONFIG_INVALID", "config", f"{name} must be a non-negative integer")
    return value


def _pair(value: Any, name: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise InputError("E_CONFIG_INVALID", "config", f"{name} must contain two integers")
    return _positive_int(value[0], name), _positive_int(value[1], name)


@dataclass(frozen=True)
class SegmentationConfig:
    raw: dict[str, Any]
    config_hash: str
    classification_fingerprint: str
    window_size: tuple[int, int]
    stride: tuple[int, int]
    context_overlap: int
    feature_thresholds: dict[str, float]
    class_weights: dict[int, dict[str, float]]
    class_names: dict[int, str]
    epsilon: float
    edge_scale: float
    slope_scale: float
    fusion_strategy: str
    min_connected_area: int
    concurrency: int
    recompute_budget: int

    @property
    def window_height(self) -> int:
        return self.window_size[0]

    @property
    def window_width(self) -> int:
        return self.window_size[1]

    @property
    def stride_height(self) -> int:
        return self.stride[0]

    @property
    def stride_width(self) -> int:
        return self.stride[1]


def _parse_weights(raw: Any) -> dict[int, dict[str, float]]:
    if not isinstance(raw, dict):
        raise InputError("E_CONFIG_INVALID", "config", "class_weights must be an object")
    out: dict[int, dict[str, float]] = {}
    for class_id in CLASS_IDS:
        item = raw.get(str(class_id), raw.get(class_id))
        if not isinstance(item, dict):
            raise InputError("E_CONFIG_INVALID", "config", f"class_weights[{class_id}] is missing")
        weights: dict[str, float] = {}
        for key, value in item.items():
            if key != "bias" and key not in FEATURE_NAMES:
                raise InputError("E_CONFIG_INVALID", "config", f"unknown classification weight {key!r}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InputError("E_CONFIG_INVALID", "config", f"weight {class_id}.{key} must be numeric")
            weights[str(key)] = float(value)
        out[class_id] = weights
    return out


def parse_config(data: dict[str, Any]) -> SegmentationConfig:
    if not isinstance(data, dict):
        raise InputError("E_CONFIG_INVALID", "config", "configuration must be a JSON object")
    window = _pair(data.get("window_size"), "window_size")
    stride = _pair(data.get("stride"), "stride")
    if window[0] < stride[0] or window[1] < stride[1]:
        raise InputError("E_CONFIG_INVALID", "config", "window_size must be >= stride on both axes")
    context = _nonnegative_int(data.get("context_overlap", 0), "context_overlap")
    if context * 2 > min(window):
        raise InputError("E_CONFIG_INVALID", "config", "context_overlap is too large for window_size")

    thresholds_raw = data.get("feature_thresholds")
    if not isinstance(thresholds_raw, dict):
        raise InputError("E_CONFIG_INVALID", "config", "feature_thresholds must be an object")
    thresholds: dict[str, float] = {}
    for name in FEATURE_NAMES:
        value = thresholds_raw.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise InputError("E_CONFIG_INVALID", "config", f"feature_thresholds.{name} must be numeric")
        thresholds[name] = float(value)

    class_weights = _parse_weights(data.get("class_weights"))
    names_raw = data.get("class_names", {})
    class_names = {
        cid: str(names_raw.get(str(cid), names_raw.get(cid, f"class_{cid}")))
        for cid in CLASS_IDS
    }
    strategy = str(data.get("fusion_strategy", "distance_weighted_vote"))
    if strategy not in FUSION_STRATEGIES:
        raise InputError("E_CONFIG_INVALID", "config", f"unsupported fusion_strategy {strategy!r}")

    epsilon = float(data.get("epsilon", 1e-6))
    if epsilon <= 0:
        raise InputError("E_CONFIG_INVALID", "config", "epsilon must be positive")
    edge_scale = float(data.get("edge_scale", 2.0))
    slope_scale = float(data.get("slope_scale", 4.0))
    if edge_scale <= 0 or slope_scale <= 0:
        raise InputError("E_CONFIG_INVALID", "config", "edge_scale and slope_scale must be positive")

    min_area = _nonnegative_int(data.get("min_connected_area", 0), "min_connected_area")
    concurrency = _positive_int(data.get("concurrency", 1), "concurrency")
    budget = _positive_int(data.get("recompute_budget", 1), "recompute_budget")

    classification_payload = {
        "schema": 1,
        "window_size": list(window),
        "stride": list(stride),
        "context_overlap": context,
        "feature_thresholds": thresholds,
        "class_weights": {str(k): v for k, v in class_weights.items()},
        "epsilon": epsilon,
        "edge_scale": edge_scale,
        "slope_scale": slope_scale,
    }
    return SegmentationConfig(
        raw=data,
        config_hash=sha256_json(data),
        classification_fingerprint=sha256_json(classification_payload),
        window_size=window,
        stride=stride,
        context_overlap=context,
        feature_thresholds=thresholds,
        class_weights=class_weights,
        class_names=class_names,
        epsilon=epsilon,
        edge_scale=edge_scale,
        slope_scale=slope_scale,
        fusion_strategy=strategy,
        min_connected_area=min_area,
        concurrency=concurrency,
        recompute_budget=budget,
    )


def load_config(path: str | Path) -> SegmentationConfig:
    try:
        data = read_json(path)
    except (OSError, ValueError) as exc:
        raise InputError("E_CONFIG_INVALID", "config", f"cannot read config: {exc}") from exc
    return parse_config(data)
