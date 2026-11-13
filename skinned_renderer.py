#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""skinned_renderer.py — 2D 蒙皮网格 + 骨骼关键帧动画 → 逐帧 PNG 序列渲染器。

仅使用 Python 标准库（json / math / struct / zlib / argparse / os / sys）。

输入 JSON 结构：
{
  "mesh": {
    "vertices":  [[x, y], ...],                 // 2D 顶点（像素坐标系）
    "triangles": [[i0, i1, i2], ...],           // 三角形索引
    "weights":   [[[bone, w], ...], ...]        // 每个顶点的 (骨骼索引, 权重) 列表
  },
  "bones": [
    {"name": "root", "parent": null,
     "rest": {"translate": [tx, ty], "rotate": 度}}, ...   // 最多三层层级
  ],
  "animation": {
    "keyframes": [
      {"time": 0.0,
       "bones": {"root": {"translate": [tx, ty], "rotate": 度}, ...}}, ...
    ]
  },
  "output": {
    "width": 128, "height": 128, "frames": 3,
    "start_time": 0.0, "end_time": 2.0,
    "color": [r, g, b, a],                      // 三角形纯色填充
    "directory": "out"                          // 可被命令行 -o 覆盖
  }
}

用法：
    python skinned_renderer.py input.json [-o 输出目录]

退出码：0 成功；2 输入非法或超出限制（不生成任何 PNG）。
"""

import argparse
import json
import math
import os
import struct
import sys
import zlib

MAX_FRAMES = 100                 # 帧数上限
MAX_PIXELS = 4_000_000           # 单帧像素上限
MAX_BONE_DEPTH = 3               # 骨骼层级最大深度
WEIGHT_SUM_MIN = 0.99            # 顶点权重和容许区间
WEIGHT_SUM_MAX = 1.01


class InputError(Exception):
    """输入非法或超出限制时抛出，消息面向用户。"""


# ---------------------------------------------------------------------------
# 2D 仿射变换：矩阵以 6 元组 (a, b, c, d, e, f) 表示
#   | a c e |   x' = a*x + c*y + e
#   | b d f |   y' = b*x + d*y + f
# ---------------------------------------------------------------------------

def mat_identity():
    return (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def mat_mul(m, n):
    """返回 m·n（先应用 n，再应用 m）。"""
    ma, mb, mc, md, me, mf = m
    na, nb, nc, nd, ne, nf = n
    return (
        ma * na + mc * nb,
        mb * na + md * nb,
        ma * nc + mc * nd,
        mb * nc + md * nd,
        ma * ne + mc * nf + me,
        mb * ne + md * nf + mf,
    )


def mat_inverse(m):
    a, b, c, d, e, f = m
    det = a * d - b * c
    if abs(det) < 1e-12:
        raise InputError("骨骼静止姿态变换不可逆（缩放/旋转退化）")
    ia, ib, ic, id_ = d / det, -b / det, -c / det, a / det
    return (ia, ib, ic, id_, -(ia * e + ic * f), -(ib * e + id_ * f))


def mat_from_trs(tx, ty, rot_deg):
    """局部变换 L = T(t) · R(rot)（先旋转后平移）。"""
    r = math.radians(rot_deg)
    c, s = math.cos(r), math.sin(r)
    return (c, s, -s, c, tx, ty)


def mat_apply(m, x, y):
    a, b, c, d, e, f = m
    return (a * x + c * y + e, b * x + d * y + f)


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------

def _is_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _check_trs(obj, ctx):
    if not isinstance(obj, dict):
        raise InputError(f"{ctx} 必须是对象，含 translate/rotate")
    tr = obj.get("translate", [0.0, 0.0])
    rot = obj.get("rotate", 0.0)
    if not (isinstance(tr, list) and len(tr) == 2 and all(_is_num(v) for v in tr)):
        raise InputError(f"{ctx}.translate 必须是两个有限数值")
    if not _is_num(rot):
        raise InputError(f"{ctx}.rotate 必须是有限数值（角度制）")
    return (float(tr[0]), float(tr[1]), float(rot))


def validate_scene(data):
    """校验并预处理输入，返回可直接渲染的 scene 字典。任何非法输入抛 InputError。"""
    if not isinstance(data, dict):
        raise InputError("顶层必须是 JSON 对象")

    # ---- mesh.vertices ----
    mesh = data.get("mesh")
    if not isinstance(mesh, dict):
        raise InputError("缺少必需字段: mesh")
    if "vertices" not in mesh:
        raise InputError("缺少必需字段: mesh.vertices")
    raw_verts = mesh["vertices"]
    if not isinstance(raw_verts, list) or not raw_verts:
        raise InputError("mesh.vertices 必须是非空数组")
    vertices = []
    for i, v in enumerate(raw_verts):
        if not (isinstance(v, list) and len(v) == 2 and all(_is_num(c) for c in v)):
            raise InputError(f"顶点 {i} 坐标非法：必须是两个有限数值，得到 {v!r}")
        vertices.append((float(v[0]), float(v[1])))

    # ---- mesh.triangles ----
    if "triangles" not in mesh:
        raise InputError("缺少必需字段: mesh.triangles")
    raw_tris = mesh["triangles"]
    if not isinstance(raw_tris, list):
        raise InputError("mesh.triangles 必须是数组")
    triangles = []
    for i, t in enumerate(raw_tris):
        if not (isinstance(t, list) and len(t) == 3 and all(_is_int(k) for k in t)):
            raise InputError(f"三角形 {i} 必须是三个整数索引")
        for k in t:
            if not (0 <= k < len(vertices)):
                raise InputError(f"三角形 {i} 引用了越界顶点索引 {k}（共 {len(vertices)} 个顶点）")
        triangles.append((t[0], t[1], t[2]))

    # ---- bones ----
    raw_bones = data.get("bones")
    if not isinstance(raw_bones, list) or not raw_bones:
        raise InputError("缺少必需字段: bones（非空数组）")
    bones = []
    name_to_idx = {}
    for i, b in enumerate(raw_bones):
        if not isinstance(b, dict) or not isinstance(b.get("name"), str):
            raise InputError(f"骨骼 {i} 必须是含字符串 name 的对象")
        name = b["name"]
        if name in name_to_idx:
            raise InputError(f"骨骼名称重复: {name!r}")
        parent = b.get("parent")
        if parent is None:
            parent_idx, depth = -1, 1
        else:
            if parent not in name_to_idx:
                raise InputError(f"骨骼 {name!r} 的父骨骼 {parent!r} 未定义（父骨骼须先声明）")
            parent_idx = name_to_idx[parent]
            depth = bones[parent_idx]["depth"] + 1
        if depth > MAX_BONE_DEPTH:
            raise InputError(f"骨骼 {name!r} 层级深度 {depth} 超过上限 {MAX_BONE_DEPTH} 层")
        rest = _check_trs(b.get("rest", {}), f"骨骼 {name!r} 的 rest")
        name_to_idx[name] = i
        bones.append({"name": name, "parent_idx": parent_idx, "depth": depth, "rest": rest})

    # ---- mesh.weights ----
    if "weights" not in mesh:
        raise InputError("缺少必需字段: mesh.weights")
    raw_weights = mesh["weights"]
    if not isinstance(raw_weights, list) or len(raw_weights) != len(vertices):
        raise InputError(f"mesh.weights 必须与顶点一一对应（{len(vertices)} 项）")
    weights = []
    for i, entries in enumerate(raw_weights):
        if not isinstance(entries, list) or not entries:
            raise InputError(f"顶点 {i} 的权重必须是非空数组 [[骨骼索引, 权重], ...]")
        parsed = []
        for e in entries:
            if not (isinstance(e, list) and len(e) == 2 and _is_int(e[0]) and _is_num(e[1])):
                raise InputError(f"顶点 {i} 的权重项非法: {e!r}（应为 [骨骼索引, 权重]）")
            bi, w = e[0], float(e[1])
            if not (0 <= bi < len(bones)):
                raise InputError(f"顶点 {i} 引用了越界骨骼索引 {bi}（共 {len(bones)} 根骨骼）")
            if w < 0:
                raise InputError(f"顶点 {i} 的权重为负数: {w}")
            parsed.append((bi, w))
        total = sum(w for _, w in parsed)
        if not (WEIGHT_SUM_MIN <= total <= WEIGHT_SUM_MAX):
            raise InputError(
                f"顶点 {i} 的权重和 {total:.4f} 超出允许范围 "
                f"[{WEIGHT_SUM_MIN}, {WEIGHT_SUM_MAX}]，无法归一化"
            )
        # 在容许区间内：归一化使权重和精确为 1
        weights.append([(bi, w / total) for bi, w in parsed])

    # ---- animation.keyframes ----
    anim = data.get("animation", {})
    if not isinstance(anim, dict):
        raise InputError("animation 必须是对象")
    raw_kfs = anim.get("keyframes", [])
    if not isinstance(raw_kfs, list):
        raise InputError("animation.keyframes 必须是数组")
    keyframes = []
    for i, kf in enumerate(raw_kfs):
        if not isinstance(kf, dict) or not _is_num(kf.get("time")):
            raise InputError(f"关键帧 {i} 必须含数值 time")
        locals_trs = list(b["rest"] for b in bones)  # 缺省回退到静止姿态
        kb = kf.get("bones", {})
        if not isinstance(kb, dict):
            raise InputError(f"关键帧 {i} 的 bones 必须是对象")
        for bname, trs in kb.items():
            if bname not in name_to_idx:
                raise InputError(f"关键帧 {i} 引用了未定义骨骼 {bname!r}")
            locals_trs[name_to_idx[bname]] = _check_trs(trs, f"关键帧 {i} 骨骼 {bname!r}")
        keyframes.append({"time": float(kf["time"]), "locals": locals_trs})
    if not keyframes:  # 无动画：静止姿态单关键帧
        keyframes = [{"time": 0.0, "locals": [b["rest"] for b in bones]}]
    keyframes.sort(key=lambda k: k["time"])

    # ---- output ----
    out = data.get("output")
    if not isinstance(out, dict):
        raise InputError("缺少必需字段: output")
    width, height, frames = out.get("width"), out.get("height"), out.get("frames")
    if not (_is_int(width) and _is_int(height) and width >= 1 and height >= 1):
        raise InputError("output.width/height 必须是正整数")
    if not (_is_int(frames) and frames >= 1):
        raise InputError("output.frames 必须是正整数")
    if frames > MAX_FRAMES:
        raise InputError(f"请求帧数 {frames} 超过上限 {MAX_FRAMES}（帧数最多 {MAX_FRAMES} 帧）")
    pixels = width * height
    if pixels > MAX_PIXELS:
        raise InputError(
            f"单帧像素 {width}x{height}={pixels} 超过上限 {MAX_PIXELS}"
        )
    color = out.get("color", [255, 255, 255, 255])
    if not (isinstance(color, list) and len(color) == 4
            and all(_is_int(c) and 0 <= c <= 255 for c in color)):
        raise InputError("output.color 必须是 [r, g, b, a] 四个 0-255 整数")
    start_time = out.get("start_time", 0.0)
    end_time = out.get("end_time", 0.0)
    if not (_is_num(start_time) and _is_num(end_time)):
        raise InputError("output.start_time/end_time 必须是有限数值")
    if end_time < start_time:
        raise InputError("output.end_time 不得小于 start_time")
    directory = out.get("directory", "frames_output")
    if not isinstance(directory, str) or not directory:
        raise InputError("output.directory 必须是非空字符串")

    # ---- 预计算静止姿态全局变换及其逆 ----
    rest_global = []
    for b in bones:
        local = mat_from_trs(*b["rest"])
        g = local if b["parent_idx"] < 0 else mat_mul(rest_global[b["parent_idx"]], local)
        rest_global.append(g)
    inv_rest_global = [mat_inverse(g) for g in rest_global]

    return {
        "vertices": vertices,
        "triangles": triangles,
        "weights": weights,
        "bones": bones,
        "inv_rest_global": inv_rest_global,
        "keyframes": keyframes,
        "output": {
            "width": width, "height": height, "frames": frames,
            "color": tuple(color), "start_time": float(start_time),
            "end_time": float(end_time), "directory": directory,
        },
    }


# ---------------------------------------------------------------------------
# 动画采样与蒙皮
# ---------------------------------------------------------------------------

def sample_pose_locals(scene, t):
    """按关键帧时间线性插值，返回每根骨骼的局部 (tx, ty, rot)。

    t 超出关键帧时间范围时取最近（首/末）关键帧。
    """
    kfs = scene["keyframes"]
    if t <= kfs[0]["time"]:
        return kfs[0]["locals"]
    if t >= kfs[-1]["time"]:
        return kfs[-1]["locals"]
    for a, b in zip(kfs, kfs[1:]):
        if a["time"] <= t <= b["time"]:
            span = b["time"] - a["time"]
            f = 0.0 if span == 0 else (t - a["time"]) / span
            return [
                (la[0] + (lb[0] - la[0]) * f,
                 la[1] + (lb[1] - la[1]) * f,
                 la[2] + (lb[2] - la[2]) * f)
                for la, lb in zip(a["locals"], b["locals"])
            ]
    return kfs[-1]["locals"]  # 不可达，防御


def anim_globals(scene, t):
    """按父子顺序累积局部变换，得到每根骨骼的全局动画变换。"""
    locals_trs = sample_pose_locals(scene, t)
    globals_ = []
    for b, trs in zip(scene["bones"], locals_trs):
        local = mat_from_trs(*trs)
        g = local if b["parent_idx"] < 0 else mat_mul(globals_[b["parent_idx"]], local)
        globals_.append(g)
    return globals_


def skin_vertices(scene, t):
    """线性混合蒙皮（LBS）：v' = Σ w_i · (G_anim_i · G_rest_i⁻¹) · v"""
    globals_ = anim_globals(scene, t)
    inv_rest = scene["inv_rest_global"]
    skin_mats = [mat_mul(g, ir) for g, ir in zip(globals_, inv_rest)]
    result = []
    for (x, y), wentries in zip(scene["vertices"], scene["weights"]):
        # 加权矩阵求和后作用于顶点（等价于各骨骼变换结果的加权和）
        acc = [0.0] * 6
        for bi, w in wentries:
            m = skin_mats[bi]
            for k in range(6):
                acc[k] += w * m[k]
        result.append(mat_apply(tuple(acc), x, y))
    return result


# ---------------------------------------------------------------------------
# 光栅化与 PNG 编码
# ---------------------------------------------------------------------------

def rasterize(scene, skinned):
    """纯色填充三角形，背景全透明。返回 RGBA 字节串。"""
    w, h = scene["output"]["width"], scene["output"]["height"]
    img = bytearray(w * h * 4)  # 全 0 = 全透明
    r, g, b, a = scene["output"]["color"]
    for i0, i1, i2 in scene["triangles"]:
        (x0, y0), (x1, y1), (x2, y2) = skinned[i0], skinned[i1], skinned[i2]
        den = (y1 - y2) * (x0 - x2) + (x2 - x1) * (y0 - y2)
        if den == 0:
            continue  # 退化三角形
        minx = max(0, int(math.floor(min(x0, x1, x2))))
        maxx = min(w - 1, int(math.ceil(max(x0, x1, x2))))
        miny = max(0, int(math.floor(min(y0, y1, y2))))
        maxy = min(h - 1, int(math.ceil(max(y0, y1, y2))))
        for py in range(miny, maxy + 1):
            cy = py + 0.5
            for px in range(minx, maxx + 1):
                cx = px + 0.5
                w0 = ((y1 - y2) * (cx - x2) + (x2 - x1) * (cy - y2)) / den
                w1 = ((y2 - y0) * (cx - x2) + (x0 - x2) * (cy - y2)) / den
                w2 = 1.0 - w0 - w1
                if w0 >= 0 and w1 >= 0 and w2 >= 0:
                    o = (py * w + px) * 4
                    img[o] = r
                    img[o + 1] = g
                    img[o + 2] = b
                    img[o + 3] = a
    return bytes(img)


def encode_png(width, height, rgba):
    """RGBA 字节串 → PNG 文件字节（8 位 RGBA，扫描线滤波器 0）。"""
    if len(rgba) != width * height * 4:
        raise ValueError("RGBA 数据长度与宽高不符")

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    stride = width * 4
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride] for y in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def frame_times(output):
    n = output["frames"]
    if n == 1:
        return [output["start_time"]]
        # 单帧时取起始时刻
    t0, t1 = output["start_time"], output["end_time"]
    return [t0 + (t1 - t0) * i / (n - 1) for i in range(n)]


def run(scene, outdir):
    output = scene["output"]
    os.makedirs(outdir, exist_ok=True)
    times = frame_times(output)
    names = []
    for i, t in enumerate(times):
        rgba = rasterize(scene, skin_vertices(scene, t))
        name = f"frame_{i + 1:04d}.png"
        with open(os.path.join(outdir, name), "wb") as f:
            f.write(encode_png(output["width"], output["height"], rgba))
        names.append(name)
    return names


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="2D 蒙皮网格骨骼动画 → 逐帧 PNG 序列渲染器（仅标准库）")
    parser.add_argument("input", help="输入 JSON 文件路径")
    parser.add_argument("-o", "--outdir", default=None,
                        help="输出目录（缺省用 JSON 中 output.directory）")
    args = parser.parse_args(argv)

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"错误: 输入文件不存在: {args.input}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"错误: 输入文件不是合法 JSON: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"错误: 无法读取输入文件: {e}", file=sys.stderr)
        return 2

    try:
        scene = validate_scene(data)  # 全部校验通过后才会写出任何文件
    except InputError as e:
        print(f"错误: {e}", file=sys.stderr)
        return 2

    outdir = args.outdir or scene["output"]["directory"]
    try:
        names = run(scene, outdir)
    except OSError as e:
        print(f"错误: 写出 PNG 失败: {e}", file=sys.stderr)
        return 2

    print(f"总帧数: {len(names)}")
    print(f"首帧: {names[0]}")
    print(f"末帧: {names[-1]}")
    print(f"输出目录: {os.path.abspath(outdir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
