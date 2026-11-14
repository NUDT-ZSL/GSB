#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""确定性生成验收用样例：光照贴图 PNG 与 UV 边 JSON。

生成内容（无需外部素材，重复运行结果一致）：
  samples/lightmap.png            512x512 基础样例贴图（含透明区域）
  samples/edges.json              基础 UV 边（乱序、含重复编号/缺编号/越界/透明样例）
  samples/edges_bad_canvas.json   画布宽 +10 的尺寸不一致样例
  samples/edges_no_tolerance.json 缺少容差字段样例
  samples/edges_neg_tolerance.json 容差为负值样例
  samples/full/lightmap_full.png  2048x2048 满规模贴图
  samples/full/edges_full.json    500 条接缝满规模样例
"""
import json
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).parent
SAMPLES = ROOT / "samples"


def build_base_lightmap():
    """512x512 RGBA：左半亮度100，右半亮度160；内部小台阶105；一块透明区。"""
    h = w = 512
    img = np.zeros((h, w, 4), dtype=np.uint8)
    img[..., 3] = 255
    img[:, :256, :3] = 100          # 左半
    img[:, 256:, :3] = 160          # 右半（与左半差 60，明显超容差）
    img[300:400, 100:200, :3] = 105  # 小台阶（与周围差 5，容差内）
    img[380:460, 400:470, 3] = 0     # 透明区域
    return img


def base_edges():
    """乱序输入，覆盖：合格/超差/越界/全透明/部分透明/重复编号/缺编号。"""
    return [
        # face 5：整条边落在透明区内 -> 无法判定（全部透明）
        {"face_id": 5, "material": "glass", "p1": [410, 400], "p2": [450, 430]},
        # face 2：跨过 x=256 亮度台阶（100 vs 160，差60）-> 超差
        {"face_id": 2, "material": "wall", "p1": [256, 100], "p2": [256, 200]},
        # face 7（第一次出现）：均匀区域 -> 合格
        {"face_id": 7, "material": "floor", "p1": [50, 50], "p2": [150, 50]},
        # face 1：右半均匀区域 -> 合格
        {"face_id": 1, "material": "wall", "p1": [300, 100], "p2": [350, 150]},
        # 缺面片编号 -> 跳过
        {"material": "trim", "p1": [100, 450], "p2": [150, 450]},
        # face 4：端点越出画布 -> 无法判定（越界）
        {"face_id": 4, "material": "rock", "p1": [500, 500], "p2": [560, 540]},
        # face 7（第二次出现，端点不同，独立判定）-> 合格
        {"face_id": 7, "material": "floor", "p1": [60, 60], "p2": [60, 160]},
        # face 3：跨小台阶（100 vs 105，差5）-> 合格
        {"face_id": 3, "material": "wall", "p1": [100, 320], "p2": [100, 380]},
        # face 6：部分采样点落入透明区（透明点被剔除）-> 正常判定合格
        {"face_id": 6, "material": "glass", "p1": [380, 420], "p2": [420, 420]},
    ]


def write_json(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def build_full_scale():
    """2048x2048 贴图 + 500 条接缝，确定性随机。"""
    rng = np.random.default_rng(98)
    n = 2048
    grid = rng.integers(60, 200, size=(32, 32), endpoint=False).astype(np.uint8)
    img = np.repeat(np.repeat(grid, 64, axis=0), 64, axis=1)
    noise = rng.integers(-3, 4, size=(n, n)).astype(np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)
    rgba = np.stack([img, img, img, np.full_like(img, 255)], axis=-1)

    edges = []
    mats = ["wall", "floor", "rock", "trim", "glass"]
    for i in range(500):
        x1, y1 = rng.integers(4, n - 4, size=2)
        ang = rng.uniform(0, np.pi)
        ln = rng.uniform(30, 200)
        x2 = int(np.clip(x1 + ln * np.cos(ang), 4, n - 5))
        y2 = int(np.clip(y1 + ln * np.sin(ang), 4, n - 5))
        edges.append({
            "face_id": i + 1,
            "material": mats[i % len(mats)],
            "p1": [int(x1), int(y1)],
            "p2": [x2, y2],
        })
    rng.shuffle(edges)  # 乱序输入
    return rgba, {"canvas": {"width": n, "height": n}, "tolerance": 15.0, "edges": edges}


def main():
    SAMPLES.mkdir(exist_ok=True)
    (SAMPLES / "full").mkdir(exist_ok=True)

    Image.fromarray(build_base_lightmap(), "RGBA").save(SAMPLES / "lightmap.png")
    cfg = {"canvas": {"width": 512, "height": 512},
           "tolerance": 10.0, "edges": base_edges()}
    write_json(SAMPLES / "edges.json", cfg)

    bad = json.loads(json.dumps(cfg))
    bad["canvas"]["width"] += 10
    write_json(SAMPLES / "edges_bad_canvas.json", bad)

    no_tol = json.loads(json.dumps(cfg))
    del no_tol["tolerance"]
    write_json(SAMPLES / "edges_no_tolerance.json", no_tol)

    neg_tol = json.loads(json.dumps(cfg))
    neg_tol["tolerance"] = -1.0
    write_json(SAMPLES / "edges_neg_tolerance.json", neg_tol)

    rgba, full_cfg = build_full_scale()
    Image.fromarray(rgba, "RGBA").save(SAMPLES / "full" / "lightmap_full.png")
    write_json(SAMPLES / "full" / "edges_full.json", full_cfg)

    print("样例已生成：")
    for p in sorted(SAMPLES.rglob("*")):
        if p.is_file():
            print(" ", p.relative_to(ROOT))


if __name__ == "__main__":
    main()
