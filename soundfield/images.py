"""六面体房间的镜像声源枚举（确定性阶序调度）。

枚举规则：

* 对每个声源，按反射次数 0..max_reflections 展开所有六面墙序列（笛卡尔积），
  只保留重建阶数 ``reconstruction_order`` 以内的结果；
* 同一展开格点（镜像声源位置）可能由多条墙序列产生，依据
  ``(阶数, 反射面序列, 声源标识)`` 字典序保留第一条，保证同一反射路径只枚举一次；
* 最终全局次序仍以 ``(阶数, 反射面序列, 声源标识)`` 字典序为最终决胜，
  同一场景重复枚举得到逐镜像一致的位置、阶数与反射历史。
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product

import numpy as np

from .config import WALL_IDS
from .fixedpoint import chain_mul_q, quantize
from .scene import Scene


def wall_axis(wall: str) -> int:
    return WALL_IDS.index(wall) // 2


def reflect_position(
    position: np.ndarray,
    wall: str,
    room_size: tuple[float, float, float],
) -> np.ndarray:
    """对单个墙面做镜像反射。"""

    idx = WALL_IDS.index(wall)
    axis = idx // 2
    high = idx % 2
    boundary = room_size[axis] if high else 0.0
    out = np.array(position, dtype=np.float64, copy=True)
    out[axis] = 2.0 * boundary - out[axis]
    return out


def cell_key(image_pos: np.ndarray, room_size: tuple[float, float, float]) -> tuple[int, int, int]:
    """镜像点在展开空间中的整数格点键，用于路径去重。

    镜像点坐标恒为 ``(2k + s) * L`` 形态（s 为 0 或 ±1），对浮点结果做带
    epsilon 的就近取整即可稳定恢复整数格点。
    """

    key: list[int] = []
    for axis in range(3):
        ratio = float(image_pos[axis]) / room_size[axis]
        key.append(int(np.floor(ratio + np.sign(ratio) * 1e-12 + 0.5)))
    return tuple(key)  # type: ignore[return-value]


@dataclass(frozen=True)
class ImageSource:
    """一个去重后的镜像声源。"""

    image_index: int                  # 全局枚举序号（字典序决胜后的最终次序）
    source_index: int
    source_id: str
    order: int                        # 反射阶数（0 = 直达声）
    walls: tuple[str, ...]            # 反射面序列（字典序最小者）
    cell: tuple[int, int, int]
    position: tuple[float, float, float]
    absorption_q: tuple[tuple[int, ...], ...]   # 每频段：反射链 Q24 累乘
    absorption_f: tuple[tuple[float, ...], ...]  # 每频段：float64 累乘


def enumerate_images(scene: Scene) -> list[ImageSource]:
    per_source: list[list[ImageSource]] = []
    for source_index, source in enumerate(scene.sources):
        seen: dict[tuple[int, int, int], ImageSource] = {}
        origin = np.asarray(source.position, dtype=np.float64)
        for order in range(scene.max_reflections + 1):
            if order == 0:
                wall_sequences: list[tuple[str, ...]] = [()]
            else:
                wall_sequences = list(product(WALL_IDS, repeat=order))
            for walls in wall_sequences:
                position = origin.copy()
                for wall in walls:
                    position = reflect_position(position, wall, scene.room_size)
                key = cell_key(position, scene.room_size)
                if key in seen:
                    continue
                abs_q: list[tuple[int, ...]] = []
                abs_f: list[tuple[float, ...]] = []
                for band in range(scene.band_count):
                    chain_q = [scene.absorption_q[wall][band] for wall in walls]
                    chain_f = [scene.absorption_f[wall][band] for wall in walls]
                    abs_q.append((quantize(1.0),) if not chain_q else (chain_mul_q(chain_q),))
                    abs_f.append((1.0,) if not chain_f else (float(np.prod(chain_f)),))
                image = ImageSource(
                    image_index=-1,
                    source_index=source_index,
                    source_id=source.id,
                    order=order,
                    walls=walls,
                    cell=key,
                    position=tuple(float(v) for v in position),
            absorption_q=tuple(seq[0] for seq in abs_q),
            absorption_f=tuple(seq[0] for seq in abs_f),
                )
                seen[key] = image
        per_source.append(list(seen.values()))

    merged = [image for images in per_source for image in images]
    merged = [image for image in merged if image.order <= scene.reconstruction_order]
    merged.sort(key=lambda img: (img.order, img.walls, img.source_id))
    return [
        ImageSource(
            image_index=index,
            source_index=img.source_index,
            source_id=img.source_id,
            order=img.order,
            walls=img.walls,
            cell=img.cell,
            position=img.position,
            absorption_q=img.absorption_q,
            absorption_f=img.absorption_f,
        )
        for index, img in enumerate(merged)
    ]
