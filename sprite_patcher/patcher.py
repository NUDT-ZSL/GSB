"""补丁叠加引擎。

规则：
- 同一格子多个补丁按清单顺序叠加，后处理补丁覆盖先处理补丁的重合像素；
  源像素 alpha=0（完全透明）不覆盖目标，使用 ``alpha_composite`` 实现。
- 补丁超出目标格子的部分裁剪，仍应用落在格内的部分，并统计被裁像素数。
- 浮点偏移按四舍五入（半数远离零）取整为最近整数像素。
- 补丁文件缺失/损坏/无法解码时跳过，格子保持原样，结果中标记不可用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .config import Config, PatchSpec
from .errors import PatchLoadError
from .geometry import Geometry


@dataclass
class PatchOutcome:
    order: int
    row: int
    column: int
    index: int
    source_path: str
    requested_offset: dict
    actual_offset: dict | None = None
    patch_size: dict | None = None
    status: str = "applied"          # applied | unavailable
    reason: str | None = None
    clipped_pixels: int = 0
    applied_pixels: int = 0           # 本补丁实际写入的非透明像素数（格内）


@dataclass
class CellStat:
    row: int
    column: int
    index: int
    patch_orders: list[int] = field(default_factory=list)
    covered_pixels: int = 0           # 被非透明补丁像素覆盖的格内像素（并集）
    retained_original_pixels: int = 0


def round_half_up(value: float) -> int:
    """四舍五入到最近整数，0.5 远离零（2.6 -> 3，-2.6 -> -3）。"""
    if value == 0:
        return 0
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def _load_patch_image(path: Path) -> Image.Image:
    try:
        with Image.open(path) as img:
            img.load()  # 强制解码，截断/损坏文件在此处抛错
            return img.convert("RGBA")
    except FileNotFoundError as exc:
        raise PatchLoadError(f"补丁文件不存在: {path}") from exc
    except UnidentifiedImageError as exc:
        raise PatchLoadError(f"无法识别的图片格式或文件已损坏: {path}") from exc
    except OSError as exc:
        raise PatchLoadError(f"图片解码失败（文件可能已损坏）: {path}（{exc}）") from exc


def apply_patches(
    base: Image.Image, config: Config, geometry: Geometry
) -> tuple[Image.Image, list[PatchOutcome], dict[int, CellStat]]:
    """在底图副本上应用全部补丁，返回 (新图, 每个补丁的结果, 按格子序号的统计)。"""
    if base.mode != "RGBA":
        base = base.convert("RGBA")
    sheet = base.copy()

    outcomes: list[PatchOutcome] = []
    cell_stats: dict[int, CellStat] = {}
    # 每格维护已覆盖像素的并集（格内局部坐标），用于统计保留原像素数
    covered_sets: dict[int, set[tuple[int, int]]] = {}

    for spec in config.patches:
        cell_index = spec.row * config.grid.columns + spec.column
        left, top, right, bottom = geometry.cell_box(spec.row, spec.column)
        outcome = PatchOutcome(
            order=spec.order,
            row=spec.row,
            column=spec.column,
            index=cell_index,
            source_path=str(spec.path),
            requested_offset={"x": spec.offset_x, "y": spec.offset_y},
        )

        try:
            patch = _load_patch_image(spec.path)
        except PatchLoadError as exc:
            outcome.status = "unavailable"
            outcome.reason = str(exc)
            outcomes.append(outcome)
            continue

        ax = round_half_up(spec.offset_x)
        ay = round_half_up(spec.offset_y)
        outcome.actual_offset = {"x": ax, "y": ay}
        outcome.patch_size = {"width": patch.width, "height": patch.height}

        cell_w = right - left
        cell_h = bottom - top
        total = patch.width * patch.height

        # 补丁在精灵表坐标系中的目标矩形
        dest_left = left + ax
        dest_top = top + ay
        dest_right = dest_left + patch.width
        dest_bottom = dest_top + patch.height

        # 与格子求交，交集即裁剪后可应用区域
        clip_left = max(dest_left, left)
        clip_top = max(dest_top, top)
        clip_right = min(dest_right, right)
        clip_bottom = min(dest_bottom, bottom)

        in_cell_w = max(0, clip_right - clip_left)
        in_cell_h = max(0, clip_bottom - clip_top)
        in_cell_count = in_cell_w * in_cell_h
        outcome.clipped_pixels = total - in_cell_count

        stat = cell_stats.setdefault(
            cell_index,
            CellStat(spec.row, spec.column, cell_index),
        )
        stat.patch_orders.append(spec.order)
        covered = covered_sets.setdefault(cell_index, set())

        if in_cell_count > 0:
            # 补丁内对应的源矩形
            src_left = clip_left - dest_left
            src_top = clip_top - dest_top
            piece = patch.crop(
                (src_left, src_top, src_left + in_cell_w, src_top + in_cell_h)
            )
            target = sheet.crop((clip_left, clip_top, clip_right, clip_bottom))
            merged = Image.alpha_composite(target, piece)
            sheet.paste(merged, (clip_left, clip_top))

            px = piece.load()
            for sy in range(in_cell_h):
                for sx in range(in_cell_w):
                    if px[sx, sy][3] > 0:
                        outcome.applied_pixels += 1
                        covered.add((sx + clip_left - left, sy + clip_top - top))

        stat.covered_pixels = len(covered)
        stat.retained_original_pixels = cell_w * cell_h - len(covered)
        outcomes.append(outcome)

    for cell_index, stat in cell_stats.items():
        cell_w = geometry.cell_w
        cell_h = geometry.cell_h
        stat.retained_original_pixels = cell_w * cell_h - stat.covered_pixels

    return sheet, outcomes, cell_stats
