#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify.py — 对 8 项验收标准逐条运行并程序化检查, 输出 PASS/FAIL。"""
import json
import os
import subprocess
import sys

import numpy as np
from PIL import Image

ROOT = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(ROOT, "samples")
OUT = os.path.join(ROOT, "out")
TOOL = os.path.join(ROOT, "mesh_preview.py")

results = []


def run_tool(case, mesh, texture, views):
    outdir = os.path.join(OUT, case)
    os.makedirs(outdir, exist_ok=True)
    # 清理上次运行的输出, 保证"不生成 preview.png"类断言有效
    for f in ("preview.png", "report.json"):
        p = os.path.join(outdir, f)
        if os.path.exists(p):
            os.remove(p)
    cmd = [sys.executable, TOOL, "--mesh", mesh, "--views", views, "--outdir", outdir]
    if texture:
        cmd += ["--texture", texture]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    report = None
    rp = os.path.join(outdir, "report.json")
    if os.path.exists(rp):
        with open(rp, encoding="utf-8") as fh:
            report = json.load(fh)
    return outdir, proc.returncode, report


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def s(*parts):
    return os.path.join(SAMPLES, *parts)


# ------------------------------------------------ 1. 默认立方体: 宽800, views长度4
outdir, rc, rep = run_tool("cube", s("cube.obj"), s("texture.png"), s("views.json"))
img = Image.open(os.path.join(outdir, "preview.png"))
check("1. 默认立方体 preview.png 宽为 800", img.width == 800, f"实际宽 {img.width}")
check("1. report views 长度为 4", rep is not None and len(rep["views"]) == 4,
      f"实际 {len(rep['views']) if rep else '无报告'}")
check("1. 四视角均 rendered",
      rep is not None and all(v["status"] == "rendered" for v in rep["views"]))

# ------------------------------------------------ 2. 无法线球体: 亮度连续无硬边
outdir, rc, rep = run_tool("sphere", s("sphere.obj"), s("texture.png"), s("views.json"))
img = np.asarray(Image.open(os.path.join(outdir, "preview.png"))).astype(np.float64)
cell = img[400:800, 400:800]                       # iso 视角单元格
bright = cell.sum(axis=2)
obj_mask = bright > (36 + 36 + 40 + 30)            # 球体像素(高于背景)
obj_mask[:60, :] = False                           # 排除左上角名称标签条带
# 腐蚀一圈, 排除轮廓边缘(轮廓处与背景相邻, 差值天然大)
er = obj_mask[1:-1, 1:-1] & obj_mask[:-2, 1:-1] & obj_mask[2:, 1:-1] \
    & obj_mask[1:-1, :-2] & obj_mask[1:-1, 2:]
lum = cell.sum(axis=2)
dx = np.abs(lum[1:-1, 1:-1] - lum[1:-1, 2:])[er]
dy = np.abs(lum[1:-1, 1:-1] - lum[2:, 1:-1])[er]
max_diff = max(dx.max() if dx.size else 0, dy.max() if dy.size else 0)
p999 = np.percentile(np.concatenate([dx, dy]), 99.9) if dx.size else 0
check("2. 无法线球体相邻像素亮度无突变(硬边)",
      max_diff < 60 and p999 < 30,
      f"球面内部相邻像素最大差 {max_diff:.1f}, 99.9%分位 {p999:.1f} (0-765 亮度轴)")

# ------------------------------------------------ 3. UV 越界: 棋盘格 + 计数 + 邻近不变
outdir, rc, rep = run_tool("plane", s("plane_uv15.obj"), s("texture.png"), s("views.json"))
img = np.asarray(Image.open(os.path.join(outdir, "preview.png"))).astype(np.float64) / 255.0
cell = img[0:400, 0:400]                           # front 视角
r, g, b = cell[..., 0], cell[..., 1], cell[..., 2]
blue_checker = (b > r + 0.15) & (b > g + 0.15)     # 棋盘格蓝色块
obj = cell.sum(axis=2) > (36 + 36 + 40 + 30) / 255.0
ys, xs = np.nonzero(obj)
mid_x = (xs.min() + xs.max()) // 2
left_obj = obj[:, :mid_x]
right_obj = obj[:, mid_x:]
left_blue = (blue_checker[:, :mid_x] & left_obj).sum()
right_blue = (blue_checker[:, mid_x:] & right_obj).sum()
check("3. 越界面片计数 > 0",
      rep["outOfRangeUvFaceCount"] > 0,
      f"outOfRangeUvFaceCount={rep['outOfRangeUvFaceCount']}, 索引={rep['outOfRangeUvFaces']}")
check("3. 越界区呈棋盘格(右半出现棋盘蓝块)", right_blue > 200,
      f"右半棋盘蓝像素 {right_blue}")
check("3. 邻近面片保持原纹理(左半无棋盘蓝块)", left_blue < 20,
      f"左半棋盘蓝像素 {left_blue}")

# ------------------------------------------------ 4. 退化面片: 跳过并记录索引
outdir, rc, rep = run_tool("degenerate", s("cube_degenerate.obj"),
                           s("texture.png"), s("views.json"))
check("4. 报告列出退化面片索引 12", rep["degenerateFaces"] == [12],
      f"degenerateFaces={rep['degenerateFaces']}")
check("4. 其余面片正常渲染", rep["renderedFaceCount"] == 12
      and all(v["status"] == "rendered" for v in rep["views"]),
      f"renderedFaceCount={rep['renderedFaceCount']}")

# ------------------------------------------------ 5. 纹理缺失: 全灰 + textureStatus
outdir, rc, rep = run_tool("no_texture", s("cube.obj"),
                           s("no_such_texture.png"), s("views.json"))
img = np.asarray(Image.open(os.path.join(outdir, "preview.png"))).astype(np.int64)
cell = img[0:400, 0:400]
obj = cell.sum(axis=2) > (36 + 36 + 40 + 30)
obj[:60, :] = False                                # 排除左上角名称标签条带
px = cell[obj]
gray = (np.abs(px[:, 0] - px[:, 1]).max() < 8) and (np.abs(px[:, 1] - px[:, 2]).max() < 8)
check("5. textureStatus 为 missing_or_invalid",
      rep["textureStatus"] == "missing_or_invalid", rep["textureStatus"])
check("5. 模型呈纯灰且输出完整", gray and os.path.exists(os.path.join(outdir, "preview.png")),
      f"物体像素 RGB 通道最大差 {np.abs(px[:,0]-px[:,1]).max()}/{np.abs(px[:,1]-px[:,2]).max()}")

# ------------------------------------------------ 6. 损坏网格: 无预览图 + meshStatus=error
outdir, rc, rep = run_tool("broken", s("broken.obj"), s("texture.png"), s("views.json"))
check("6. 不生成 preview.png", not os.path.exists(os.path.join(outdir, "preview.png")))
check("6. meshStatus 为 error 且附可读错误信息",
      rep["meshStatus"] == "error" and bool(rep.get("error")),
      (rep.get("error") or "")[:100])

# ------------------------------------------------ 7. 单视角无效: 其余仍渲染
outdir, rc, rep = run_tool("invalid_side", s("cube.obj"), s("texture.png"),
                           s("views_invalid_side.json"))
statuses = {v["name"]: v["status"] for v in rep["views"]}
side_reason = next((v.get("reason", "") for v in rep["views"] if v["name"] == "side"), "")
check("7. side 视角为 error 且注明原因",
      statuses.get("side") == "error" and bool(side_reason), side_reason[:80])
check("7. 其余三视角 rendered",
      all(statuses.get(n) == "rendered" for n in ("front", "top", "iso")),
      str(statuses))
img = np.asarray(Image.open(os.path.join(outdir, "preview.png"))).astype(np.int64)
ok_cells = True
for idx, (cy, cx) in enumerate([(0, 0), (400, 0), (400, 400)]):  # front/top/iso 区域
    cell = img[cy:cy + 400, cx:cx + 400]
    if (cell.sum(axis=2) > (36 + 36 + 40 + 30)).sum() < 5000:    # 区域内有模型像素
        ok_cells = False
check("7. 三个成功视角在预览图中可见", ok_cells)

# ------------------------------------------------ 8. 标签: 四区域名称 + 失败原因不越界
img = np.asarray(Image.open(os.path.join(outdir, "preview.png"))).astype(np.int64)
name_hits, reason_ok = [], True
for idx, (cy, cx) in enumerate([(0, 0), (0, 400), (400, 0), (400, 400)]):
    cell = img[cy:cy + 400, cx:cx + 400]
    yellow = ((np.abs(cell[..., 0] - 255) < 40) & (np.abs(cell[..., 1] - 210) < 40)
              & (cell[..., 2] < 60))
    name_hits.append(yellow[:60, :].sum() > 10)   # 左上角名称标签(黄字)
red = ((img[..., 0] > 200) & (np.abs(img[..., 1] - 100) < 40)
       & (np.abs(img[..., 2] - 100) < 40))         # 原因文本红(255,100,100), 纹理蓝分量仅38
red_in_side = red[0:400, 400:800].sum()
# 原因文本只绘制在本单元格图像上(构造上不会越界); 检查文本未触及单元格边缘(未被截断)
ys, xs = np.nonzero(red[0:400, 400:800])
not_clipped = len(xs) > 0 and xs.max() < 390 and ys.max() < 390
check("8. 四个视角区域均有名称标签", all(name_hits), str(name_hits))
check("8. side 区域原因文本可见且未越出本区域",
      red_in_side > 100 and not_clipped,
      f"side 区内红色像素 {red_in_side}, 文本最右列 {xs.max() if len(xs) else '-'}"
      f"(格宽400), 最下行 {ys.max() if len(ys) else '-'}")

# ------------------------------------------------ 汇总
failed = [n for n, ok, _ in results if not ok]
print(f"\n共 {len(results)} 项检查, 通过 {len(results) - len(failed)}, 失败 {len(failed)}")
if failed:
    print("失败项:")
    for n in failed:
        print("  -", n)
sys.exit(1 if failed else 0)
