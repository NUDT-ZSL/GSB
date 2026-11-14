"""确定性生成轨道与遮挡表样本（无随机源，重复运行逐字节相同）。

生成内容:
- sample_track.json / sample_occlusion.json:
  12 x 10 = 120 块瓦片的等距地图，3 个相机关键帧；
  遮挡表含 1 个故意设反的对、1 个正确对、1 个越界引用、1 个重复编号。
- bad_missing_field_*.json: 地图尺寸字段缺失。
- bad_negative_size_*.json: 瓦片宽度为负数。
"""

from __future__ import annotations

import json
import os
from typing import Tuple

# ---- 固定样本参数（全部为整数字面量，保证确定性） ----
COLS = 12
ROWS = 10
TILE_W = 64
TILE_H = 32
VIEWPORT_W = 480
VIEWPORT_H = 320
STEPS_PER_GAP = 2

# 相机坐标取 3 的倍数，使 t=1/3、2/3 的插值结果仍为整数，
# 视口边界判定不产生浮点歧义。
KEYFRAMES = [
    {"id": 0, "x": -160, "y": 0},
    {"id": 1, "x": 40, "y": 120},
    {"id": 2, "x": 240, "y": 240},
]


def _track_object() -> dict:
    return {
        "map": {"cols": COLS, "rows": ROWS},
        "tile_size": {"w": TILE_W, "h": TILE_H},
        "viewport": {"w": VIEWPORT_W, "h": VIEWPORT_H},
        "steps_per_gap": STEPS_PER_GAP,
        "keyframes": [dict(kf) for kf in KEYFRAMES],
    }


def _occlusion_object() -> Tuple[dict, dict]:
    """返回 (正常遮挡表, 各条目意图说明)。"""
    # 几何上 tile 0 深度键 (0,0) 小于 tile 1 深度键 (1,1)，
    # 即 tile 0 必须先绘制；此处故意写反，构成失效遮挡对。
    reversed_pair = {"id": "occ_rev", "first": 1, "second": 0}
    # 正确对：tile 0 先于 tile 2（深度键 (0,0) < (2,2)）。
    correct_pair = {"id": "occ_ok", "first": 0, "second": 2}
    # 越界引用：tile 999 超出 [0, 119] 范围。
    out_of_range = {"id": "occ_bad_ref", "first": 0, "second": 999}
    # 重复编号：与正确对同 id，应被忽略并在报告注明。
    duplicate = {"id": "occ_ok", "first": 2, "second": 4}
    extra = {"id": "occ_extra", "first": 4, "second": 6}
    occlusion = {
        "relations": [reversed_pair, correct_pair, out_of_range, duplicate, extra]
    }
    notes = {
        "occ_rev": "故意设反：几何规则要求 tile 0 先于 tile 1，表中写反",
        "occ_ok": "正确对：tile 0 先于 tile 2",
        "occ_bad_ref": "越界：tile 999 超出地图编号范围 [0, 119]",
        "occ_extra": "正确对：tile 4 先于 tile 6",
    }
    return occlusion, notes


def generate(out_dir: str) -> dict:
    """把全部样本写入 ``out_dir``，返回路径清单（确定顺序）。"""
    os.makedirs(out_dir, exist_ok=True)
    paths = {}

    track = _track_object()
    occlusion, notes = _occlusion_object()

    paths["track"] = os.path.join(out_dir, "sample_track.json")
    paths["occlusion"] = os.path.join(out_dir, "sample_occlusion.json")
    with open(paths["track"], "w", encoding="utf-8", newline="\n") as f:
        json.dump(track, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    with open(paths["occlusion"], "w", encoding="utf-8", newline="\n") as f:
        json.dump(occlusion, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    # 坏样本 1：缺少 map.rows 字段。
    bad_missing = _track_object()
    del bad_missing["map"]["rows"]
    paths["bad_missing_track"] = os.path.join(out_dir, "bad_missing_field_track.json")
    with open(paths["bad_missing_track"], "w", encoding="utf-8", newline="\n") as f:
        json.dump(bad_missing, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    # 坏样本 2：瓦片宽度为负数。
    bad_negative = _track_object()
    bad_negative["tile_size"]["w"] = -TILE_W
    paths["bad_negative_track"] = os.path.join(out_dir, "bad_negative_size_track.json")
    with open(paths["bad_negative_track"], "w", encoding="utf-8", newline="\n") as f:
        json.dump(bad_negative, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    # 坏样本共用的空遮挡表。
    paths["empty_occlusion"] = os.path.join(out_dir, "empty_occlusion.json")
    with open(paths["empty_occlusion"], "w", encoding="utf-8", newline="\n") as f:
        json.dump({"relations": []}, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")

    paths["notes"] = notes
    return paths
