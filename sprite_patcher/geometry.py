"""网格几何：格子尺寸、边带（不可整除余数像素）与坐标换算。"""

from __future__ import annotations

from dataclasses import dataclass

from .config import GridSpec, SheetSpec


@dataclass(frozen=True)
class Band:
    """一条保持原样的余数边带。"""

    side: str            # "right" | "bottom"
    position: str        # 报告用中文位置描述
    pixels: int          # 边带宽度/高度（像素），0 表示无余数


@dataclass(frozen=True)
class Geometry:
    cell_w: int
    cell_h: int
    right_band: Band
    bottom_band: Band

    @property
    def bands(self) -> tuple[Band, ...]:
        return tuple(b for b in (self.right_band, self.bottom_band) if b.pixels > 0)

    def cell_box(self, row: int, col: int) -> tuple[int, int, int, int]:
        """返回格子的 (left, top, right, bottom)，right/bottom 为排他边界。"""
        left = col * self.cell_w
        top = row * self.cell_h
        return left, top, left + self.cell_w, top + self.cell_h


def build_geometry(sheet: SheetSpec, grid: GridSpec) -> Geometry:
    cell_w, rem_w = divmod(sheet.width, grid.columns)
    cell_h, rem_h = divmod(sheet.height, grid.rows)
    if cell_w == 0 or cell_h == 0:
        raise ValueError(
            f"网格数超过精灵表尺寸（表 {sheet.width}x{sheet.height}，"
            f"网格 {grid.columns}列x{grid.rows}行，无法划分出至少 1 像素的格子）"
        )
    right = Band(
        side="right",
        position=(
            f"右侧边带 x={sheet.width - rem_w}..{sheet.width - 1}"
            if rem_w else "右侧无边带（宽度可被列数整除）"
        ),
        pixels=rem_w,
    )
    bottom = Band(
        side="bottom",
        position=(
            f"底部边带 y={sheet.height - rem_h}..{sheet.height - 1}"
            if rem_h else "底部无边带（高度可被行数整除）"
        ),
        pixels=rem_h,
    )
    return Geometry(cell_w, cell_h, right, bottom)


def divisible_hint(sheet: SheetSpec, grid: GridSpec) -> str:
    """整除调整提示（余数为 0 时也给出说明）。"""
    _, rem_w = divmod(sheet.width, grid.columns)
    _, rem_h = divmod(sheet.height, grid.rows)
    hints: list[str] = []
    if rem_w == 0:
        hints.append(f"宽度 {sheet.width} 可被 {grid.columns} 列整除，右侧余数 0 像素")
    else:
        hints.append(
            f"宽度 {sheet.width} 除以 {grid.columns} 列余 {rem_w} 像素；"
            f"若要完全整除可将宽度调整为 {sheet.width - rem_w} 或 {sheet.width + grid.columns - rem_w}"
        )
    if rem_h == 0:
        hints.append(f"高度 {sheet.height} 可被 {grid.rows} 行整除，底部余数 0 像素")
    else:
        hints.append(
            f"高度 {sheet.height} 除以 {grid.rows} 行余 {rem_h} 像素；"
            f"若要完全整除可将高度调整为 {sheet.height - rem_h} 或 {sheet.height + grid.rows - rem_h}"
        )
    return "；".join(hints)
