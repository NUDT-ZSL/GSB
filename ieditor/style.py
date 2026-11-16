"""Deterministic lookup-table style transfer.

A 3-D lookup table indexed by quantized input colors (L levels per channel) is
built once from:
  * per-channel float weights,
  * deterministic reference-image statistics (per-channel mean),
  * the named palette (nearest deterministic hash match on ties).

No random sampling occurs. All blending is integer fixed-point, so the same
project and reference always yield byte-identical styled images.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Tuple

import numpy as np

from .config import CHANNELS, RenderConfig
from .models import PaletteEntry, StyleParams, UnifiedProject


@dataclass
class StyleResult:
    image: np.ndarray
    build_time_ms: float
    table_size: int
    quant_levels: int
    hit_counts: Dict[str, int] = field(default_factory=dict)
    distinct_input_buckets: int = 0
    palette_hits: Dict[str, int] = field(default_factory=dict)

    def stats(self) -> Dict[str, Any]:
        return {
            "build_time_ms": round(self.build_time_ms, 6),
            "table_size": self.table_size,
            "quant_levels": self.quant_levels,
            "distinct_input_buckets": self.distinct_input_buckets,
            "hit_counts": dict(sorted(self.hit_counts.items())),
            "palette_hits": dict(sorted(self.palette_hits.items())),
        }


def reference_means(reference_rgb: np.ndarray) -> List[int]:
    flat = reference_rgb.reshape(-1, 3).astype(np.uint32)
    means = np.floor(flat.mean(axis=0) + 0.5).astype(int)
    return [int(v) for v in means]


def _palette_color(palette: List[PaletteEntry], r_idx: int, g_idx: int,
                   b_idx: int) -> Tuple[List[int], str]:
    if not palette:
        return [128, 128, 128], "default_gray"
    best_idx = 0
    best_key = None
    for idx, entry in enumerate(palette):
        color = np.array(entry.color, dtype=np.int64)
        dist = int(np.sum((color - np.array([r_idx, g_idx, b_idx],
                                             dtype=np.int64)) ** 2))
        # Deterministic tie break: smallest palette index then name.
        key = (dist, idx, entry.name)
        if best_key is None or key < best_key:
            best_key = key
            best_idx = idx
    return list(palette[best_idx].color), palette[best_idx].name


def build_lut(style: StyleParams, reference_rgb: np.ndarray,
              config: RenderConfig) -> np.ndarray:
    levels = int(config.quant_levels)
    scale = int(config.fixed_point_scale)
    num = int(config.style_blend_num)
    den = int(config.style_blend_den)
    means = reference_means(reference_rgb)
    weights = {c: int(round(style.channel_weights[c] * scale))
               for c in CHANNELS}

    lut = np.zeros((levels, levels, levels, 3), dtype=np.uint8)
    step = 256 // levels
    for r_idx in range(levels):
        for g_idx in range(levels):
            for b_idx in range(levels):
                base = [min(255, (idx + 1) * step - 1)
                        for idx in (r_idx, g_idx, b_idx)]
                palette_color, _name = _palette_color(
                    style.palette, r_idx, g_idx, b_idx)
                out = [0, 0, 0]
                for channel, name in enumerate(CHANNELS):
                    ref_value = means[channel]
                    weight = weights[name]
                    # weight modulates how strongly the reference mean/palette
                    # pulls the base color toward the target style.
                    target = ((ref_value * weight +
                               palette_color[channel] * (scale - weight))
                              // scale)
                    numerator = base[channel] * (den - num) + target * num
                    value = (numerator + den // 2) // den
                    out[channel] = int(np.clip(value, 0, 255))
                lut[r_idx, g_idx, b_idx] = out
    return lut


def apply_style(project: UnifiedProject, inpainted: np.ndarray,
                reference_rgb: np.ndarray,
                config: RenderConfig) -> StyleResult:
    levels = int(config.quant_levels)
    start = time.perf_counter()
    lut = build_lut(project.style, reference_rgb, config)
    build_time_ms = (time.perf_counter() - start) * 1000.0

    bucket = np.minimum(
        (inpainted.astype(np.uint32) * levels) // 256, levels - 1
    ).astype(np.uint8)
    styled = lut[bucket[..., 0], bucket[..., 1], bucket[..., 2]]

    flat_keys = (bucket[..., 0].astype(np.uint32) * levels * levels +
                 bucket[..., 1].astype(np.uint32) * levels +
                 bucket[..., 2].astype(np.uint32)).reshape(-1)
    unique, counts = np.unique(flat_keys, return_counts=True)
    hit_counts = {f"{int(k):04d}": int(c) for k, c in zip(unique, counts)}

    palette_hits: Dict[str, int] = {}
    for entry in project.style.palette:
        palette_hits[entry.name] = 0
    palette_hits.setdefault("default_gray", 0)
    # Deterministic per-bucket palette attribution for hit statistics.
    count_by_key = {int(k): int(c) for k, c in zip(unique, counts)}
    for key in sorted(count_by_key):
        r_idx = key // (levels * levels)
        g_idx = (key // levels) % levels
        b_idx = key % levels
        _color, name = _palette_color(project.style.palette,
                                      r_idx, g_idx, b_idx)
        palette_hits[name] = palette_hits.get(name, 0) + count_by_key[key]

    return StyleResult(
        image=styled.astype(np.uint8),
        build_time_ms=build_time_ms,
        table_size=levels ** 3,
        quant_levels=levels,
        hit_counts=hit_counts,
        distinct_input_buckets=int(len(unique)),
        palette_hits=dict(sorted(palette_hits.items())),
    )
