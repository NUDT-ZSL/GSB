#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地验证脚本：对 11 条验收要求逐项核对并打印结果。

用法:
    python verify.py
退出码 0 表示全部通过，否则为 1。
"""

import json
import math
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "skinning_check.py")
GEN = os.path.join(HERE, "generate_samples.py")
WORK = os.path.join(HERE, "verify_out")
SAMPLES = os.path.join(WORK, "samples")

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         (" -- " + detail) if detail else ""))


def run_tool(manifest, outdir):
    return subprocess.run(
        [sys.executable, TOOL, manifest, "-o", outdir],
        capture_output=True, text=True)


def load_result(outdir):
    with open(os.path.join(outdir, "result.json"), encoding="utf-8") as fh:
        return json.load(fh)


def frame_of(result, no):
    return next(f for f in result["frames"] if f["frame"] == no)


def vertex_of(frame, char_id, vid):
    matches = [v for v in frame["vertices"]
               if v["character"] == char_id and v["vertex_id"] == vid]
    return matches[0] if matches else None


def close(a, b, tol=1e-3):
    return abs(a - b) < tol


def xy_close(v, x, y, tol=1e-3):
    return v is not None and close(v["x"], x, tol) and close(v["y"], y, tol)


# ---- 独立手算参考（不依赖被测程序的矩阵代码） ----

def rot(p, deg):
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    return (c * p[0] - s * p[1], s * p[0] + c * p[1])


def add(p, q):
    return (p[0] + q[0], p[1] + q[1])


def sub(p, q):
    return (p[0] - q[0], p[1] - q[1])


def scl(p, sx, sy):
    return (p[0] * sx, p[1] * sy)


def main():
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(SAMPLES)
    subprocess.run([sys.executable, GEN, "-o", SAMPLES], check=True,
                   capture_output=True)

    # ---------- 用例 8：输入缺失 -> 提示 + 非零退出 ----------
    proc = subprocess.run([sys.executable, TOOL,
                           os.path.join(WORK, "no_such.json"), "-o",
                           os.path.join(WORK, "x")],
                          capture_output=True, text=True)
    check("8a 输入缺失时非零退出并提示",
          proc.returncode != 0 and "不存在" in proc.stderr,
          "exit=%d stderr=%s" % (proc.returncode, proc.stderr.strip()))

    # ---------- 用例 8：合法输入自动创建输出目录 ----------
    out_translate = os.path.join(WORK, "new_dir", "translate")
    proc = run_tool(os.path.join(SAMPLES, "case_translate.json"), out_translate)
    ok = (proc.returncode == 0
          and os.path.isfile(os.path.join(out_translate, "result.json"))
          and os.path.isfile(os.path.join(out_translate, "pose_frame_0000.svg")))
    check("8b 合法输入创建输出目录并生成姿态图与结果JSON", ok,
          "exit=%d" % proc.returncode)

    # ---------- 用例 2：单骨骼平移，手算对比 ----------
    res = load_result(out_translate)
    f0, f1 = frame_of(res, 0), frame_of(res, 1)
    ok = (xy_close(vertex_of(f0, "hero", 1), 4.0, 6.0)
          and xy_close(vertex_of(f0, "hero", 2), 1.0, 2.0)
          and xy_close(vertex_of(f1, "hero", 1), 2.0, 4.5)
          and xy_close(vertex_of(f1, "hero", 2), -1.0, 0.5))
    check("2 单骨骼平移顶点坐标与手算差<0.001", ok,
          "f0.v1=%s" % vertex_of(f0, "hero", 1))

    # ---------- 用例 3：父子骨骼旋转 + 缩放，逐级手算 ----------
    out_hier = os.path.join(WORK, "hierarchy")
    proc = run_tool(os.path.join(SAMPLES, "case_hierarchy.json"), out_hier)
    res = load_result(out_hier)
    f0, f1 = frame_of(res, 0), frame_of(res, 1)
    # 帧 0：root 旋转 90°，三骨骼蒙皮矩阵均退化为 R90
    exp0 = {1: rot((3, 1), 90), 2: rot((3.5, 0.5), 90), 3: rot((2.5, 0.2), 90)}
    ok = all(xy_close(vertex_of(f0, "knight", vid), *exp0[vid])
             for vid in (1, 2, 3))
    # 帧 1：root R90，arm R-45，hand S(2,1)
    # v1(arm): R90·T(2,0)·R(-45)·T(-2,0)·p
    p = (3, 1)
    e1 = rot(add(rot(sub(p, (2, 0)), -45), (2, 0)), 90)
    # v2(hand): R90·T(2,0)·R(-45)·T(1,0)·S(2,1)·T(-3,0)·p
    p = (3.5, 0.5)
    e2 = rot(add(rot(add(scl(sub(p, (3, 0)), 2, 1), (1, 0)), -45), (2, 0)), 90)
    # v3: 0.5*arm + 0.5*hand，p=(2.5,0.2)
    p = (2.5, 0.2)
    a = rot(add(rot(sub(p, (2, 0)), -45), (2, 0)), 90)
    h = rot(add(rot(add(scl(sub(p, (3, 0)), 2, 1), (1, 0)), -45), (2, 0)), 90)
    e3 = (0.5 * a[0] + 0.5 * h[0], 0.5 * a[1] + 0.5 * h[1])
    ok = ok and xy_close(vertex_of(f1, "knight", 1), *e1) \
        and xy_close(vertex_of(f1, "knight", 2), *e2) \
        and xy_close(vertex_of(f1, "knight", 3), *e3)
    check("3 父子骨骼旋转缩放蒙皮与逐级手算差<0.001", ok,
          "f1.v1期望(%.3f,%.3f) 实际%s" % (e1[0], e1[1],
                                            vertex_of(f1, "knight", 1)))

    # ---------- 用例 1/5/7/9：basic ----------
    out_basic = os.path.join(WORK, "basic")
    proc = run_tool(os.path.join(SAMPLES, "case_basic.json"), out_basic)
    res = load_result(out_basic)
    inv = {(v["character"], v["vertex_id"]): v for v in res["invalid_vertices"]}
    ok = (("hero", 3) in inv and any("权重和" in r for r in inv[("hero", 3)]["reasons"])
          and ("hero", 4) in inv and any("重复" in r for r in inv[("hero", 4)]["reasons"])
          and ("hero", 5) in inv and any("超过上限" in r for r in inv[("hero", 5)]["reasons"]))
    check("1 权重和超差/重复编号/影响数超限顶点列出原因", ok,
          "invalid=%s" % sorted(inv))
    summary = {c["character"]: c for c in res["characters"]}
    hero_inv_count = sum(1 for k in inv if k[0] == "hero")
    ok = (summary["hero"]["valid_vertices"] == 3
          and summary["hero"]["invalid_vertices"] == 3 == hero_inv_count
          and summary["imp"]["valid_vertices"] == 1
          and summary["imp"]["invalid_vertices"] == 0)
    check("9 逐角色合格/不合格顶点汇总与明细一致", ok,
          "hero汇总=%s 明细不合格数=%d" % (summary["hero"], hero_inv_count))
    frame_nos = [f["frame"] for f in res["frames"]]
    ok = frame_nos == [0, 1, 2] and res["ignored_frames"] == [1]
    check("7 帧号升序唯一，被忽略帧号已标注", ok,
          "frames=%s ignored=%s" % (frame_nos, res["ignored_frames"]))
    f0 = frame_of(res, 0)
    hero_ids = [v["vertex_id"] for v in f0["vertices"] if v["character"] == "hero"]
    ok = (hero_ids == sorted(hero_ids)
          and vertex_of(f0, "hero", 3)["x"] is None
          and vertex_of(f0, "hero", 3)["y"] is None
          and vertex_of(f0, "hero", 1)["x"] is not None)
    check("5 顶点按编号升序、缺失值用null、字段齐全", ok,
          "hero顶点顺序=%s" % hero_ids)
    ok = (isinstance(res["canvas"]["width"], int)
          and isinstance(res["canvas"]["height"], int)
          and close(res["canvas"]["aspect_ratio"],
                    round(res["canvas"]["width"] / res["canvas"]["height"], 3)))
    check("4a 图幅比例写入结果JSON", ok, "canvas=%s" % res["canvas"])

    # ---------- 用例 4b：姿态图内容 ----------
    svg_path = os.path.join(out_basic, "pose_frame_0000.svg")
    with open(svg_path, encoding="utf-8") as fh:
        svg = fh.read()
    ok = ("<line" in svg and "<circle" in svg
          and ">1<" in svg and ">2<" in svg      # 合格顶点在图中
          and ">3<" not in svg and ">5<" not in svg  # 不合格顶点不出图
          and svg.count(">4<") == 1)             # 重复编号仅首个出图
    check("4b 姿态图含骨骼连线与合格顶点，不合格顶点不出图", ok)

    # ---------- 用例 6：未知骨骼 / 帧缺骨骼 ----------
    out_unk = os.path.join(WORK, "unknown_bone")
    run_tool(os.path.join(SAMPLES, "case_unknown_bone.json"), out_unk)
    res = load_result(out_unk)
    inv = {(v["character"], v["vertex_id"]): v for v in res["invalid_vertices"]}
    ok = (("imp", 2) in inv and any("不存在的骨骼" in r for r in inv[("imp", 2)]["reasons"])
          and ("imp", 3) in inv and any("缺少骨骼" in r for r in inv[("imp", 3)]["reasons"])
          and ("imp", 1) not in inv)
    f0 = frame_of(res, 0)
    ok = ok and xy_close(vertex_of(f0, "imp", 1), 2.0, 2.0)
    check("6 未知骨骼/帧缺骨骼顶点列入不合格，其余照常", ok,
          "invalid=%s" % sorted(inv))
    # 一致性：去掉不合格顶点后，顶点 1 坐标不变
    variant = {
        "characters": [{
            "id": "imp",
            "bones": [
                {"id": "root", "parent": None, "rest_position": [0, 0]},
                {"id": "spine", "parent": "root", "rest_position": [1, 0]},
            ],
            "vertices": [
                {"id": 1, "position": [1, 1],
                 "influences": [{"bone": "root", "weight": 1.0}]},
            ],
        }],
        "frames": [
            {"frame": 0, "transforms": {"imp": {
                "root": {"translate": [1, 1], "rotate": 0, "scale": [1, 1]},
                "spine": {"translate": [0, 0], "rotate": 0, "scale": [1, 1]}}}},
            {"frame": 1, "transforms": {"imp": {
                "root": {"translate": [0, 1], "rotate": 0, "scale": [1, 1]}}}},
        ],
    }
    variant_path = os.path.join(WORK, "variant_clean.json")
    with open(variant_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(variant, fh, ensure_ascii=False)
    out_clean = os.path.join(WORK, "unknown_bone_clean")
    run_tool(variant_path, out_clean)
    res_clean = load_result(out_clean)
    ok = True
    for no in (0, 1):
        a = vertex_of(frame_of(res, no), "imp", 1)
        b = vertex_of(frame_of(res_clean, no), "imp", 1)
        ok = ok and a["x"] == b["x"] and a["y"] == b["y"]
    check("6b 剔除不合格顶点后其余顶点坐标一致", ok)

    # ---------- 用例 10：重复运行逐字节一致 ----------
    out_a, out_b = os.path.join(WORK, "rep_a"), os.path.join(WORK, "rep_b")
    run_tool(os.path.join(SAMPLES, "case_basic.json"), out_a)
    run_tool(os.path.join(SAMPLES, "case_basic.json"), out_b)
    names_a = sorted(os.listdir(out_a))
    ok = names_a == sorted(os.listdir(out_b))
    for name in names_a:
        with open(os.path.join(out_a, name), "rb") as fa, \
             open(os.path.join(out_b, name), "rb") as fb:
            ok = ok and fa.read() == fb.read()
    check("10 连续两次运行结果JSON与姿态图逐字节相同", ok,
          "文件=%s" % names_a)

    # ---------- 用例 11：空清单 / 无合格顶点 ----------
    out_empty = os.path.join(WORK, "empty")
    proc = run_tool(os.path.join(SAMPLES, "case_empty.json"), out_empty)
    res = load_result(out_empty)
    ok = (proc.returncode == 0
          and res["frames"] == [] and res["characters"] == []
          and res["invalid_vertices"] == []
          and os.path.isfile(os.path.join(out_empty, "pose_empty.svg")))
    check("11a 空清单退出码为0，结果JSON结构完整且含空列表", ok,
          "exit=%d" % proc.returncode)
    out_nv = os.path.join(WORK, "no_valid")
    proc = run_tool(os.path.join(SAMPLES, "case_no_valid.json"), out_nv)
    res = load_result(out_nv)
    ok = (proc.returncode == 0
          and res["characters"][0]["valid_vertices"] == 0
          and res["characters"][0]["invalid_vertices"] == 1
          and frame_of(res, 0)["vertices"][0]["x"] is None
          and os.path.isfile(os.path.join(out_nv, "pose_frame_0000.svg")))
    check("11b 角色无合格顶点仍生成姿态图与结构完整JSON", ok,
          "exit=%d" % proc.returncode)

    failed = [r for r in RESULTS if not r[1]]
    print("\n==== 共 %d 项检查，通过 %d 项，失败 %d 项 ===="
          % (len(RESULTS), len(RESULTS) - len(failed), len(failed)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
