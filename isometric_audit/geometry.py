"""等距瓦片几何、视口裁剪与遮挡深度判定。

坐标约定
--------
地图为 ``rows x cols`` 的瓦片网格，瓦片编号从 0 开始按行优先（row-major）分配：
``tile_id = row * cols + col``。

等距投影（瓦片菱形，宽 ``tile_w``、高 ``tile_h``）:
    screen_x = (col - row) * tile_w / 2
    screen_y = (col + row) * tile_h / 2

瓦片菱形的轴对齐包围盒（AABB）:
    x_min = screen_x - tile_w / 2
    y_min = screen_y - tile_h / 2
    x_max = screen_x + tile_w / 2
    y_max = screen_y + tile_h / 2

视口为屏幕坐标下的轴对齐矩形，``(camera_x, camera_y)`` 为其左上角。
瓦片与视口“完全包含或相交”即判定为可见：两 AABB 在 x、y 两个轴上
都存在非空交集（边界相接也算相交）。

遮挡规则（等距画家算法）
------------------------
深度键 ``(col + row, col)`` 小的瓦片先绘制。瓦片 A 必须先于 B 绘制
当且仅当 A 的深度键严格小于 B；两键相等时菱形不重叠，不构成先后约束。
"""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

Rect = Tuple[float, float, float, float]


def tile_id(col: int, row: int, cols: int) -> int:
    """行优先编号。"""
    return row * cols + col


def col_row(tile_id_: int, cols: int) -> Tuple[int, int]:
    """编号反解为 (列, 行)。tile_id = row * cols + col，故 divmod 得 (row, col)。"""
    row, col = divmod(tile_id_, cols)
    return col, row


def tile_bounds(col: int, row: int, tile_w: float, tile_h: float) -> Rect:
    """瓦片菱形的 AABB。"""
    center_x = (col - row) * tile_w / 2.0
    center_y = (col + row) * tile_h / 2.0
    return (
        center_x - tile_w / 2.0,
        center_y - tile_h / 2.0,
        center_x + tile_w / 2.0,
        center_y + tile_h / 2.0,
    )


def viewport_rect(cam_x: float, cam_y: float, vp_w: float, vp_h: float) -> Rect:
    """视口 AABB。"""
    return (cam_x, cam_y, cam_x + vp_w, cam_y + vp_h)


def intersects(a: Rect, b: Rect) -> bool:
    """两 AABB 是否相交或相接（完全包含也返回 True）。"""
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def visible_tiles(
    cols: int,
    rows: int,
    tile_w: float,
    tile_h: float,
    cam_x: float,
    cam_y: float,
    vp_w: float,
    vp_h: float,
) -> List[int]:
    """按输入顺序（行优先编号升序）返回视口内可见瓦片编号。

    逐块对全部瓦片做一次 AABB 相交判定。样本不超过 2000 块，
    直接枚举即可，且天然保证顺序确定、不依赖字典/集合遍历。
    """
    view = viewport_rect(cam_x, cam_y, vp_w, vp_h)
    result: List[int] = []
    for row in range(rows):
        for col in range(cols):
            if intersects(tile_bounds(col, row, tile_w, tile_h), view):
                result.append(tile_id(col, row, cols))
    return result


def depth_key(tile_id_: int, cols: int) -> Tuple[int, int]:
    """等距深度键：先比 (col+row)，再比 col。"""
    col, row = col_row(tile_id_, cols)
    return (col + row, col)


def required_draw_order(first_id: int, second_id: int, cols: int) -> int:
    """判断表中 (first, second) 要求的先后与几何规则是否一致。

    返回:
        -1: first 应先绘制，表中规则正确
         1: second 应先绘制，表中规则被违反（故意设反的对会命中这里）
         0: 两瓦片深度键相等，无遮挡先后关系，不判定违规
    """
    ka = depth_key(first_id, cols)
    kb = depth_key(second_id, cols)
    if ka < kb:
        return -1
    if kb < ka:
        return 1
    return 0


def lerp(a: float, b: float, t: float) -> float:
    """线性插值。"""
    return a + (b - a) * t


def viewport_size_at(keyframes: Sequence[dict], index: int, track: dict) -> Tuple[float, float]:
    """取某关键帧生效的视口尺寸：关键帧覆盖优先，否则用全局值。"""
    kf = keyframes[index]
    vp_w = kf.get("viewport_w", track["viewport"]["w"])
    vp_h = kf.get("viewport_h", track["viewport"]["h"])
    return float(vp_w), float(vp_h)


def expanded_frames(
    track: dict, steps_per_gap: int
) -> List[Tuple[float, float, float, float]]:
    """展开关键帧序列，返回每帧的 (cam_x, cam_y, vp_w, vp_h)。

    相邻关键帧之间按线性插值生成 ``steps_per_gap`` 个中间帧；
    视口尺寸若随帧变化同样参与插值。返回序列以第 0 个关键帧开头、
    最后一个关键帧结尾，顺序完全确定。
    """
    keyframes = track["keyframes"]
    frames: List[Tuple[float, float, float, float]] = []
    for i, kf in enumerate(keyframes):
        if i > 0:
            prev = keyframes[i - 1]
            prev_w, prev_h = viewport_size_at(keyframes, i - 1, track)
            cur_w, cur_h = viewport_size_at(keyframes, i, track)
            for s in range(1, steps_per_gap + 1):
                t = s / steps_per_gap
                frames.append(
                    (
                        lerp(float(prev["x"]), float(kf["x"]), t),
                        lerp(float(prev["y"]), float(kf["y"]), t),
                        lerp(prev_w, cur_w, t),
                        lerp(prev_h, cur_h, t),
                    )
                )
        w, h = viewport_size_at(keyframes, i, track)
        frames.append((float(kf["x"]), float(kf["y"]), w, h))
    return frames


def positions_by_id(cols: int, rows: int) -> Dict[int, Tuple[int, int]]:
    """编号 -> (列, 行) 映射，确定性构造（测试与报告复用）。"""
    return {tile_id(c, r, cols): (c, r) for r in range(rows) for c in range(cols)}
