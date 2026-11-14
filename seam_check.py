#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""UV 接缝亮度核对工具

读取烘焙光照贴图 (PNG) 与 UV 边数据 (JSON)，沿每条 UV 边两侧采样，
按 RGB 加权亮度 (Rec.601) 计算两侧平均亮度差，超过容差判为超差。

产物（输出到 --out 目录）：
  preview.png    标注预览图（超差=红 / 合格=绿 / 无法判定=黄）
  checklist.csv  逐接缝核对列表（面片编号升序，同面片按输入顺序）
  summary.txt    汇总（含全部判定面片数、含无法判定接缝的面片数）

用法：
  python seam_check.py --image lightmap.png --edges edges.json --out out
"""
import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

# Rec.601 加权亮度系数
LUMA_W = (0.299, 0.587, 0.114)
SAMPLES_PER_EDGE = 8      # 每条边每侧采样点数
OFFSET_PX = 2.0           # 距边的垂直偏移（像素）

STATUS_PASS = "合格"
STATUS_FAIL = "超差"
STATUS_UNDETERMINED = "无法判定"
STATUS_SKIPPED = "跳过"

MARK_COLORS = {
    STATUS_FAIL: (255, 40, 40, 255),      # 红
    STATUS_PASS: (30, 200, 60, 255),      # 绿
    STATUS_UNDETERMINED: (255, 210, 0, 255),  # 黄
}


def fail_exit(msg):
    print("错误：" + msg, file=sys.stderr)
    sys.exit(2)


def load_edges(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        fail_exit(f"找不到 UV 边数据文件：{path}")
    except json.JSONDecodeError as e:
        fail_exit(f"UV 边数据 JSON 解析失败：{e}")

    # 容差校验：缺失或为负值时给出含“容差”字样的提示，整次运行不产出
    if "tolerance" not in cfg:
        fail_exit("配置缺少容差字段 tolerance，无法判定接缝是否超差。")
    tol = cfg["tolerance"]
    if not isinstance(tol, (int, float)) or isinstance(tol, bool):
        fail_exit(f"容差字段 tolerance 类型非法（{tol!r}），应为非负数值。")
    if tol < 0:
        fail_exit(f"容差字段 tolerance 为负值（{tol}），应为非负数值。")

    canvas = cfg.get("canvas") or {}
    cw, ch = canvas.get("width"), canvas.get("height")
    if not isinstance(cw, int) or not isinstance(ch, int):
        fail_exit("配置缺少画布尺寸 canvas.width / canvas.height。")

    edges = cfg.get("edges")
    if not isinstance(edges, list):
        fail_exit("配置缺少 UV 边列表 edges。")
    return cfg, float(tol), (cw, ch), edges


def sample_edge(arr, p1, p2):
    """沿边两侧采样，返回 (side_a_lumas, side_b_lumas, reason_or_None)。

    arr: H×W×4 uint8 (RGBA)。采样点越界 -> 无法判定；
    透明像素 (alpha==0) 不参与亮度计算；某侧全部透明 -> 无法判定。
    """
    h, w = arr.shape[:2]
    p1 = np.asarray(p1, dtype=float)
    p2 = np.asarray(p2, dtype=float)
    d = p2 - p1
    length = float(np.hypot(d[0], d[1]))
    if length < 1e-9:
        return None, None, "边长为零，无法确定采样方向"

    n = np.array([-d[1], d[0]]) / length  # 单位法线
    ts = np.linspace(0.0, 1.0, SAMPLES_PER_EDGE + 2)[1:-1]
    base = p1[None, :] + ts[:, None] * d[None, :]

    sides = []
    for sign in (+1.0, -1.0):
        pts = base + sign * OFFSET_PX * n[None, :]
        xy = np.rint(pts).astype(int)
        # 越界检查：任一采样点落到贴图边界外 -> 该接缝无法判定
        oob = (xy[:, 0] < 0) | (xy[:, 0] >= w) | (xy[:, 1] < 0) | (xy[:, 1] >= h)
        if oob.any():
            x, y = xy[np.argmax(oob)]
            return None, None, f"采样点越界 ({x},{y})，贴图尺寸 {w}x{h}"
        px = arr[xy[:, 1], xy[:, 0]].astype(float)
        lumas = px[:, 0] * LUMA_W[0] + px[:, 1] * LUMA_W[1] + px[:, 2] * LUMA_W[2]
        valid = px[:, 3] > 0  # 透明像素不参与亮度计算
        sides.append(lumas[valid])

    for sign, s in zip(("+", "-"), sides):
        if s.size == 0:
            return None, None, f"法线{sign}侧采样点全部透明"
    return sides[0], sides[1], None


def judge_edges(arr, edges, tol):
    """逐条判定，返回结果列表（保持输入顺序，附带 input_index）。"""
    results = []
    for idx, e in enumerate(edges):
        rec = {
            "input_index": idx,
            "face_id": e.get("face_id"),
            "material": e.get("material", ""),
            "p1": e.get("p1"),
            "p2": e.get("p2"),
            "status": None,
            "diff": None,
            "note": "",
        }
        fid = e.get("face_id")
        if fid is None:
            rec["status"] = STATUS_SKIPPED
            rec["note"] = "缺少面片编号 face_id，跳过"
            results.append(rec)
            continue
        p1, p2 = e.get("p1"), e.get("p2")
        if not (isinstance(p1, (list, tuple)) and len(p1) == 2
                and isinstance(p2, (list, tuple)) and len(p2) == 2):
            rec["status"] = STATUS_UNDETERMINED
            rec["note"] = "端点坐标缺失或格式非法"
            results.append(rec)
            continue
        sa, sb, reason = sample_edge(arr, p1, p2)
        if reason is not None:
            rec["status"] = STATUS_UNDETERMINED
            rec["note"] = reason
            results.append(rec)
            continue
        diff = abs(float(sa.mean()) - float(sb.mean()))
        rec["diff"] = round(diff, 4)
        rec["status"] = STATUS_FAIL if diff > tol else STATUS_PASS
        rec["note"] = f"两侧平均亮度差 {rec['diff']:.4f}，容差 {tol}"
        results.append(rec)
    return results


def sort_results(results):
    """判定类按面片编号升序、同面片按输入首次出现顺序；跳过类排末尾。"""
    judged = [r for r in results if r["status"] != STATUS_SKIPPED]
    skipped = [r for r in results if r["status"] == STATUS_SKIPPED]
    judged.sort(key=lambda r: (r["face_id"], r["input_index"]))
    skipped.sort(key=lambda r: r["input_index"])
    return judged + skipped


def draw_preview(img, results, out_path):
    canvas = img.convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    for r in results:
        color = MARK_COLORS.get(r["status"])
        if color is None or not r["p1"] or not r["p2"]:
            continue
        p1 = (float(r["p1"][0]), float(r["p1"][1]))
        p2 = (float(r["p2"][0]), float(r["p2"][1]))
        draw.line([p1, p2], fill=color, width=3)
        for p in (p1, p2):
            draw.ellipse([p[0] - 4, p[1] - 4, p[0] + 4, p[1] + 4],
                         outline=color, width=2)
    canvas.save(out_path)


def write_checklist(results, out_path):
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["序号", "面片编号", "材质", "端点1", "端点2",
                    "判定结果", "亮度差值", "说明"])
        for i, r in enumerate(results, 1):
            w.writerow([
                i,
                r["face_id"] if r["face_id"] is not None else "(缺失)",
                r["material"],
                f"({r['p1'][0]},{r['p1'][1]})" if r["p1"] else "",
                f"({r['p2'][0]},{r['p2'][1]})" if r["p2"] else "",
                r["status"],
                "" if r["diff"] is None else f"{r['diff']:.4f}",
                r["note"],
            ])


def build_summary(results, tol):
    counts = {s: 0 for s in (STATUS_PASS, STATUS_FAIL, STATUS_UNDETERMINED, STATUS_SKIPPED)}
    for r in results:
        counts[r["status"]] += 1
    judged = [r for r in results if r["status"] in (STATUS_PASS, STATUS_FAIL, STATUS_UNDETERMINED)]
    judged_faces = {r["face_id"] for r in judged}
    undet_faces = {r["face_id"] for r in judged if r["status"] == STATUS_UNDETERMINED}
    lines = [
        "===== UV 接缝亮度核对汇总 =====",
        f"容差: {tol}",
        f"接缝总数: {len(results)}",
        f"  合格: {counts[STATUS_PASS]}",
        f"  超差: {counts[STATUS_FAIL]}",
        f"  无法判定: {counts[STATUS_UNDETERMINED]}",
        f"  跳过: {counts[STATUS_SKIPPED]}",
        f"全部判定面片数: {len(judged_faces)}",
        f"含无法判定接缝的面片数: {len(undet_faces)}",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="UV 接缝亮度核对工具")
    ap.add_argument("--image", required=True, help="光照贴图 PNG 路径")
    ap.add_argument("--edges", required=True, help="UV 边数据 JSON 路径")
    ap.add_argument("--out", required=True, help="产物输出目录")
    args = ap.parse_args()

    cfg, tol, (cw, ch), edges = load_edges(args.edges)

    try:
        img = Image.open(args.image).convert("RGBA")
    except FileNotFoundError:
        fail_exit(f"找不到贴图文件：{args.image}")
    iw, ih = img.size
    if (iw, ih) != (cw, ch):
        fail_exit(
            f"贴图像素尺寸与配置画布尺寸不一致："
            f"贴图实际 {iw}x{ih}，配置画布 {cw}x{ch}。请核对后重试。"
        )

    arr = np.asarray(img)
    results = judge_edges(arr, edges, tol)
    ordered = sort_results(results)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    draw_preview(img, results, out_dir / "preview.png")
    write_checklist(ordered, out_dir / "checklist.csv")
    summary = build_summary(results, tol)
    (out_dir / "summary.txt").write_text(summary + "\n", encoding="utf-8")

    print(summary)
    print(f"\n产物已输出到 {out_dir.resolve()}")
    print("  preview.png    标注预览图（红=超差 绿=合格 黄=无法判定）")
    print("  checklist.csv  逐接缝核对列表")
    print("  summary.txt    汇总")


if __name__ == "__main__":
    main()
