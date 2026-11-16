"""Local JSON configuration loading: render config, version map, quant rules."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .errors import FormatInvalid

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"

CHANNELS = ("R", "G", "B")


@dataclass
class RenderConfig:
    neighborhood_radius: int = 2
    fixed_point_scale: int = 65536          # 2**16
    quant_levels: int = 16                  # L per channel for style LUT
    style_blend_num: int = 6                # fixed-point style blend numerator
    style_blend_den: int = 10
    deviation_threshold: float = 0.12
    fill_tie_priority: str = "row-major"
    neighbor_order: str = "row-major"

    @classmethod
    def from_dict(cls, data: dict) -> "RenderConfig":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise FormatInvalid(
                f"unknown render config keys: {sorted(unknown)}",
                context={"keys": sorted(unknown)})
        return cls(**{k: data[k] for k in known if k in data})


@dataclass
class VersionMap:
    """v1 intensity档位 -> per-channel weights (v2 expansion)."""
    intensity_weights: Dict[int, Dict[str, float]]
    palette_buckets: int = 8

    @classmethod
    def from_dict(cls, data: dict) -> "VersionMap":
        raw = data["intensity_weights"]
        weights: Dict[int, Dict[str, float]] = {}
        for key, val in raw.items():
            missing = [c for c in CHANNELS if c not in val]
            if missing:
                raise FormatInvalid(
                    f"intensity {key} missing channel weights",
                    context={"intensity": key, "missing": missing})
            weights[int(key)] = {c: float(val[c]) for c in CHANNELS}
        return cls(intensity_weights=weights,
                   palette_buckets=int(data.get("palette_buckets", 8)))

    def weights_for(self, intensity: int) -> Dict[str, float]:
        if intensity not in self.intensity_weights:
            raise FormatInvalid(
                f"intensity档位 {intensity} 不在版本映射表中",
                context={"param_key": "intensity", "intensity": intensity})
        return dict(self.intensity_weights[intensity])


@dataclass
class QuantRules:
    """v2 -> v1 folding rules."""
    intensity_levels: List[int]
    fold_method: str = "nearest_l2"
    weight_quant_decimals: int = 4
    mask_bit_width_v1: int = 1
    mask_bit_width_v2: int = 8

    @classmethod
    def from_dict(cls, data: dict) -> "QuantRules":
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        unknown = set(data) - known
        if unknown:
            raise FormatInvalid(
                f"unknown quant rule keys: {sorted(unknown)}",
                context={"keys": sorted(unknown)})
        levels = sorted(int(v) for v in data["intensity_levels"])
        return cls(intensity_levels=levels,
                   fold_method=str(data.get("fold_method", "nearest_l2")),
                   weight_quant_decimals=int(
                       data.get("weight_quant_decimals", 4)),
                   mask_bit_width_v1=int(data.get("mask_bit_width_v1", 1)),
                   mask_bit_width_v2=int(data.get("mask_bit_width_v2", 8)))


def _load_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise FormatInvalid(f"配置文件不存在: {path}",
                            context={"path": str(path)})
    except json.JSONDecodeError as exc:
        raise FormatInvalid(f"配置 JSON 解析失败: {path}: {exc}",
                            context={"path": str(path), "line": exc.lineno})


def load_render_config(path: Path | None = None) -> RenderConfig:
    path = path or DEFAULT_CONFIG_DIR / "render_config.json"
    return RenderConfig.from_dict(_load_json(Path(path)))


def load_version_map(path: Path | None = None) -> VersionMap:
    path = path or DEFAULT_CONFIG_DIR / "version_map.json"
    return VersionMap.from_dict(_load_json(Path(path)))


def load_quant_rules(path: Path | None = None) -> QuantRules:
    path = path or DEFAULT_CONFIG_DIR / "quant_rules.json"
    return QuantRules.from_dict(_load_json(Path(path)))
