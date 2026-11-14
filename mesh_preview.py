#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mesh_preview.py — 美术交付前三维网格表面效果本地预览工具

输入:  OBJ 网格 + 漫反射纹理 PNG + 视角配置 JSON
输出:  宽 800 的多视角预览 PNG (preview.png) 与报告 JSON (report.json)

特性:
  * 网格缺法线时自动计算平滑法线做光照(无硬边断层)
  * 纹理坐标超出 [0,1] 的面片改用棋盘格纹理, 其余面片保持原纹理
  * 共线或面积 < 1e-8 的退化面片跳过不渲染, 报告按索引列出
  * 纹理缺失/无法解码时全部面片以默认灰色渲染, textureStatus=missing_or_invalid
  * 网格无法解析时不生成预览图, meshStatus=error 并附可读错误信息
  * 各视角独立渲染, 单个视角配置无效不影响其余视角
  * 每个视角区域左上角标注视角名称, 失败视角在其区域内标注原因

用法:
  python mesh_preview.py --mesh model.obj --texture tex.png --views views.json --outdir out
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

import numpy as np

# ---------------------------------------------------------------- 常量
DEGEN_AREA_EPS = 1e-8          # 退化面片面积阈值
TOTAL_WIDTH = 800              # 预览图总宽
FOV_DEG = 45.0
BG_COLOR = np.array([36.0, 36.0, 40.0])           # 背景 (0-255)
GRAY_BASE = np.array([0.72, 0.72, 0.72])          # 默认灰色 (0-1)
CHECKER_A = np.array([0.92, 0.92, 0.92])          # 棋盘格亮色
CHECKER_B = np.array([0.25, 0.25, 0.85])          # 棋盘格暗色(蓝)
CHECKER_SCALE = 8.0
LIGHT_DIR = np.array([0.4, 0.65, 0.8])            # 相机空间光照方向
AMBIENT = 0.25
LABEL_BG = (20, 20, 24)
LABEL_FG = (255, 210, 0)       # 视角名称(黄)
ERROR_FG = (255, 100, 100)     # 失败原因(红)


# ---------------------------------------------------------------- 加载
def load_texture(path):
    """加载纹理 PNG; 缺失或无法解码时返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        from PIL import Image
        img = Image.open(path)
        img.load()
        arr = np.asarray(img.convert("RGB"), dtype=np.float64) / 255.0
        if arr.size == 0 or arr.shape[0] < 1 or arr.shape[1] < 1:
            return None
        return arr
    except Exception:
        return None


def load_mesh(path):
    """用 trimesh 解析 OBJ; 失败或不含三角网格时抛出 ValueError(可读信息)。"""
    import trimesh
    try:
        mesh = trimesh.load(path, force="mesh", process=False, maintain_order=True)
    except Exception as exc:
        raise ValueError(f"无法解析网格文件 '{path}': {exc}") from exc
    if not isinstance(mesh, trimesh.Trimesh):
        raise ValueError(f"文件 '{path}' 不包含三角网格几何体")
    if mesh.vertices.size == 0 or mesh.faces.size == 0:
        raise ValueError(f"文件 '{path}' 解析结果为空(无顶点或无面片), 可能是损坏的 OBJ")
    return mesh


def load_views_config(path):
    """读取视角配置 JSON, 返回 views 列表(保留配置顺序)。"""
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    views = cfg.get("views") if isinstance(cfg, dict) else cfg
    if not isinstance(views, list) or len(views) == 0:
        raise ValueError("视角配置 JSON 中缺少非空的 views 数组")
    return views


def validate_view(entry):
    """校验单个视角配置; 返回 (name, azimuth, elevation) 或错误信息字符串。"""
    if not isinstance(entry, dict):
        return None, "视角配置条目不是 JSON 对象"
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        return None, "缺少有效的 name 字段(需为非空字符串)"
    name = name.strip()
    for key in ("azimuth", "elevation"):
        val = entry.get(key)
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            return name, f"字段 '{key}' 缺失或不是数字(当前值: {val!r})"
        if not math.isfinite(val):
            return name, f"字段 '{key}' 不是有限数值(当前值: {val!r})"
    return (name, float(entry["azimuth"]), float(entry["elevation"])), None


# ---------------------------------------------------------------- 几何
def face_areas(vertices, faces):
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    return 0.5 * np.linalg.norm(cross, axis=1), cross


def compute_smooth_normals(vertices, faces, valid_mask, face_cross):
    """按位置焊接顶点后累加面法线, 得到平滑顶点法线(对缺法线网格)。

    按位置焊接可避免 UV 接缝处顶点被拆分造成的硬边。
    """
    key = np.round(vertices / 1e-8).astype(np.int64)
    _, inverse = np.unique(key, axis=0, return_inverse=True)
    accum = np.zeros((int(inverse.max()) + 1, 3), dtype=np.float64)
    ok_faces = faces[valid_mask]
    ok_cross = face_cross[valid_mask]
    for corner in range(3):
        np.add.at(accum, inverse[ok_faces[:, corner]], ok_cross)
    normals = accum[inverse]
    norm = np.linalg.norm(normals, axis=1, keepdims=True)
    norm[norm < 1e-20] = 1.0
    return normals / norm


def look_at_rotation(eye, center):
    """返回世界->相机旋转矩阵 R (cam = (world - eye) @ R.T), 相机看向 -Z。"""
    fwd = center - eye
    fwd /= np.linalg.norm(fwd)
    up = np.array([0.0, 1.0, 0.0])
    if abs(float(np.dot(fwd, up))) > 0.999:      # 顶/底视角, 换参考 up
        up = np.array([0.0, 0.0, -1.0])
    right = np.cross(fwd, up)
    right /= np.linalg.norm(right)
    true_up = np.cross(right, fwd)
    return np.stack([right, true_up, -fwd])


# ---------------------------------------------------------------- 光栅化
def sample_texture_bilinear(tex, u, v):
    """双线性采样, u/v 为数组, 已保证在 [0,1]。OBJ 的 v 原点在图像底部。"""
    h, w = tex.shape[0], tex.shape[1]
    x = np.clip(u, 0.0, 1.0) * (w - 1)
    y = (1.0 - np.clip(v, 0.0, 1.0)) * (h - 1)
    x0 = np.floor(x).astype(np.int64)
    y0 = np.floor(y).astype(np.int64)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (x - x0)[..., None]
    fy = (y - y0)[..., None]
    c00 = tex[y0, x0]
    c10 = tex[y0, x1]
    c01 = tex[y1, x0]
    c11 = tex[y1, x1]
    return (c00 * (1 - fx) * (1 - fy) + c10 * fx * (1 - fy)
            + c01 * (1 - fx) * fy + c11 * fx * fy)


def checker_color(u, v):
    c = (np.floor(u * CHECKER_SCALE) + np.floor(v * CHECKER_SCALE)).astype(np.int64) % 2
    c = c[..., None].astype(np.float64)
    return CHECKER_A * (1 - c) + CHECKER_B * c


def render_view(vertices, faces, valid_mask, normals, uv, face_out_of_range,
                texture, azimuth, elevation, cell_w, cell_h):
    """软光栅化渲染单个视角, 返回 HxWx3 的 uint8 图像。"""
    center = (vertices.min(axis=0) + vertices.max(axis=0)) / 2.0
    radius = float(np.linalg.norm(vertices - center, axis=1).max())
    radius = max(radius, 1e-6)
    azr = math.radians(azimuth)
    elr = math.radians(elevation)
    direction = np.array([
        math.cos(elr) * math.sin(azr),
        math.sin(elr),
        math.cos(elr) * math.cos(azr),
    ])
    dist = radius / math.tan(math.radians(FOV_DEG) / 2.0) * 1.15
    eye = center + direction * dist
    R = look_at_rotation(eye, center)

    cam = (vertices - eye) @ R.T              # 相机空间, 相机位于原点
    depth = -cam[:, 2]                        # 相机前方为正
    tan_half = math.tan(math.radians(FOV_DEG) / 2.0)

    img = np.tile(BG_COLOR, (cell_h, cell_w, 1))
    zbuf = np.full((cell_h, cell_w), np.inf)
    n_cam = normals @ R.T
    light = LIGHT_DIR / np.linalg.norm(LIGHT_DIR)

    safe_depth = np.where(np.abs(depth) < 1e-12, 1e-12, depth)
    px = (cam[:, 0] / (safe_depth * tan_half) * 0.5 + 0.5) * cell_w
    py = (0.5 - cam[:, 1] / (safe_depth * tan_half) * 0.5) * cell_h

    for fi in np.nonzero(valid_mask)[0]:
        f = faces[fi]
        d = depth[f]
        if np.any(d <= 1e-9):                 # 面片在相机背后或相机平面上
            continue
        xs = px[f]
        ys = py[f]
        minx = max(int(math.floor(xs.min())), 0)
        maxx = min(int(math.ceil(xs.max())), cell_w - 1)
        miny = max(int(math.floor(ys.min())), 0)
        maxy = min(int(math.ceil(ys.max())), cell_h - 1)
        if minx > maxx or miny > maxy:
            continue
        gx, gy = np.meshgrid(np.arange(minx, maxx + 1) + 0.5,
                             np.arange(miny, maxy + 1) + 0.5)
        det = ((ys[1] - ys[2]) * (xs[0] - xs[2])
               + (xs[2] - xs[1]) * (ys[0] - ys[2]))
        if abs(det) < 1e-12:                  # 屏幕空间退化
            continue
        l0 = ((ys[1] - ys[2]) * (gx - xs[2]) + (xs[2] - xs[1]) * (gy - ys[2])) / det
        l1 = ((ys[2] - ys[0]) * (gx - xs[2]) + (xs[0] - xs[2]) * (gy - ys[2])) / det
        l2 = 1.0 - l0 - l1
        mask = (l0 >= -1e-9) & (l1 >= -1e-9) & (l2 >= -1e-9)
        if not mask.any():
            continue

        w = 1.0 / d                            # 透视校正权重
        W = l0 * w[0] + l1 * w[1] + l2 * w[2]
        W[W == 0] = 1e-12
        z = 1.0 / W
        mask &= z > 0
        if not mask.any():
            continue

        sub = zbuf[miny:maxy + 1, minx:maxx + 1]
        mask &= z < sub
        if not mask.any():
            continue

        # 透视校正插值
        def interp(attr):
            return (l0[..., None] * w[0] * attr[f[0]]
                    + l1[..., None] * w[1] * attr[f[1]]
                    + l2[..., None] * w[2] * attr[f[2]]) / W[..., None]

        nrm = interp(n_cam)
        nlen = np.linalg.norm(nrm, axis=2, keepdims=True)
        nlen[nlen < 1e-12] = 1e-12
        nrm /= nlen
        flip = nrm[..., 2:3] < 0               # 双面光照
        nrm = np.where(flip, -nrm, nrm)
        intensity = AMBIENT + (1.0 - AMBIENT) * np.clip(nrm @ light, 0.0, 1.0)

        if texture is None or uv is None:
            base = np.tile(GRAY_BASE, (*mask.shape, 1))
        else:
            uvs = interp(uv)
            uu = uvs[..., 0]
            vv = uvs[..., 1]
            if face_out_of_range[fi]:
                base = checker_color(uu, vv)
            else:
                base = sample_texture_bilinear(texture, uu, vv)

        color = base * intensity[..., None] * 255.0
        region = img[miny:maxy + 1, minx:maxx + 1]
        region[mask] = color[mask]
        sub[mask] = z[mask]

    return np.clip(img, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- 标注
def _fonts():
    from PIL import ImageFont
    # 优先使用系统中文字体, 保证中文原因文本可读; 找不到则退回 Pillow 内置字体
    for candidate in (r"C:\Windows\Fonts\msyh.ttc",
                      r"C:\Windows\Fonts\simhei.ttf",
                      r"C:\Windows\Fonts\simsun.ttc"):
        if os.path.isfile(candidate):
            try:
                return (ImageFont.truetype(candidate, 16),
                        ImageFont.truetype(candidate, 14))
            except Exception:
                pass
    try:
        return ImageFont.load_default(size=16), ImageFont.load_default(size=14)
    except TypeError:                            # 旧版 Pillow
        f = ImageFont.load_default()
        return f, f


def _wrap_text(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        trial = (cur + " " + word).strip()
        if not cur or draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def draw_labels(cell_img, name, reason):
    """在单元格左上角标注视角名称; 失败视角在其区域内部标注原因(自动换行,
    只画在本单元格图像上, 不会溢出到其他区域)。"""
    from PIL import ImageDraw
    draw = ImageDraw.Draw(cell_img)
    name_font, reason_font = _fonts()
    x, y = 6, 5
    bb = draw.textbbox((x, y), name, font=name_font)
    draw.rectangle([bb[0] - 4, bb[1] - 3, bb[2] + 4, bb[3] + 3], fill=LABEL_BG)
    draw.text((x, y), name, font=name_font, fill=LABEL_FG)
    y = bb[3] + 7
    if reason:
        max_w = cell_img.width - 12
        for line in _wrap_text(draw, f"渲染失败: {reason}", reason_font, max_w):
            bb = draw.textbbox((x, y), line, font=reason_font)
            draw.rectangle([bb[0] - 3, bb[1] - 2, bb[2] + 3, bb[3] + 2], fill=LABEL_BG)
            draw.text((x, y), line, font=reason_font, fill=ERROR_FG)
            y = bb[3] + 4


# ---------------------------------------------------------------- 主流程
def run(mesh_path, texture_path, views_path, outdir):
    from PIL import Image

    os.makedirs(outdir, exist_ok=True)
    report = {
        "mesh": mesh_path,
        "texture": texture_path,
        "viewsConfig": views_path,
        "meshStatus": "ok",
        "textureStatus": "unknown",
        "faceCount": 0,
        "renderedFaceCount": 0,
        "degenerateFaces": [],
        "outOfRangeUvFaceCount": 0,
        "outOfRangeUvFaces": [],
        "views": [],
        "preview": None,
        "previewWidth": None,
    }

    texture = load_texture(texture_path)
    report["textureStatus"] = "ok" if texture is not None else "missing_or_invalid"

    try:
        view_entries = load_views_config(views_path)
    except Exception as exc:
        report["meshStatus"] = "error"
        report["error"] = f"无法读取视角配置: {exc}"
        _write_report(outdir, report)
        return 2

    # ---- 网格解析失败: 不生成预览图 ----
    try:
        mesh = load_mesh(mesh_path)
    except ValueError as exc:
        report["meshStatus"] = "error"
        report["error"] = str(exc)
        for i, entry in enumerate(view_entries):
            name = entry.get("name") if isinstance(entry, dict) else None
            report["views"].append({
                "index": i,
                "name": name if isinstance(name, str) else f"view_{i}",
                "status": "error",
                "reason": "网格加载失败, 未渲染",
            })
        _write_report(outdir, report)
        return 1

    vertices = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int64)
    report["faceCount"] = int(len(faces))

    # ---- 退化面片检测: 共线或面积 < 1e-8 ----
    areas, face_cross = face_areas(vertices, faces)
    valid_mask = areas >= DEGEN_AREA_EPS
    report["degenerateFaces"] = [int(i) for i in np.nonzero(~valid_mask)[0]]
    report["renderedFaceCount"] = int(valid_mask.sum())

    # ---- 平滑法线(缺法线网格也能平滑光照) ----
    normals = compute_smooth_normals(vertices, faces, valid_mask, face_cross)

    # ---- 纹理坐标越界检测 ----
    uv = getattr(getattr(mesh, "visual", None), "uv", None)
    if uv is not None and len(uv) == len(vertices):
        uv = np.asarray(uv, dtype=np.float64)
        face_uv = uv[faces]                      # (F,3,2)
        out_of_range = ((face_uv[..., 0] < 0.0) | (face_uv[..., 0] > 1.0)
                        | (face_uv[..., 1] < 0.0) | (face_uv[..., 1] > 1.0))
        face_out_of_range = out_of_range.any(axis=1) & valid_mask
    else:
        uv = None
        face_out_of_range = np.zeros(len(faces), dtype=bool)
    report["outOfRangeUvFaces"] = [int(i) for i in np.nonzero(face_out_of_range)[0]]
    report["outOfRangeUvFaceCount"] = len(report["outOfRangeUvFaces"])

    # ---- 逐视角渲染(相互独立) ----
    n = len(view_entries)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    cell_w = TOTAL_WIDTH // cols
    cell_h = cell_w
    canvas = Image.new("RGB", (cell_w * cols, cell_h * rows),
                       tuple(int(c) for c in BG_COLOR))

    for i, entry in enumerate(view_entries):
        parsed, err = validate_view(entry)
        if err is not None:
            name = parsed if isinstance(parsed, str) else f"view_{i}"
            report["views"].append({"index": i, "name": name,
                                    "status": "error", "reason": err})
            cell_arr = np.tile(BG_COLOR, (cell_h, cell_w, 1)).astype(np.uint8)
            cell_img = Image.fromarray(cell_arr, "RGB")
            draw_labels(cell_img, name, err)
        else:
            name, az, el = parsed
            try:
                cell_arr = render_view(vertices, faces, valid_mask, normals,
                                       uv, face_out_of_range, texture,
                                       az, el, cell_w, cell_h)
                report["views"].append({"index": i, "name": name, "status": "rendered"})
                cell_img = Image.fromarray(cell_arr, "RGB")
                draw_labels(cell_img, name, None)
            except Exception as exc:             # 单视角渲染异常不影响其他视角
                reason = f"{type(exc).__name__}: {exc}"
                report["views"].append({"index": i, "name": name,
                                        "status": "error", "reason": reason})
                cell_img = Image.fromarray(
                    np.tile(BG_COLOR, (cell_h, cell_w, 1)).astype(np.uint8), "RGB")
                draw_labels(cell_img, name, reason)
        canvas.paste(cell_img, ((i % cols) * cell_w, (i // cols) * cell_h))

    preview_path = os.path.join(outdir, "preview.png")
    canvas.save(preview_path)
    report["preview"] = "preview.png"
    report["previewWidth"] = int(canvas.width)
    _write_report(outdir, report)
    return 0


def _write_report(outdir, report):
    with open(os.path.join(outdir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)


def main(argv=None):
    ap = argparse.ArgumentParser(description="三维网格四视角预览工具")
    ap.add_argument("--mesh", required=True, help="OBJ 网格路径")
    ap.add_argument("--texture", default=None, help="漫反射纹理 PNG 路径(可缺省)")
    ap.add_argument("--views", required=True, help="视角配置 JSON 路径")
    ap.add_argument("--outdir", required=True, help="输出目录")
    args = ap.parse_args(argv)
    code = run(args.mesh, args.texture, args.views, args.outdir)
    print(f"[mesh_preview] 退出码 {code}, 报告见 {os.path.join(args.outdir, 'report.json')}")
    return code


if __name__ == "__main__":
    sys.exit(main())
