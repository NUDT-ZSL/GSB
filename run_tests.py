#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_tests.py — 对 skinned_renderer.py 的 9 条验收标准逐项实测。

数值正确性（骨骼层级 / 插值 / 蒙皮）直接调用模块函数验证；
端到端行为（PNG 文件、退出码、摘要）通过子进程运行 CLI 验证。
全部确定性构造，无需外部文件。退出码 0 = 全部通过。
"""

import copy
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import skinned_renderer as sr

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "skinned_renderer.py")
TMP = tempfile.mkdtemp(prefix="renderer_tests_")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -- {detail}" if detail and not cond else ""))


def run_cli(data, outdir):
    """写 JSON 到临时文件并运行 CLI，返回 (exit_code, stdout, stderr)。"""
    path = os.path.join(TMP, "case.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, TOOL, path, "-o", outdir],
                       capture_output=True, text=True, encoding="utf-8", env=env)
    return p.returncode, p.stdout, p.stderr


def read_png(path):
    """读取本工具写出的 PNG（滤波器 0），返回 (w, h, pixels)，pixels[y][x]=(r,g,b,a)。"""
    data = open(path, "rb").read()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "不是合法 PNG 签名"
    pos, w, h, idat = 8, None, None, b""
    while pos < len(data):
        (ln,) = struct.unpack(">I", data[pos:pos + 4])
        tag, payload = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if tag == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", payload[:10])
            assert bd == 8 and ct == 6
        elif tag == b"IDAT":
            idat += payload
    raw = zlib.decompress(idat)
    stride = w * 4
    pixels = []
    for y in range(h):
        row = raw[y * (stride + 1):(y + 1) * (stride + 1)]
        assert row[0] == 0, "未知滤波器"
        pixels.append([tuple(row[1 + x * 4:1 + x * 4 + 4]) for x in range(w)])
    return w, h, pixels


def base_scene():
    return {
        "mesh": {
            "vertices": [[2, 2], [20, 2], [2, 20]],
            "triangles": [[0, 1, 2]],
            "weights": [[[0, 1.0]], [[0, 1.0]], [[0, 1.0]]],
        },
        "bones": [{"name": "root", "parent": None,
                   "rest": {"translate": [0, 0], "rotate": 0}}],
        "animation": {"keyframes": [
            {"time": 0, "bones": {"root": {"translate": [0, 0], "rotate": 0}}},
        ]},
        "output": {"width": 32, "height": 32, "frames": 1,
                   "start_time": 0, "end_time": 0,
                   "color": [255, 0, 0, 255], "directory": "out"},
    }


def scene_of(data):
    return sr.validate_scene(copy.deepcopy(data))


# --- 验收 1：权重和 1.02 报错并指出顶点索引 -------------------------------
def test_weight_sum():
    d = base_scene()
    d["mesh"]["vertices"] = [[2, 2], [20, 2], [2, 20]]
    d["mesh"]["weights"] = [[[0, 1.0]], [[0, 1.02]], [[0, 1.0]]]
    outdir = os.path.join(TMP, "t1")
    code, _, err = run_cli(d, outdir)
    ok = code != 0 and "顶点 1" in err and not os.path.exists(outdir)
    check("1. 权重和1.02报错且含顶点索引", ok, f"code={code} err={err!r}")
    # 边界：0.99~1.01 内可归一化，应成功
    d["mesh"]["weights"] = [[[0, 1.0]], [[0, 1.005]], [[0, 1.0]]]
    code2, _, _ = run_cli(d, os.path.join(TMP, "t1b"))
    check("1b. 权重和1.005归一化后成功", code2 == 0, f"code={code2}")


# --- 验收 2：三层内骨骼层级，父旋转 90° + 子平移，顶点坐标与手算一致 ------
def test_hierarchy():
    d = base_scene()
    d["bones"] = [
        {"name": "root", "parent": None, "rest": {"translate": [0, 0], "rotate": 0}},
        {"name": "mid", "parent": "root", "rest": {"translate": [0, 0], "rotate": 0}},
        {"name": "leaf", "parent": "mid", "rest": {"translate": [0, 0], "rotate": 0}},
    ]
    d["animation"]["keyframes"] = [{"time": 0, "bones": {
        "root": {"translate": [0, 0], "rotate": 90},     # 父旋转 90°
        "mid": {"translate": [0, 0], "rotate": 0},
        "leaf": {"translate": [0, 5], "rotate": 0},      # 子平移 (0,5)
    }}]
    d["mesh"]["vertices"] = [[2, 1], [20, 2], [2, 20]]
    d["mesh"]["weights"] = [[[2, 1.0]], [[2, 1.0]], [[2, 1.0]]]  # 全绑 leaf
    sc = scene_of(d)
    (x, y) = sr.skin_vertices(sc, 0.0)[0]
    # 手算：v=(2,1) → 子平移 (2,6) → 父旋转90°(x,y)->(-y,x) → (-6,2)
    ok = abs(x - (-6.0)) < 1e-9 and abs(y - 2.0) < 1e-9
    check("2. 父旋转90°+子平移，顶点=(-6,2)", ok, f"got=({x},{y})")
    # 第三层再叠加：mid 也平移 (3,0)：v→leaf(2,6)→mid(5,6)→root R90 → (-6,5)
    d["animation"]["keyframes"][0]["bones"]["mid"] = {"translate": [3, 0], "rotate": 0}
    sc = scene_of(d)
    (x, y) = sr.skin_vertices(sc, 0.0)[0]
    ok = abs(x - (-6.0)) < 1e-9 and abs(y - 5.0) < 1e-9
    check("2b. 三层累积，顶点=(-6,5)", ok, f"got=({x},{y})")


# --- 验收 3：关键帧线性插值，中间时刻线性过渡 ------------------------------
def test_interpolation():
    d = base_scene()
    d["animation"]["keyframes"] = [
        {"time": 0, "bones": {"root": {"translate": [0, 0], "rotate": 0}}},
        {"time": 2, "bones": {"root": {"translate": [10, 0], "rotate": 0}}},
    ]
    sc = scene_of(d)
    (x0, y0) = sr.skin_vertices(sc, 0.0)[0]   # (2,2)
    (x1, y1) = sr.skin_vertices(sc, 1.0)[0]   # 中间时刻 → (7,2)
    (x2, y2) = sr.skin_vertices(sc, 2.0)[0]   # (12,2)
    ok = (abs(x0 - 2) < 1e-9 and abs(x1 - 7) < 1e-9 and abs(x2 - 12) < 1e-9
          and abs(y0 - 2) < 1e-9 and abs(y1 - 2) < 1e-9 and abs(y2 - 2) < 1e-9)
    check("3. t=1 时顶点 x 线性过渡 2→7→12", ok, f"x={x0},{x1},{x2}")
    # 超出范围取最近关键帧
    (xm, _) = sr.skin_vertices(sc, 99.0)[0]
    check("3b. 超出范围取最近关键帧", abs(xm - 12) < 1e-9, f"x={xm}")


# --- 验收 4：两骨骼各 0.5 权重，输出为两变换坐标均值 -----------------------
def test_lbs():
    d = base_scene()
    d["bones"] = [
        {"name": "A", "parent": None, "rest": {"translate": [0, 0], "rotate": 0}},
        {"name": "B", "parent": None, "rest": {"translate": [0, 0], "rotate": 0}},
    ]
    d["animation"]["keyframes"] = [{"time": 0, "bones": {
        "A": {"translate": [10, 0], "rotate": 0},
        "B": {"translate": [0, 10], "rotate": 0},
    }}]
    d["mesh"]["vertices"] = [[1, 1], [20, 2], [2, 20]]
    d["mesh"]["weights"] = [[[0, 0.5], [1, 0.5]], [[0, 1.0]], [[0, 1.0]]]
    sc = scene_of(d)
    (x, y) = sr.skin_vertices(sc, 0.0)[0]
    # A 变换 → (11,1)；B 变换 → (1,11)；均值 → (6,6)
    ok = abs(x - 6.0) < 1e-9 and abs(y - 6.0) < 1e-9
    check("4. 双骨骼0.5/0.5，顶点=均值(6,6)", ok, f"got=({x},{y})")


# --- 验收 5：光栅化，中心像素颜色正确，外部 alpha=0 ------------------------
def test_rasterize():
    d = base_scene()
    d["mesh"]["vertices"] = [[10, 10], [50, 10], [10, 50]]
    d["mesh"]["weights"] = [[[0, 1.0]]] * 3
    d["output"]["width"] = d["output"]["height"] = 64
    d["output"]["color"] = [255, 0, 0, 255]
    outdir = os.path.join(TMP, "t5")
    code, _, _ = run_cli(d, outdir)
    if code != 0:
        check("5. 光栅化单三角形", False, "CLI 失败")
        return
    w, h, px = read_png(os.path.join(outdir, "frame_0001.png"))
    inside = px[23][23]      # 三角形内部（质心附近）
    outside = px[60][60]     # 三角形外
    edge_out = px[40][40]    # 斜边 x+y=60 之外
    ok = (inside == (255, 0, 0, 255) and outside[3] == 0 and edge_out[3] == 0)
    check("5. 中心像素红色不透明、外部alpha=0", ok,
          f"inside={inside} outside={outside} edge={edge_out}")


# --- 验收 6+9：导出 3 帧命名递增非空；摘要含总帧数/首帧/输出目录 ----------
def test_export_and_summary():
    d = base_scene()
    d["output"]["frames"] = 3
    d["output"]["end_time"] = 2
    d["animation"]["keyframes"] = [
        {"time": 0, "bones": {"root": {"translate": [0, 0], "rotate": 0}}},
        {"time": 2, "bones": {"root": {"translate": [5, 0], "rotate": 0}}},
    ]
    outdir = os.path.join(TMP, "t6")
    code, out, _ = run_cli(d, outdir)
    names = ["frame_0001.png", "frame_0002.png", "frame_0003.png"]
    files = sorted(os.listdir(outdir)) if os.path.isdir(outdir) else []
    ok_files = (files == names
                and all(os.path.getsize(os.path.join(outdir, n)) > 0 for n in names))
    check("6. 导出3帧 frame_0001..0003 递增非空、无跳号重名", code == 0 and ok_files,
          f"files={files}")
    ok_sum = ("总帧数: 3" in out and "首帧: frame_0001.png" in out
              and "末帧: frame_0003.png" in out
              and os.path.abspath(outdir) in out)
    check("9. 摘要含总帧数/首帧/末帧/输出目录绝对路径", ok_sum, f"stdout={out!r}")


# --- 验收 7：缺 vertices 字段 → 明确错误、退出码非零、无 PNG --------------
def test_missing_field():
    d = base_scene()
    del d["mesh"]["vertices"]
    outdir = os.path.join(TMP, "t7")
    code, _, err = run_cli(d, outdir)
    no_png = not os.path.exists(outdir) or not os.listdir(outdir)
    ok = code != 0 and "vertices" in err and no_png
    check("7. 缺vertices字段→非零退出且无PNG", ok, f"code={code} err={err!r}")
    # 非数值顶点坐标
    d = base_scene()
    d["mesh"]["vertices"][1] = ["abc", 2]
    code2, _, err2 = run_cli(d, os.path.join(TMP, "t7b"))
    ok2 = code2 != 0 and "顶点 1" in err2
    check("7b. 非数值顶点坐标→报错含顶点索引", ok2, f"code={code2} err={err2!r}")


# --- 验收 8：超限拒绝并提示限制值 ------------------------------------------
def test_limits():
    d = base_scene()
    d["output"]["frames"] = 101
    code, _, err = run_cli(d, os.path.join(TMP, "t8"))
    ok = code != 0 and "100" in err
    check("8. 请求101帧→报错且提及上限100", ok, f"code={code} err={err!r}")
    d = base_scene()
    d["output"]["width"], d["output"]["height"] = 2100, 2000  # 4,200,000 > 4,000,000
    code2, _, err2 = run_cli(d, os.path.join(TMP, "t8b"))
    ok2 = code2 != 0 and "4000000" in err2
    check("8b. 单帧像素超限→报错且提及4000000", ok2, f"code={code2} err={err2!r}")


# --- 附加：骨骼层级超过三层拒绝 --------------------------------------------
def test_bone_depth():
    d = base_scene()
    d["bones"] = [
        {"name": "b1", "parent": None, "rest": {"translate": [0, 0], "rotate": 0}},
        {"name": "b2", "parent": "b1", "rest": {"translate": [0, 0], "rotate": 0}},
        {"name": "b3", "parent": "b2", "rest": {"translate": [0, 0], "rotate": 0}},
        {"name": "b4", "parent": "b3", "rest": {"translate": [0, 0], "rotate": 0}},
    ]
    code, _, err = run_cli(d, os.path.join(TMP, "t9"))
    ok = code != 0 and "3" in err
    check("附加. 四层骨骼→报错且提及上限3层", ok, f"code={code} err={err!r}")


def main():
    print(f"临时目录: {TMP}\n")
    test_weight_sum()
    test_hierarchy()
    test_interpolation()
    test_lbs()
    test_rasterize()
    test_export_and_summary()
    test_missing_field()
    test_limits()
    test_bone_depth()
    print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:", ", ".join(FAIL))
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
