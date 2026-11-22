"""块网格规划: 切块、中心优先顺序、边界耦合态与首块耗时估算."""

from __future__ import annotations

from typing import List, Tuple

from .config import GenerationConfig
from .pipeline import Region, cone_region


def parse_grid(grid: str) -> Tuple[int, int]:
    """解析 "RxC" 网格声明为 (rows, cols)."""
    parts = grid.lower().split("x")
    if len(parts) != 2:
        raise ValueError(f"invalid grid spec: {grid}")
    rows, cols = int(parts[0]), int(parts[1])
    if rows < 1 or cols < 1:
        raise ValueError(f"invalid grid dims: {grid}")
    return rows, cols


def block_ranges(cfg: GenerationConfig, rows: int, cols: int) -> List[Region]:
    """行优先编号的块像素区间 (半开区间), 覆盖整幅画布."""
    ranges: List[Region] = []
    for r in range(rows):
        y0 = cfg.height * r // rows
        y1 = cfg.height * (r + 1) // rows
        for c in range(cols):
            x0 = cfg.width * c // cols
            x1 = cfg.width * (c + 1) // cols
            ranges.append((y0, y1, x0, x1))
    return ranges


def center_first_order(rows: int, cols: int) -> List[int]:
    """生成顺序: 中心区域块优先, 其余按到中心距离与编号排序."""
    ids = list(range(rows * cols))
    center_r = (rows - 1) / 2.0
    center_c = (cols - 1) / 2.0

    def key(bid: int) -> Tuple[float, int]:
        r, c = divmod(bid, cols)
        return ((r - center_r) ** 2 + (c - center_c) ** 2, bid)

    return sorted(ids, key=key)


def boundary_coupling_states(cfg: GenerationConfig, rows: int, cols: int) -> int:
    """边界耦合态规模: 内部边界像素数 x 迭代步数.

    块越小内部边界越长, 耦合态越多, 一致性风险越大.
    """
    interior = 0
    if rows > 1:
        interior += (rows - 1) * cfg.width
    if cols > 1:
        interior += (cols - 1) * cfg.height
    return interior * cfg.steps


def cone_pixels(cfg: GenerationConfig, block: Region) -> int:
    """首块依赖锥在第 0 步的像素规模."""
    ry0, ry1, rx0, rx1 = cone_region(cfg, block, cfg.steps)
    return (ry1 - ry0) * (rx1 - rx0)


def block_resident_bytes(cfg: GenerationConfig, block: Region) -> int:
    """单块驻留内存估算: 依赖锥场 + 当前层场."""
    cone = cone_pixels(cfg, block)
    y0, y1, x0, x1 = block
    own = (y1 - y0) * (x1 - x0)
    return (cone + own) * cfg.channels * cfg.dtype.itemsize


def estimate_first_block_ms(cfg: GenerationConfig, block: Region) -> float:
    """首块耗时估算 (毫秒).

    估算口径: 依赖锥像素数 x 迭代步数 x 单像素单步校准成本,
    邻域耦合规模已包含在依赖锥像素数内.
    """
    work = cone_pixels(cfg, block) * cfg.steps
    return work * cfg.cost_per_pixel_step_us / 1000.0
