"""Deterministic editing operations with an append-only edit log."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict

import numpy as np

from .config import CHANNELS, VersionMap
from .errors import FormatInvalid
from .models import MASK_DAMAGED, MASK_INTACT, UnifiedProject


def _next_seq(project: UnifiedProject) -> int:
    if not project.edit_log:
        return 0
    return max(int(entry["seq"]) for entry in project.edit_log) + 1


def edit_mask_rect(project: UnifiedProject, x: int, y: int, width: int,
                   height: int, value: int) -> UnifiedProject:
    if value not in (0, 255):
        raise FormatInvalid("掩码取值只能为 0(完好) 或 255(缺损)",
                            context={"param_key": "mask_value", "value": value})
    img_h, img_w = project.mask.shape
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(img_w, x + width)
    y1 = min(img_h, y + height)
    if width <= 0 or height <= 0 or x0 >= x1 or y0 >= y1:
        raise FormatInvalid(
            "掩码矩形区域与图像无交集或尺寸非法",
            context={"rect": [x, y, width, height],
                     "image_shape": [img_h, img_w]})
    changed = int(np.count_nonzero(
        project.mask[y0:y1, x0:x1] != np.uint8(value)))
    new_mask = project.mask.copy()
    new_mask[y0:y1, x0:x1] = np.uint8(value)
    entry = {
        "seq": _next_seq(project),
        "op": "edit_mask_rect",
        "rect": [int(x), int(y), int(width), int(height)],
        "clipped_rect": [int(x0), int(y0), int(x1 - x0), int(y1 - y0)],
        "value": int(value),
        "changed_pixels": changed,
    }
    return replace(project, mask=new_mask,
                   edit_log=project.edit_log + [entry])


def set_channel_weight(project: UnifiedProject, channel: str,
                       weight: float, version_map: VersionMap
                       ) -> UnifiedProject:
    if channel not in CHANNELS:
        raise FormatInvalid(f"未知通道 {channel}",
                            context={"param_key": "channel",
                                     "channel": channel})
    if not 0.0 <= float(weight) <= 1.0:
        raise FormatInvalid("通道权重必须位于 [0,1]",
                            context={"param_key": f"channel_weights.{channel}",
                                     "weight": weight})
    if project.source_version == 1:
        raise FormatInvalid(
            "v1 工程不支持直接编辑浮点权重，请先切换为 v2",
            context={"param_key": "channel_weights",
                     "source_version": 1})
    new_style = replace(project.style)
    new_style.channel_weights = dict(project.style.channel_weights)
    old_value = new_style.channel_weights[channel]
    new_style.channel_weights[channel] = float(weight)
    entry = {
        "seq": _next_seq(project),
        "op": "set_channel_weight",
        "channel": channel,
        "old_value": float(old_value),
        "new_value": float(weight),
    }
    return replace(project, style=new_style,
                   edit_log=project.edit_log + [entry])


def set_intensity(project: UnifiedProject, intensity: int,
                  version_map: VersionMap) -> UnifiedProject:
    weights = version_map.weights_for(intensity)  # validates档位
    new_style = replace(project.style, intensity=int(intensity))
    if project.source_version == 2:
        new_style.channel_weights = weights
    entry = {
        "seq": _next_seq(project),
        "op": "set_intensity",
        "old_value": int(project.style.intensity),
        "new_value": int(intensity),
    }
    return replace(project, style=new_style,
                   edit_log=project.edit_log + [entry])


def edit_log_summary(project: UnifiedProject) -> Dict[str, Any]:
    return {
        "operation_count": len(project.edit_log),
        "last_seq": (max((e["seq"] for e in project.edit_log), default=-1)),
        "ops": [e["op"] for e in project.edit_log],
    }
