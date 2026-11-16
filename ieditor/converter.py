"""Bidirectional v1 <-> v2 project conversion with explicit loss records."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import List, Tuple

import numpy as np

from .config import CHANNELS, QuantRules, VersionMap
from .errors import FormatInvalid
from .fingerprint import structure_fingerprint
from .models import (MASK_DAMAGED, LossEntry, PaletteEntry, StyleParams,
                     UnifiedProject)
from . import parsers

PALETTE_NAMES = ("ink", "coral", "amber", "leaf", "cyan", "azure",
                 "violet", "magenta", "slate", "sand", "rose", "mint",
                 "sky", "plum", "steel", "peach")


def build_palette(reference_rgb: np.ndarray, buckets: int) -> List[PaletteEntry]:
    """Deterministic named palette: equal-bucket mean colors of the reference."""
    flat = reference_rgb.reshape(-1, 3).astype(np.uint32)
    order = np.lexsort((flat[:, 2], flat[:, 1], flat[:, 0]))
    sorted_pixels = flat[order]
    total = sorted_pixels.shape[0]
    palette: List[PaletteEntry] = []
    for idx in range(buckets):
        start = (idx * total) // buckets
        end = ((idx + 1) * total) // buckets
        if end <= start:
            end = min(total, start + 1)
        chunk = sorted_pixels[start:end]
        mean = np.floor(chunk.mean(axis=0) + 0.5).astype(int)
        name = PALETTE_NAMES[idx % len(PALETTE_NAMES)]
        palette.append(PaletteEntry(name=name, color=[int(c) for c in mean]))
    return palette


def upgrade_v1_to_v2(project: UnifiedProject, version_map: VersionMap,
                     reference_rgb: np.ndarray) -> UnifiedProject:
    """Expand integer intensity档位 into per-channel weights + palette."""
    if project.source_version != 1:
        raise FormatInvalid("upgrade_v1_to_v2 仅接受 v1 工程",
                            context={"source_version": project.source_version})
    weights = version_map.weights_for(project.style.intensity)
    palette = build_palette(reference_rgb, version_map.palette_buckets)
    style = StyleParams(intensity=project.style.intensity,
                        channel_weights=weights, palette=palette)
    meta = dict(project.meta)
    meta.update({
        "encoding": "json_base64_8bit",
        "upgraded_from": 1,
        "palette_name": f"{project.project_id}_palette",
        "weight_expansion": {
            "intensity": project.style.intensity,
            "channel_weights": dict(sorted(weights.items())),
        },
    })
    return replace(project, source_version=2, style=style, meta=meta,
                   loss_log=list(project.loss_log))


def _fold_intensity(weights: dict, version_map: VersionMap,
                    rules: QuantRules) -> Tuple[int, dict]:
    """Nearest L2 match over the configured intensity档位."""
    target = np.array([weights[c] for c in CHANNELS], dtype=np.float64)
    best_level = None
    best_dist = None
    for level in rules.intensity_levels:
        candidate_w = version_map.weights_for(level)
        candidate = np.array([candidate_w[c] for c in CHANNELS],
                             dtype=np.float64)
        dist = float(np.sum((target - candidate) ** 2))
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best_level = level
    assert best_level is not None
    folded = version_map.weights_for(best_level)
    per_channel = {c: float(weights[c] - folded[c]) for c in CHANNELS}
    return int(best_level), {"folded_intensity": best_level,
                             "l2_distance": best_dist,
                             "per_channel_delta": per_channel,
                             "original_weights": dict(weights),
                             "folded_weights": folded,
                             "fold_method": rules.fold_method}


def downgrade_v2_to_v1(project: UnifiedProject, version_map: VersionMap,
                       rules: QuantRules) -> UnifiedProject:
    """Fold per-channel float weights into one integer intensity档位."""
    if project.source_version != 2:
        raise FormatInvalid("downgrade_v2_to_v1 仅接受 v2 工程",
                            context={"source_version": project.source_version})

    intensity, fold_detail = _fold_intensity(
        project.style.channel_weights, version_map, rules)
    original_weights = dict(project.style.channel_weights)

    losses: List[LossEntry] = list(project.loss_log)
    losses.append(LossEntry(
        kind="intensity_fold",
        detail="分通道浮点权重折叠为单一整型强度档位",
        values=fold_detail,
    ))

    quantized = {c: round(original_weights[c], rules.weight_quant_decimals)
                 for c in CHANNELS}
    precision = {c: float(original_weights[c] - quantized[c])
                 for c in CHANNELS}
    losses.append(LossEntry(
        kind="float_precision",
        detail="分通道浮点权重定点量化精度损失",
        values={"decimals": rules.weight_quant_decimals,
                "original": original_weights,
                "quantized": quantized,
                "rounding_delta": precision},
    ))

    dropped = [p.to_dict() for p in project.style.palette]
    losses.append(LossEntry(
        kind="palette_dropped",
        detail="v1 二进制工程不携带命名调色板，降级时丢弃",
        values={"entry_count": len(dropped), "entries": dropped},
    ))

    unique_values = sorted(int(v) for v in np.unique(project.mask))
    losses.append(LossEntry(
        kind="mask_bit_width",
        detail="掩码位宽差异：v2 逐像素 8 位 0/255 折叠为 v1 1 位行程",
        values={"v2_bit_width": rules.mask_bit_width_v2,
                "v1_bit_width": rules.mask_bit_width_v1,
                "v2_unique_values": unique_values,
                "damaged_pixels": int(np.count_nonzero(
                    project.mask == MASK_DAMAGED))},
    ))

    style = StyleParams(intensity=intensity, channel_weights={}, palette=[])
    meta = {"encoding": "binary_rle_1bit",
            "downgraded_from": 2,
            "original_weights": original_weights}
    return replace(project, source_version=1, style=style, meta=meta,
                   loss_log=losses)


def convert_project(project: UnifiedProject, target_version: int,
                    version_map: VersionMap, rules: QuantRules,
                    reference_rgb: np.ndarray | None = None) -> UnifiedProject:
    if target_version == project.source_version:
        return project
    if target_version == 2:
        return upgrade_v1_to_v2(project, version_map,
                                reference_rgb if reference_rgb is not None
                                else _missing_ref())
    if target_version == 1:
        return downgrade_v2_to_v1(project, version_map, rules)
    from .errors import VersionUnsupported
    raise VersionUnsupported(f"未知目标版本号: {target_version}",
                             context={"target_version": target_version})


def _missing_ref():
    raise FormatInvalid("v1 升 v2 构建调色板需要风格参考图",
                        context={"param_key": "style_ref_path"})


def save_project(project: UnifiedProject, path: str | Path) -> None:
    if project.source_version == 1:
        parsers.write_v1(project, path)
    elif project.source_version == 2:
        parsers.write_v2(project, path)
    else:
        from .errors import VersionUnsupported
        raise VersionUnsupported(
            f"未知版本号: {project.source_version}",
            context={"version": project.source_version})


def roundtrip_mask_equal(project_a: UnifiedProject,
                         project_b: UnifiedProject) -> bool:
    return bool(np.array_equal(project_a.mask, project_b.mask))


def conversion_fingerprint(project: UnifiedProject) -> str:
    return structure_fingerprint(project)
